"""Trade journal and loss post-mortem.

For every closed trade the journal keeps what the bot knew at entry (score, regime, indicators, reasons), how the trade
moved while open (best / worst excursion in R: MFE / MAE) and, a while after the close, what the price did next. From
this it answers "why did it lose?" in plain words: it never went our way, it went a bit and came back, the stop was
hit and price then recovered (stop too tight) or kept falling (stop saved us).

Nothing here sends orders or pushes Telegram messages; a failure is logged and never reaches the trading loop.
"""
from datetime import datetime, timezone
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
FOLLOWUP_HOURS = 12            # how long after the close we watch the price
FOLLOWUP_BAR = "5m"
MIN_SAMPLE = 20                # below this /analiz shows numbers but refuses to draw conclusions
NEVER_WORKED_R = 0.25          # best excursion below this: the trade never really went our way
REVERSED_R = 1.0               # after the stop, price travelled this far (in R) in our direction / against it

KIND_TEXT = {
    "win": "hedefe/kâra ulaştı",
    "never_worked": "hiç lehine gitmedi",
    "faded": "bir miktar lehine gitti, geri döndü",
    "gave_back": "kârdayken geri verdi",
    "breakeven": "başa baş stop çalıştı",
    "time": "süre doldu, yön oluşmadı",
    "external": "dışarıdan kapandı",
    "unknown": "veri yetersiz",
}
STOP_TEXT = {
    "too_tight": "stoptan sonra fiyat dönüp lehimize gitti (stop dar/gürültüye takıldı)",
    "right_exit": "stoptan sonra fiyat aleyhe devam etti (stop doğru çıkıştı)",
    "neutral": "stoptan sonra belirgin bir yön olmadı",
}


# ---- pure helpers ---------------------------------------------------------------------------------------------
def snapshot(candidate: dict) -> dict:
    """What the bot saw when it decided to enter (small and JSON-safe)."""
    result = candidate["sides"][candidate["side"]]
    ind = candidate.get("indicators", {})
    news = candidate.get("news")
    return {"score": round(float(result["score"]), 1), "regime": candidate.get("market_regime"),
            "funding": candidate.get("funding_rate"), "atr_pct": round(float(candidate.get("atr_pct", 0.0)), 5),
            "rsi": round(ind.get("rsi", float("nan")), 1), "adx": round(ind.get("adx", float("nan")), 1),
            "volume_ratio": round(ind.get("volume_ratio", float("nan")), 2), "bb_pctb": round(ind.get("bb_pctb", float("nan")), 2),
            "news": None if not news or not news.get("count") else news["sentiment"],
            "reasons": [r.removesuffix(" ✓") for r in result["reasons"]["positive"][:4]]}


def track(trade: dict, mark: float, now_ms: int):
    """Update the running best / worst excursion (in R) of an open trade. Sampled on every manage tick."""
    distance = trade.get("risk_distance")
    if not distance or not mark:
        return
    sign = 1 if trade["side"] == "LONG" else -1
    r = sign * (mark - trade["entry"]) / distance
    if r > trade.get("mfe_r", 0.0):
        trade["mfe_r"], trade["mfe_ms"] = r, now_ms
    if r < trade.get("mae_r", 0.0):
        trade["mae_r"], trade["mae_ms"] = r, now_ms


def diagnose(record: dict) -> str:
    """Classify HOW a trade ended, from the facts recorded at close."""
    reason, r = record.get("reason"), record.get("r")
    mfe = record.get("mfe_r") or 0.0
    if r is None:                      # no fills / no PnL: nothing reliable to say about this trade
        return "unknown"
    if reason == "TAKE_PROFIT" or r > 0.3:
        return "win"
    if reason == "EXTERNAL":
        return "external"
    if reason == "TIME_STOP":
        return "time"
    if reason == "BREAKEVEN_STOP":
        return "breakeven"
    if mfe < NEVER_WORKED_R:
        return "never_worked"
    if mfe < REVERSED_R:
        return "faded"
    return "gave_back"


