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

    def set_commands(self, commands: list[tuple[str, str]]):
        """Show the command menu in Telegram. Best effort."""
        if self.configured:
            self._call("setMyCommands", {"commands": [{"command": c.lstrip("/"), "description": d} for c, d in commands]})

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


def _held(opened_ms: int, now_ms: int) -> str:
    minutes = max(0, now_ms - opened_ms) // 60_000
    return f"{minutes // 60}sa {minutes % 60}dk"


def fmt_positions(rows: list[dict], balance: dict | None, now_ms: int) -> str:
    """rows: trade fields + live exchange fields (mark, pnl, liquidation) per open position."""
    if not rows:
        return "📭 Açık pozisyon yok."
    lines = [f"📌 <b>Açık pozisyonlar ({len(rows)})</b>"]
    total = 0.0
    for r in rows:
        total += r["pnl"]
        sign = 1 if r["side"] == "LONG" else -1
        margin = r["quantity"] * r["entry"] / r["leverage"]
        move = sign * (r["mark"] - r["entry"]) / r["entry"] * 100
        icon = "🟢" if r["side"] == "LONG" else "🔴"
        lines += [f"{icon} <b>{esc(r['side'])}</b> <code>{esc(r['symbol'])}</code> {r['leverage']}x",
                  f"Giriş {price(r['entry'])} → Anlık {price(r['mark'])} ({move:+.2f}%)",
                  f"PnL: <b>{r['pnl']:+,.2f} USDT</b> · ROE {r['pnl'] / margin * 100:+.1f}% · {r['pnl'] / r['planned_risk']:+.2f}R"
                  if r.get("planned_risk") else f"PnL: <b>{r['pnl']:+,.2f} USDT</b> · ROE {r['pnl'] / margin * 100:+.1f}%",
                  f"Stop {price(r['stop'])}{' (başa baş)' if r.get('breakeven_done') else ''} · Hedef {price(r['take_profit'])}"
                  + (f" · Tasfiye {price(r['liquidation'])}" if r.get("liquidation") else ""),
                  f"Marj {money(margin)} USDT · Süre {_held(r['opened_ms'], now_ms)}"]
    lines.append(f"━━━━━━━━\nToplam gerçekleşmemiş: <b>{total:+,.2f} USDT</b>")
    if balance:
        lines.append(f"Cüzdan {money(balance['wallet'])} USDT · Özkaynak {money(balance['wallet'] + balance['unrealized'])} USDT")
    return "\n".join(lines)


def fmt_pnl(daily: dict, totals: dict, unrealized: float | None, history: list[dict], balance: dict | None) -> str:
    realized_today = daily.get("realized", 0.0)
    lines = [f"💰 <b>Kâr / Zarar</b> ({esc(daily.get('date', '-'))} UTC)",
             f"Bugün gerçekleşen: <b>{realized_today:+,.2f} USDT</b> ({daily.get('trades', 0)} işlem, {daily.get('wins', 0)} kazanan)"]
    if unrealized is not None:
        lines.append(f"Açık pozisyonlar (gerçekleşmemiş): <b>{unrealized:+,.2f} USDT</b>")
        lines.append(f"Bugün toplam: <b>{realized_today + unrealized:+,.2f} USDT</b>")
    trades = totals.get("trades", 0)
    rate = f"%{totals.get('wins', 0) / trades * 100:.0f}" if trades else "-"
    lines.append(f"Tüm zamanlar: {totals.get('realized', 0.0):+,.2f} USDT · {trades} işlem · isabet {rate}")
    if balance:
        lines.append(f"Cüzdan {money(balance['wallet'])} USDT")
    recent = [h for h in history[-5:]][::-1]
    if recent:
        lines.append("Son işlemler:")
        lines += [f"{'✅' if (h['pnl'] or 0) > 0 else '❌' if h['pnl'] is not None else '⚪'} {esc(h['symbol'])} {esc(h['side'])} "
                  f"{'?' if h['pnl'] is None else format(h['pnl'], '+.2f')} ({esc(h['reason'])})" for h in recent]
    return "\n".join(lines)


def now_utc_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
