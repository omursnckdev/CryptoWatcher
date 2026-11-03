"""Exit-rule shadow test: would a different EXIT have done better on the very same signals?

The live exit is: stop 1R (= atr_stop_multiplier x ATR), target tp_r x R, stop to breakeven after +breakeven_r, time stop after
max_hold_hours. Three alternatives from the claude-trading-skills `exit-strategies` skill are replayed next to it, on the same real
mainnet 5-minute candles, for every signal the signal log has already resolved (opened and skipped alike). Nothing here places an
order or sends a Telegram message; a failure is logged and never reaches the trading loop.

The comparison is PAIRED: the same signal, the same bars, the same cost model, only the exit differs. The baseline is replayed with
the generic engine below and must equal the signal log's own replay (signals.simulate); a mismatch is counted and shown.

The variants and the decision rule are FROZEN (2026-11-03). Changing them after seeing results would void the test.
"""
from datetime import datetime, timezone
import json
import logging
import math
import time
from pathlib import Path
from .signals import BAR, BAR_MS, HOUR_MS, SLIPPAGE, TAKEN

log = logging.getLogger(__name__)

DAY_MS = 24 * HOUR_MS
RETRY_MS = 30 * 60_000         # re-check an undecided signal at most this often
BATCH = 25                     # signals replayed per call (the first run may face a backlog)
BAR_LIMIT = 1300               # 5-minute bars per request: covers the longest variant (2 x 48 h) plus margin; Binance allows 1500
# ---- frozen variants (2026-11-03) ------------------------------------------------------------------------------------------
# levels and distances in R (1R = the live stop distance = 3 x ATR with the default settings)
VARIANTS = {
    "scaled": {"label": "Kademeli", "desc": "⅓ 2R, ⅓ 3R, son ⅓ zirvenin 1R gerisinden takip",
               "tranches": ((2.0, 1 / 3), (3.0, 1 / 3)), "trail_after_r": 2.0, "trail_r": 1.0, "extend": False},
    "trail": {"label": "Takip", "desc": "sabit hedef yok, 2R'den sonra zirvenin 1R gerisi",
              "tranches": (), "trail_after_r": 2.0, "trail_r": 1.0, "extend": False},
    "time_plus": {"label": "Zaman+", "desc": "mevcut kural, ama süre dolunca kârdaysa 2 katı süreye kadar tut",
                  "tranches": None, "trail_after_r": None, "trail_r": None, "extend": True},
}
# ---- pre-registered decision rule ---------------------------------------------------------------------------------------------
# A variant is recommended only if its paired difference to the live exit is (1) at least +0.03R per signal, (2) day-clustered
# t >= 2.0 (stricter than the single-hypothesis 1.65: three variants are tested), (3) positive in both halves of the period, after
# (4) >= 150 resolved signals spanning >= 90 days. If several pass, the one with the largest mean difference is named.
DECISION = {"min_signals": 150, "min_days": 90, "min_t": 2.0, "min_mean_r": 0.03}


