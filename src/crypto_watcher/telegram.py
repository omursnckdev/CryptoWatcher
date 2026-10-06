"""Telegram notifications (Turkish) and command polling. Uses the plain Bot API over HTTPS."""
from datetime import datetime, timezone
import html
import logging
import re
import time
import requests

log = logging.getLogger(__name__)
LIMIT = 4000  # Telegram caps messages at 4096 characters

REASONS = {"STOP_LOSS": "Stop-loss tetiklendi", "TAKE_PROFIT": "Take-profit hedefi vuruldu",
           "BREAKEVEN_STOP": "Başa baş stop tetiklendi", "TIME_STOP": "Zaman stopu (azami bekleme süresi doldu)",
           "EXTERNAL": "Bot dışında kapandı (manuel / tasfiye / bilinmiyor)"}


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def money(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def price(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 100 else f"{value:.6g}" if abs(value) < 1 else f"{value:,.4f}"


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        for piece in [line[i:i + limit] for i in range(0, len(line), limit)] or [""]:
            if current and len(current) + len(piece) + 1 > limit:
                chunks.append(current)
                current = ""
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    """Never raises: a Telegram outage must not stop trading. Falls back to logging if unconfigured."""

    def __init__(self, token: str = "", chat_id: str = "", session=None, sleep=time.sleep, timeout: float = 15.0):
        self.token, self.chat_id = token, str(chat_id)
        self.session, self._sleep, self.timeout = session or requests.Session(), sleep, timeout
        self.sent: list[str] = []
        self._rejected = False

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _scrub(self, text) -> str:
        return str(text).replace(self.token, "***") if self.token else str(text)

    def _call(self, method: str, payload: dict) -> dict | None:
        self._rejected = False
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for attempt in range(3):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout)
                data = response.json()
            except (requests.RequestException, ValueError) as error:
                log.warning("Telegram %s failed: %s", method, self._scrub(error))
                self._sleep(1 + attempt)
                continue
            if data.get("ok"):
                return data
            if response.status_code == 429:
                self._sleep(min(float(data.get("parameters", {}).get("retry_after", 2)), 30))
                continue
            log.warning("Telegram %s rejected: %s", method, self._scrub(data.get("description")))
            self._rejected = True
            return None
        return None

    def send(self, text: str) -> bool:
        self.sent.append(text)
        log.info("NOTIFY %s", re.sub(r"<[^>]+>", "", text).replace("\n", " | "))
        if not self.configured:
            return False
        ok = True
        for chunk in split_message(text):
            sent = self._call("sendMessage", {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
            if sent is None and self._rejected:  # e.g. a chunk cut through a tag: resend as plain text, don't lose it
                sent = self._call("sendMessage", {"chat_id": self.chat_id, "text": html.unescape(re.sub(r"<[^>]+>", "", chunk)),
                                                   "disable_web_page_preview": True})
            ok &= sent is not None
        return ok

    def poll(self, offset: int) -> tuple[list[str], int]:
        """Commands from the authorised chat only. -> (commands, next_offset)."""
        if not self.configured:
            return [], offset
        data = self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
        commands = []
        for update in (data or {}).get("result", []):
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            if str(message.get("chat", {}).get("id")) == self.chat_id and text.startswith("/"):
                commands.append(text.split()[0].split("@")[0].lower())
        return commands, offset


# ---- message templates ----------------------------------------------------
def fmt_open(trade: dict, candidate: dict, dry_run: bool = False) -> str:
    side = trade["side"]
    icon, label = ("🟢", "LONG") if side == "LONG" else ("🔴", "SHORT")
    sign = 1 if side == "LONG" else -1
    entry, stop, take = trade["entry"], trade["stop"], trade["take_profit"]
    result = candidate["sides"][side]
    head = f"{icon} <b>{label} {'SİNYALİ (işlem açılmadı)' if dry_run else 'AÇILDI'}</b> · <code>{esc(trade['symbol'])}</code> · testnet"
    lines = [head,
             f"Kaldıraç: {trade['leverage']}x {esc(trade.get('margin_type', 'ISOLATED'))}",
             f"Giriş: {price(entry)} · Miktar: {trade['quantity']:g} (≈ {money(trade['quantity'] * entry)} USDT)",
             f"Stop: {price(stop)} ({sign * (stop - entry) / entry * 100:+.2f}%) · "
             f"Hedef: {price(take)} ({sign * (take - entry) / entry * 100:+.2f}%)",
             f"Marj: {money(trade['quantity'] * entry / trade['leverage'])} USDT · "
             f"Planlanan risk: {money(trade['planned_risk'])} USDT · R:R 1:{trade['risk_reward']:g}",
             f"Skor: <b>{result['score']:.1f}</b>/100 ({esc(candidate['signal'])}) · BTC rejimi: {esc(candidate['market_regime'])}"
             f" · Funding: {candidate['funding_rate'] * 100:+.4f}%"]
    reasons = [r for r in result["reasons"]["positive"]][:5]
    if reasons:
        lines.append("Gerekçe: " + "; ".join(esc(r.removesuffix(" ✓")) for r in reasons))
    news = candidate.get("news")
    if news and news["count"]:
        lines.append(f"📰 Haber ({news['count']}): duyarlılık {news['sentiment']:+.2f}")
        lines.extend(f"  • {esc(h)}" for h in news["headlines"][:2])
    else:
        lines.append("📰 Haber: bu coin için güncel haber bulunamadı (skora katılmadı)")
    return "\n".join(lines)


def fmt_close(trade: dict, outcome: dict) -> str:
    pnl = outcome.get("net_pnl")
    icon = "⚪" if pnl is None else "✅" if pnl > 0 else "❌"
    held = max(0, outcome["closed_ms"] - trade["opened_ms"]) // 60_000
    lines = [f"{icon} <b>POZİSYON KAPANDI</b> · <code>{esc(trade['symbol'])}</code> {esc(trade['side'])} · testnet",
             f"Sebep: {esc(REASONS.get(outcome['reason'], outcome['reason']))}",
             f"Giriş → Çıkış: {price(trade['entry'])} → {price(outcome['exit_price']) if outcome.get('exit_price') else '?'}"]
    if pnl is None:
        lines.append("Net PnL: alınamadı (borsa işlem kaydı bulunamadı)")
    else:
        lines.append(f"Net PnL: <b>{pnl:+,.2f} USDT</b> ({outcome['r_multiple']:+.2f}R) · Komisyon: {outcome['fees']:.2f} USDT")
    lines.append(f"Süre: {held // 60}sa {held % 60}dk")
    return "\n".join(lines)


def fmt_breakeven(trade: dict, new_stop: float, mark: float) -> str:
    return (f"🛡 <b>STOP BAŞA BAŞA ÇEKİLDİ</b> · <code>{esc(trade['symbol'])}</code> {esc(trade['side'])}\n"
            f"Fiyat {price(mark)} (+{trade['breakeven_r']:g}R) → yeni stop {price(new_stop)}")


def fmt_warning(text: str) -> str:
    return f"⚠️ <b>UYARI</b>\n{esc(text)}"


def fmt_start(settings, universe: list[str], source: str, dry_run: bool, balance: dict | None, warning: str | None) -> str:
    lines = [f"🤖 <b>CryptoWatcher başladı</b>{' (DRY-RUN: emir gönderilmez)' if dry_run else ' · Binance Futures TESTNET'}",
             f"Veri kaynağı: {esc(source)} · Zaman dilimi: {settings.timeframe}/{settings.htf}",
             f"Kaldıraç {settings.leverage}x · risk/işlem %{settings.risk_fraction * 100:g} · en fazla {settings.max_open_positions} pozisyon",
             f"Evren ({len(universe)}): {esc(', '.join(s.removesuffix('USDT') for s in universe[:15]))}{'…' if len(universe) > 15 else ''}"]
    if balance:
        lines.append(f"Testnet bakiye: {money(balance['wallet'])} USDT")
    if warning:
        lines.append(f"⚠️ {esc(warning)}")
    return "\n".join(lines)


def fmt_daily(day: dict) -> str:
    wins, trades = day.get("wins", 0), day.get("trades", 0)
    return (f"📊 <b>Günlük özet {esc(day.get('date', ''))}</b>\nKapanan işlem: {trades} (kazanan {wins})\n"
            f"Gerçekleşen PnL: <b>{day.get('realized', 0):+,.2f} USDT</b>")


def fmt_top(report: dict, n: int = 5) -> str:
    lines = [f"🔎 <b>En yüksek skorlar</b> · BTC rejimi {esc(report['market_regime'])}"]
    for s in report["signals"][:n]:
        lines.append(f"<code>{esc(s['symbol']):10}</code> {s['score']:5.1f} {esc(s['side'])} {esc(s['signal'])}")
    return "\n".join(lines)


def now_utc_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