def stop_verdict(record: dict, bars: list) -> dict | None:
    """What price did in the FOLLOWUP_HOURS after the close, measured from the first 5-minute bar after it.

    Only relative moves are used (the trade ran on testnet prices, the bars come from mainnet). Returns None while the bars
    do not yet cover the window."""
    closed, distance, entry = record["closed_ms"], record.get("risk_distance"), record["entry"]
    if not distance or not entry or not bars:
        return None
    window = [b for b in bars if closed <= int(b[0]) < closed + FOLLOWUP_HOURS * HOUR_MS]
    if len(window) < FOLLOWUP_HOURS * 12 * 0.9:
        return None
    sign = 1 if record["side"] == "LONG" else -1
    ref = float(window[0][1])
    high, low = max(float(b[2]) for b in window), min(float(b[3]) for b in window)
    up, down = (high / ref - 1), (1 - low / ref)
    favorable, adverse = (up, down) if sign > 0 else (down, up)
    scale = entry / distance                       # fraction of price -> R
    fav_r, adv_r = favorable * scale, adverse * scale
    if fav_r >= REVERSED_R and fav_r > adv_r:
        verdict = "too_tight"
    elif adv_r >= REVERSED_R and adv_r > fav_r:
        verdict = "right_exit"
    else:
        verdict = "neutral"
    return {"fav_r": round(fav_r, 2), "adv_r": round(adv_r, 2), "verdict": verdict}


def build_record(trade: dict, outcome: dict) -> dict:
    exit_price = outcome.get("exit_price")
    # the exit itself is an excursion sample (a stop can fill between two manage ticks)
    work = dict(trade)
    if exit_price:
        track(work, exit_price, outcome["closed_ms"])
    record = {"id": f"{trade['symbol']}-{trade['opened_ms']}", "symbol": trade["symbol"], "side": trade["side"],
              "opened_ms": trade["opened_ms"], "closed_ms": outcome["closed_ms"], "entry": trade["entry"], "exit": exit_price,
              "initial_stop": trade.get("initial_stop"), "take_profit": trade.get("take_profit"),
              "risk_distance": trade.get("risk_distance"), "leverage": trade.get("leverage"),
              "reason": outcome["reason"], "pnl": outcome.get("net_pnl"), "fees": outcome.get("fees"),
              "r": outcome.get("r_multiple") if outcome.get("net_pnl") is not None else None,
              "mfe_r": round(work.get("mfe_r", 0.0), 2), "mae_r": round(work.get("mae_r", 0.0), 2),
              "mfe_h": _hours(work.get("mfe_ms"), trade["opened_ms"]), "mae_h": _hours(work.get("mae_ms"), trade["opened_ms"]),
              "hold_h": round(max(0, outcome["closed_ms"] - trade["opened_ms"]) / HOUR_MS, 2),
              "breakeven": bool(trade.get("breakeven_done")), "entry_ctx": trade.get("snap"), "after": None}
    record["kind"] = diagnose(record)
    return record


def _hours(ms, opened_ms):
    return None if ms is None else round(max(0, ms - opened_ms) / HOUR_MS, 2)


def explain(record: dict) -> str:
    """One short line for the close message: how the trade moved (and why it ended)."""
    if record.get("r") is None:
        return ""
    text = f"↳ en iyi {record['mfe_r']:+.1f}R · en kötü {record['mae_r']:+.1f}R · {KIND_TEXT[record['kind']]}"
    return text