def replay(side: str, distance_frac: float, bars: list, *, tp_r: float, breakeven_r: float, hold_hours: float, fee: float,
           tranches=None, trail_after_r=None, trail_r=None, extend=False) -> dict | None:
    """Replay one trade with partial exits / trailing stop on 5-minute bars [open_time, open, high, low, close, ...].

    Conventions are those of signals.simulate so that the default arguments reproduce it exactly: entry = open of the first bar;
    inside a bar the stop is checked before any target (a bar touching both counts as stopped); breakeven and the trailing stop
    take effect from the bar AFTER the one that reached their trigger; a gap through the stop fills at the bar's open.
    `tranches` = ((level_r, fraction), ...) closed at their levels; whatever is left after them exits only by stop/trail/time.
    None means the live single target (tp_r, 100 %). `extend`: when the hold time is up, a position in profit is kept (same rules)
    until twice the hold time. Returns None while the bars do not decide the outcome yet."""
    if not bars or not distance_frac > 0:
        return None
    sign = 1 if side == "LONG" else -1
    entry = float(bars[0][1])
    distance = distance_frac * entry
    levels = [(tp_r, 1.0)] if tranches is None else list(tranches)
    stop = entry - sign * distance
    be_stop = entry * (1 + sign * 2 * fee)
    left, gross, filled = 1.0, 0.0, [False] * len(levels)
    be_armed = trail_armed = False
    best = entry
    start, hold_ms = int(bars[0][0]), hold_hours * HOUR_MS
    limit_ms = 2 * hold_ms if extend else hold_ms
    reason = None
    for bar in bars:
        opened, o, high, low, close = int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        worst, top = (low, high) if sign > 0 else (high, low)
        if sign * (worst - stop) <= 0:                                   # stop / breakeven / trail: the rest leaves here
            price = stop if sign * (o - stop) > 0 else o
            gross += left * sign * (price - entry) / distance
            left, reason = 0.0, ("TRAIL" if trail_armed and sign * (stop - be_stop) > 0 else "BE" if be_armed else "SL")
            break
        for i, (level, fraction) in enumerate(levels):
            if not filled[i] and sign * (top - (entry + sign * level * distance)) >= 0:
                part = min(fraction, left)
                gross += part * level
                left -= part
                filled[i] = True
                reason = "TP"
        if left <= 1e-9:
            left = 0.0
            break
        best = max(best, top) if sign > 0 else min(best, top)           # effective from the next bar
        if breakeven_r and not be_armed and sign * (top - entry) >= breakeven_r * distance:
            be_armed = True
            stop = be_stop if sign * (be_stop - stop) > 0 else stop
        if trail_after_r is not None and sign * (best - entry) >= trail_after_r * distance:
            trail_armed = True
            candidate = best - sign * trail_r * distance
            if sign * (candidate - stop) > 0:
                stop = candidate
        elapsed = opened + BAR_MS - start
        if hold_hours and elapsed >= hold_ms:
            keep = extend and elapsed < limit_ms and sign * (close - entry) > 0
            if not keep:
                gross += left * sign * (close - entry) / distance
                left, reason = 0.0, "TIME"
                break
    if left > 0:
        return None
    cost = 2 * (fee + SLIPPAGE) * entry / distance
    return {"gross_r": round(gross, 3), "net_r": round(gross - cost, 3), "exit": reason}


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def paired_stats(rows: list[dict], key: str) -> dict:
    """Mean of (variant - live) per signal and its t with one observation per day (signals of a day move together)."""
    diffs = [(r["seen_ms"], r[key] - r["base"]) for r in rows]
    if not diffs:
        return {"n": 0, "mean": float("nan"), "t": float("nan")}
    by_day: dict[str, list[float]] = {}
    for ms, d in diffs:
        by_day.setdefault(_day(ms), []).append(d)
    daily = [sum(v) / len(v) for v in by_day.values()]
    mean = sum(d for _, d in diffs) / len(diffs)
    t = float("nan")
    if len(daily) >= 6:
        m = sum(daily) / len(daily)
        var = sum((x - m) ** 2 for x in daily) / (len(daily) - 1)
        if var > 0:
            t = m / math.sqrt(var / len(daily))
    return {"n": len(diffs), "mean": mean, "t": t}


