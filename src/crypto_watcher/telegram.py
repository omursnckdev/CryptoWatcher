"""Telegram notifications (Turkish) and command polling. Uses the plain Bot API over HTTPS."""
from datetime import datetime, timezone
import html
import logging
import re
import time
import requests
from .net import make_session

log = logging.getLogger(__name__)
LIMIT = 4000  # Telegram caps messages at 4096 characters

SHORT_REASONS = {"STOP_LOSS": "stop", "TAKE_PROFIT": "hedef", "BREAKEVEN_STOP": "başa baş stop",
                 "TIME_STOP": "zaman stopu", "EXTERNAL": "dışarıdan kapandı"}


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
        self.session, self._sleep, self.timeout = session or make_session(), sleep, timeout
        self.sent: list[str] = []
        self._rejected = False
        self.username = ""

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
            moved = (data.get("parameters") or {}).get("migrate_to_chat_id")
            log.warning("Telegram %s rejected: %s%s", method, self._scrub(data.get("description")),
                        f" -> the group became a supergroup, set TELEGRAM_CHAT_ID={moved}" if moved else "")
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

    def poll(self, offset: int) -> tuple[list[tuple[str, str]], int]:
        """Commands from the authorised chat only. -> ([(command, sender_user_id)], next_offset).

        Works in private chats and groups; `/cmd@BotName` is accepted, commands for other bots are ignored."""
        if not self.configured:
            return [], offset
        data = self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})
        commands = []
        for update in (data or {}).get("result", []):
            offset = max(offset, update["update_id"] + 1)
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            if str(message.get("chat", {}).get("id")) != self.chat_id or not text.startswith("/"):
                continue
            head, _, target = text.split()[0].partition("@")
            if target and self.username and target.lower() != self.username.lower():
                continue
            commands.append((head.lower(), str(message.get("from", {}).get("id", ""))))
        return commands, offset

    def discover(self) -> dict:
        """Chats and users the bot has recently seen (for `cryptowatcher telegram-id`)."""
        data = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message", "my_chat_member"]}) or {}
        chats, users = {}, {}
        for update in data.get("result", []):
            message = update.get("message") or update.get("my_chat_member") or {}
            chat, sender = message.get("chat") or {}, message.get("from") or {}
            if chat.get("id") is not None:
                chats[chat["id"]] = (chat.get("type", "?"), chat.get("title") or chat.get("first_name") or chat.get("username") or "")
            if sender.get("id") is not None and not sender.get("is_bot"):
                users[sender["id"]] = sender.get("first_name") or sender.get("username") or ""
        return {"chats": chats, "users": users}

    def whoami(self) -> str:
        data = self._call("getMe", {})
        return (data or {}).get("result", {}).get("username", "")


# ---- message templates (compact by default; `verbose` adds reasons and headlines) ---------
def _pct(side: str, entry: float, level: float) -> float:
    return (1 if side == "LONG" else -1) * (level - entry) / entry * 100