# ---- storage --------------------------------------------------------------------------------------------------
class Journal:
    def __init__(self, path: str | Path, clock=time.time):
        self.path, self._clock = Path(path), clock
        self.records: list[dict] = []
        self.last_error: str | None = None
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                record["id"], record["symbol"], record["closed_ms"]  # noqa: B018 - required keys
                self.records.append(record)
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable line %d in %s", number, self.path)

    def add(self, record: dict):
        self.records.append(record)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:     # a full disk must not stop the trading bot
            log.error("Could not write %s: %s", self.path, error)

    def _rewrite(self):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".journal-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.records))
            os.replace(tmp, self.path)
        except OSError as error:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            log.error("Could not rewrite %s: %s", self.path, error)

    def followups(self, market):
        """Fill in what the price did after each close (once FOLLOWUP_HOURS have passed). Failures are isolated."""
        now_ms = int(self._clock() * 1000)
        changed, errors = False, []
        for record in self.records:
            if record.get("after") is not None or record.get("kind") in ("win", "external", "unknown"):
                continue
            if now_ms < record["closed_ms"] + FOLLOWUP_HOURS * HOUR_MS + 5 * 60_000:
                continue
            if now_ms > record["closed_ms"] + 25 * HOUR_MS:       # too late: bars no longer reachable cheaply
                record["after"] = {"verdict": "none"}
                changed = True
                continue
            try:
                bars = market.klines(record["symbol"], FOLLOWUP_BAR, 288, start_ms=record["closed_ms"],
                                     end_ms=record["closed_ms"] + FOLLOWUP_HOURS * HOUR_MS)
                result = stop_verdict(record, bars)
            except Exception as error:
                errors.append(f"{record['symbol']}: {type(error).__name__}: {error}")
                continue
            if result:
                record["after"] = result
                changed = True
        if changed:
            self._rewrite()
        self.last_error = "; ".join(errors[:3]) if errors else None
        if errors:
            log.warning("Journal follow-up: %s", self.last_error)

    # ---- /analiz ----------------------------------------------------------------------------------------------
    def format_summary(self, last: int = 3) -> str:
        done = [r for r in self.records if r.get("r") is not None]
        if not done:
            return "📓 <b>İşlem günlüğü</b>\nHenüz kapanmış, günlüğe alınmış işlem yok. Bu özellik açıldıktan sonra kapanan işlemler birikir."
        wins = [r for r in done if r["r"] > 0]
        losses = [r for r in done if r["r"] <= 0]
        avg = sum(r["r"] for r in done) / len(done)
        lines = [f"📓 <b>İşlem günlüğü</b> · {len(done)} işlem · kazanma %{100 * len(wins) / len(done):.0f} · ortalama {avg:+.2f}R"]
        if losses:
            lines.append(f"Zarar eden {len(losses)}: ortalama {sum(r['r'] for r in losses) / len(losses):+.2f}R, "
                         f"en iyi anda ortalama {sum(r['mfe_r'] for r in losses) / len(losses):+.2f}R lehine gitmişti")
        lines.append("")
        lines.append("<b>İşlemler nasıl bitti</b>")
        for kind, text in KIND_TEXT.items():
            count = sum(r["kind"] == kind for r in done)
            if count:
                lines.append(f"• {count}× {text}")
        stops = [r for r in done if r["kind"] in ("never_worked", "faded", "gave_back")]
        judged = [r for r in stops if r.get("after") and r["after"].get("verdict") in STOP_TEXT]
        if judged:
            lines.append("")
            lines.append(f"<b>Stoptan sonra fiyat ({len(judged)} işlem, {FOLLOWUP_HOURS} saat)</b>")
            for verdict, text in STOP_TEXT.items():
                count = sum(r["after"]["verdict"] == verdict for r in judged)
                if count:
                    lines.append(f"• {count}× {text}")
        lines.append("")
        lines.append("<b>Yön / skor</b>")
        for side in ("LONG", "SHORT"):
            part = [r for r in done if r["side"] == side]
            if part:
                lines.append(f"• {side}: {len(part)} işlem, ortalama {sum(r['r'] for r in part) / len(part):+.2f}R")
        for low, high, label in ((0, 75, "skor <75"), (75, 80, "skor 75-80"), (80, 101, "skor ≥80")):
            part = [r for r in done if r.get("entry_ctx") and low <= r["entry_ctx"]["score"] < high]
            if part:
                lines.append(f"• {label}: {len(part)} işlem, ortalama {sum(r['r'] for r in part) / len(part):+.2f}R")
        lines.append("")
        lines.append(self._conclusion(done, stops, judged))
        lines.append("")
        lines.append("<b>Son işlemler</b>")
        for r in done[-last:][::-1]:
            when = datetime.fromtimestamp(r["closed_ms"] / 1000, timezone.utc).strftime("%d.%m %H:%M")
            lines.append(f"• {when} <code>{r['symbol']}</code> {r['side']} {r['r']:+.1f}R — {KIND_TEXT[r['kind']]} "
                         f"(en iyi {r['mfe_r']:+.1f}R / en kötü {r['mae_r']:+.1f}R)")
        return "\n".join(lines)

    @staticmethod
    def _conclusion(done: list[dict], stops: list[dict], judged: list[dict]) -> str:
        if len(done) < MIN_SAMPLE:
            return f"ℹ️ Örnek küçük ({len(done)}/{MIN_SAMPLE}): rakamlar bilgi amaçlı, bunlara bakıp ayar değiştirmeyin."
        notes = []
        if stops:
            never = sum(r["kind"] == "never_worked" for r in stops) / len(stops)
            if never >= 0.5:
                notes.append(f"Stop yiyenlerin %{100 * never:.0f}'i hiç lehine gitmedi: sorun stop mesafesi değil, giriş yönü/zamanı.")
        if len(judged) >= 10:
            tight = sum(r["after"]["verdict"] == "too_tight" for r in judged) / len(judged)
            right = sum(r["after"]["verdict"] == "right_exit" for r in judged) / len(judged)
            if tight >= 0.4:
                notes.append(f"Stopların %{100 * tight:.0f}'inden sonra fiyat lehimize döndü: stop çok dar olabilir.")
            elif right >= 0.4:
                notes.append(f"Stopların %{100 * right:.0f}'inden sonra fiyat aleyhe devam etti: stop işini yapıyor, genişletmek zarar büyütür.")
        if not notes:
            notes.append("Belirgin tek bir neden öne çıkmıyor.")
        return "💡 " + " ".join(notes)