class ExitLab:
    def __init__(self, path: str | Path, market, settings, signal_log, clock=time.time):
        self.path, self.market, self.s, self.signal_log, self._clock = Path(path), market, settings, signal_log, clock
        self.results: dict[str, dict] = {}
        self.start_ms: int | None = None
        self.last_error: str | None = None
        self._tried: dict[str, int] = {}
        self._load()
        if self.start_ms is None:
            self.start_ms = int(self._clock() * 1000)
            self._append({"k": "start", "ms": self.start_ms})

    # ---- persistence -------------------------------------------------------------------------------------------------------
    def _load(self):
        if not self.path.is_file():
            return
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                if record["k"] == "start" and self.start_ms is None:
                    self.start_ms = int(record["ms"])
                elif record["k"] == "res":
                    self.results[record["id"]] = record
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable line %d in %s", number, self.path)

    def _append(self, record: dict):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:      # a full disk must not stop the trading bot
            log.error("Could not write %s: %s", self.path, error)

    # ---- replay ------------------------------------------------------------------------------------------------------------
    def _rules(self) -> dict:
        return {"tp_r": self.s.tp_r, "breakeven_r": self.s.breakeven_r, "hold_hours": self.s.max_hold_hours, "fee": self.s.taker_fee}

    def pending(self) -> list[tuple[str, dict]]:
        """Signals the signal log has resolved (so their entry time is final) and that this lab has not."""
        done = self.signal_log.results
        rows = [(k, s) for k, s in self.signal_log.signals.items()
                if k not in self.results and done.get(k, {}).get("net_r") is not None]
        return sorted(rows, key=lambda kv: kv[1]["seen_ms"])

    def resolve(self):
        now_ms = int(self._clock() * 1000)
        longest_ms = int(2 * self.s.max_hold_hours * HOUR_MS)
        errors, used = [], 0
        for key, signal in self.pending():
            if used >= BATCH:
                break
            if now_ms - self._tried.get(key, 0) < RETRY_MS:
                continue
            self._tried[key] = now_ms
            used += 1
            try:
                bars = self.market.klines(signal["symbol"], BAR, BAR_LIMIT, start_ms=signal["seen_ms"], end_ms=now_ms)
                bars = [b for b in bars if int(b[0]) + BAR_MS <= now_ms]     # closed bars only
                outcomes = self._replay_all(signal, bars)
            except Exception as error:
                errors.append(f"{signal['symbol']}: {type(error).__name__}: {error}")
                continue
            if outcomes is not None:
                self._finish(key, signal, outcomes)
            elif now_ms > signal["seen_ms"] + longest_ms + DAY_MS:      # old enough and the bars still do not decide: give up
                self._finish(key, signal, None)
        self.last_error = "; ".join(errors[:3]) if errors else None
        if errors:
            log.warning("Exit lab: %s", self.last_error)

    def _replay_all(self, signal: dict, bars: list) -> dict | None:
        rules = self._rules()
        args = (signal["side"], signal["distance_frac"], bars)
        base = replay(*args, **rules)
        out = {"base": base}
        for name, v in VARIANTS.items():
            if v["tranches"] is None:
                out[name] = replay(*args, **rules, extend=v["extend"])
            else:
                out[name] = replay(*args, **rules, tranches=v["tranches"], trail_after_r=v["trail_after_r"],
                                   trail_r=v["trail_r"], extend=v["extend"])
        if any(o is None for o in out.values()):
            return None
        return out

    def _finish(self, key: str, signal: dict, outcomes: dict | None):
        record = {"k": "res", "id": key, "seen_ms": signal["seen_ms"], "status": signal.get("status")}
        if outcomes is None:
            record["expired"] = True
        else:
            record.update({name: o["net_r"] for name, o in outcomes.items()})
            record["exits"] = {name: o["exit"] for name, o in outcomes.items()}
            logged = self.signal_log.results.get(key, {}).get("net_r")
            record["base_matches_log"] = logged is not None and abs(logged - outcomes["base"]["net_r"]) < 0.002
        self.results[key] = record
        self._append(record)

    # ---- /cikis -------------------------------------------------------------------------------------------------------------
    def rows(self) -> list[dict]:
        return [r for r in self.results.values() if not r.get("expired") and r.get("base") is not None]

    def verdict(self, rows: list[dict]) -> str:
        rule = DECISION
        if not rows:
            return "henüz sonuçlanan sinyal yok"
        span = (max(r["seen_ms"] for r in rows) - min(r["seen_ms"] for r in rows)) / DAY_MS
        if len(rows) < rule["min_signals"] or span < rule["min_days"]:
            return f"henüz erken ({len(rows)}/{rule['min_signals']} sinyal, {span:.0f}/{rule['min_days']} gün)"
        mid = min(r["seen_ms"] for r in rows) + (max(r["seen_ms"] for r in rows) - min(r["seen_ms"] for r in rows)) / 2
        halves = ([r for r in rows if r["seen_ms"] < mid], [r for r in rows if r["seen_ms"] >= mid])
        passed = []
        for name in VARIANTS:
            s = paired_stats(rows, name)
            ok_halves = all(paired_stats(h, name)["mean"] > 0 for h in halves if h)
            if s["mean"] >= rule["min_mean_r"] and s["t"] == s["t"] and s["t"] >= rule["min_t"] and ok_halves:
                passed.append((s["mean"], name))
        if not passed:
            return "GEÇEN YOK: mevcut çıkış kuralı kalmalı"
        best = max(passed)[1]
        return f"GEÇTİ: «{VARIANTS[best]['label']}» çıkışı önerilir (canlıya almadan önce birlikte bakalım)"

    def format_summary(self) -> str:
        rows = self.rows()
        pending = len(self.pending())
        started = datetime.fromtimestamp(self.start_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        lines = [f"🚪 <b>Çıkış kuralı testi</b> (gölge, işlemleri etkilemez) · başlangıç {started}",
                 f"{len(rows)} sinyal sonuçlandı ({sum(r.get('status') == TAKEN for r in rows)} açılan), {pending} bekliyor"]
        if not rows:
            lines.append("Sinyaller önce /atlanan simülasyonunda sonuçlanır, sonra burada üç çıkış kuralıyla tekrar oynatılır.")
            if self.last_error:
                lines.append("⚠️ Son sorun: " + self.last_error[:220].replace("<", "‹"))
            return "\n".join(lines)
        base = [r["base"] for r in rows]
        lines.append(f"• Mevcut ({self.s.tp_r:g}R hedef, +{self.s.breakeven_r:g}R başabaş, {self.s.max_hold_hours:g}sa): "
                     f"ort <b>{sum(base) / len(base):+.2f}R</b> · kazanma %{100 * sum(b > 0 for b in base) / len(base):.0f}")
        for name, v in VARIANTS.items():
            values = [r[name] for r in rows]
            s = paired_stats(rows, name)
            t = f"{s['t']:+.1f}" if s["t"] == s["t"] else "—"
            lines.append(f"• {v['label']} ({v['desc']}): ort {sum(values) / len(values):+.2f}R · "
                         f"kazanma %{100 * sum(x > 0 for x in values) / len(values):.0f} · fark <b>{s['mean']:+.2f}R</b> (t {t})")
        mismatch = sum(not r.get("base_matches_log", True) for r in rows)
        if mismatch:
            lines.append(f"⚠️ {mismatch} sinyalde mevcut kuralın tekrarı /atlanan sonucundan farklı çıktı (kontrol edilmeli).")
        lines += [f"Karar: {self.verdict(rows)}",
                  f"Kural: ≥{DECISION['min_signals']} sinyal, ≥{DECISION['min_days']} gün; fark ≥ +{DECISION['min_mean_r']:.2f}R, "
                  f"gün-kümeli t ≥ {DECISION['min_t']:g} ve iki yarıda da pozitif."]
        if self.last_error:
            lines.append("⚠️ Son sorun: " + self.last_error[:220].replace("<", "‹"))
        return "\n".join(lines)