def _icon(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def _news_tag(candidate: dict) -> str:
    news = candidate.get("news")
    return f" · 📰 {news['sentiment']:+.1f} ({news['count']})" if news and news["count"] else ""


def fmt_open(trade: dict, candidate: dict, verbose: bool = False) -> str:
    side, entry = trade["side"], trade["entry"]
    result = candidate["sides"][side]
    lines = [f"{_icon(side)} <b>{esc(side)} {esc(trade['symbol'])}</b> {trade['leverage']}x @ {price(entry)} · skor {result['score']:.0f}",
             f"SL {price(trade['stop'])} ({_pct(side, entry, trade['stop']):+.1f}%) · TP {price(trade['take_profit'])} "
             f"({_pct(side, entry, trade['take_profit']):+.1f}%) · risk {money(trade['planned_risk'], 0)} USDT{_news_tag(candidate)}"]
    if verbose:
        lines.append(f"Miktar {trade['quantity']:g} (≈ {money(trade['quantity'] * entry, 0)} USDT) · marj "
                     f"{money(trade['quantity'] * entry / trade['leverage'], 0)} USDT · BTC {esc(candidate['market_regime'])} · "
                     f"funding {candidate['funding_rate'] * 100:+.3f}%")
        reasons = "; ".join(esc(r.removesuffix(" ✓")) for r in result["reasons"]["positive"][:5])
        if reasons:
            lines.append("Gerekçe: " + reasons)
        news = candidate.get("news")
        if news and news["count"]:
            lines.extend(f"📰 {esc(h)}" for h in news["headlines"][:2])
    return "\n".join(lines)


def fmt_signals(candidates: list[dict], limit: int = 5) -> str:
    """One digest per candle for --dry-run instead of one message per coin."""
    lines = [f"🔔 <b>{len(candidates)} sinyal</b> (dry-run: emir yok)"]
    for c in candidates[:limit]:
        plan = c["sides"][c["side"]]["risk_plan"]
        lines.append(f"{_icon(c['side'])} {esc(c['side'])} <code>{esc(c['symbol'])}</code> skor {c['score']:.0f} · "
                     f"@ {price(plan['entry'])} SL {price(plan['stop'])} TP {price(plan['take_profit'])}")
    if len(candidates) > limit:
        lines.append(f"… +{len(candidates) - limit} daha (/top)")
    return "\n".join(lines)


def fmt_close(trade: dict, outcome: dict) -> str:
    pnl = outcome.get("net_pnl")
    icon = "⚪" if pnl is None else "✅" if pnl > 0 else "❌"
    held = max(0, outcome["closed_ms"] - trade["opened_ms"]) // 60_000
    result = "PnL alınamadı" if pnl is None else f"<b>{pnl:+,.2f} USDT</b> ({outcome['r_multiple']:+.1f}R)"
    return (f"{icon} <b>{esc(trade['symbol'])} {esc(trade['side'])} kapandı</b> · {esc(SHORT_REASONS.get(outcome['reason'], outcome['reason']))}\n"
            f"{price(trade['entry'])} → {price(outcome['exit_price']) if outcome.get('exit_price') else '?'} · {result} · "
            f"{held // 60}sa {held % 60}dk")


def fmt_breakeven(trade: dict, new_stop: float, mark: float) -> str:
    return f"🛡 {esc(trade['symbol'])} stop başa baş: {price(new_stop)}"


def fmt_warning(text: str) -> str:
    """Only for safety-critical events (an unprotected position)."""
    return f"⚠️ {esc(text)}"


def fmt_top(report: dict, n: int = 5) -> str:
    lines = [f"🔎 <b>En yüksek skorlar</b> · BTC {esc(report['market_regime'])}"]
    for s in report["signals"][:n]:
        lines.append(f"<code>{esc(s['symbol']):10}</code> {s['score']:4.0f} {esc(s['side'])} {esc(s['signal'])}")
    return "\n".join(lines)


def _held(opened_ms: int, now_ms: int) -> str:
    minutes = max(0, now_ms - opened_ms) // 60_000
    return f"{minutes // 60}sa{minutes % 60:02d}dk"


def fmt_positions(rows: list[dict], balance: dict | None, now_ms: int, verbose: bool = False) -> str:
    """rows: trade fields + live exchange fields (mark, pnl, liquidation). Two lines per position."""
    if not rows:
        return "📭 Açık pozisyon yok."
    lines = []
    total = 0.0
    for r in rows:
        total += r["pnl"]
        margin = r["quantity"] * r["entry"] / r["leverage"]
        risk = f" · {r['pnl'] / r['planned_risk']:+.1f}R" if r.get("planned_risk") else ""
        lines.append(f"{_icon(r['side'])} <b>{esc(r['symbol'])}</b> {esc(r['side'])} {r['leverage']}x · "
                     f"<b>{r['pnl']:+,.2f} USDT</b> ({r['pnl'] / margin * 100:+.1f}%){risk}")
        detail = (f"   {price(r['entry'])} → {price(r['mark'])} · SL {price(r['stop'])}{'*' if r.get('breakeven_done') else ''}"
                  f" · TP {price(r['take_profit'])} · {_held(r['opened_ms'], now_ms)}")
        if verbose and r.get("liquidation"):
            detail += f" · tasfiye {price(r['liquidation'])}"
        lines.append(detail)
    footer = f"Toplam açık: <b>{total:+,.2f} USDT</b>"
    if balance:
        footer += f" · özkaynak {money(balance['wallet'] + balance['unrealized'], 0)} USDT"
    return "\n".join(lines + [footer])


def fmt_pnl(daily: dict, totals: dict, unrealized: float | None, history: list[dict], balance: dict | None) -> str:
    realized = daily.get("realized", 0.0)
    head = f"💰 <b>Bugün: {realized + (unrealized or 0):+,.2f} USDT</b>"
    head += f" (kapanan {realized:+,.2f} · açık {unrealized:+,.2f})" if unrealized is not None else f" (kapanan, {daily.get('trades', 0)} işlem)"
    trades = totals.get("trades", 0)
    rate = f"%{totals.get('wins', 0) / trades * 100:.0f}" if trades else "-"
    lines = [head, f"Tüm zamanlar: {totals.get('realized', 0.0):+,.2f} USDT · {trades} işlem · isabet {rate}"
             + (f" · cüzdan {money(balance['wallet'], 0)}" if balance else "")]
    recent = history[-5:][::-1]
    if recent:
        lines.append("Son: " + " · ".join(
            f"{'✅' if (h['pnl'] or 0) > 0 else '❌' if h['pnl'] is not None else '⚪'}{esc(h['symbol'].removesuffix('USDT'))} "
            f"{'?' if h['pnl'] is None else format(h['pnl'], '+.0f')}" for h in recent))
    return "\n".join(lines)


def now_utc_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
