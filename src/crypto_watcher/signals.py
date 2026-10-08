"""Signal log: what would the trades we did NOT open (position limits, cooldown) have done?

Every actionable signal is recorded, whether the bot opened it or a limit blocked it. Without placing any order, each signal is
replayed on real mainnet 5-minute candles with the bot's own rules (ATR stop, take-profit, breakeven, time stop, fees + slippage)
until it would have been closed. Opened and skipped signals use the same replay, so the two groups are comparable, and the
replay of opened signals is checked against what the real trades returned.

No orders, no Telegram pushes; failures are logged and never reach the trading loop.
"""
from datetime import datetime, timezone
import json
import logging
import math
import time
from pathlib import Path

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
BAR = "5m"
BAR_MS = 300_000
SLIPPAGE = 0.0002              # per side, in addition to the taker fee
RETRY_MS = 30 * 60_000         # re-check an unresolved signal at most this often
EXPIRE_MS = 24 * HOUR_MS       # give up this long after the holding period should have ended
MIN_SKIPPED, MIN_DAYS = 30, 10     # below this the summary shows numbers but draws no conclusion

# executor.can_open reason -> short Turkish label; reasons not listed are not alternative trades and are not recorded
SKIP_LABELS = {"max same-direction": "yön limiti", "max open positions": "toplam pozisyon limiti", "cooldown": "bekleme süresi",
               "daily loss limit reached": "günlük zarar kilidi", "paused": "duraklatıldı"}
NOT_OPENED = "emir/bütçe nedeniyle açılmadı"
TAKEN = "açıldı"


def skip_label(reason: str) -> str | None:
    for key, label in SKIP_LABELS.items():
        if reason.startswith(key):
            return label
    return None


def simulate(side: str, distance_frac: float, bars: list, *, tp_r: float, breakeven_r: float, hold_hours: float, fee: float) -> dict | None:
    """Replay one trade on 5-minute bars [open_time, open, high, low, close, ...]; entry = open of the first bar.

    Ambiguous bars are resolved against the trade where the order inside a bar is unknowable: a bar that touches both stop and
    target counts as stopped. Breakeven protection starts with the bar AFTER the one that reached +breakeven_r. Returns None while
    the bars do not yet decide the outcome."""
    if not bars or not distance_frac > 0:
        return None
    sign = 1 if side == "LONG" else -1
    entry = float(bars[0][1])
    distance = distance_frac * entry
    stop, take = entry - sign * distance, entry + sign * tp_r * distance
    be_stop = entry * (1 + sign * 2 * fee)
    armed, mfe, mae = False, 0.0, 0.0
    start, hold_ms = int(bars[0][0]), hold_hours * HOUR_MS
    exit_price = reason = None
    for bar in bars:
        opened, o, high, low, close = int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        worst, best = (low, high) if sign > 0 else (high, low)
        mfe, mae = max(mfe, sign * (best - entry) / distance), min(mae, sign * (worst - entry) / distance)
        if sign * (worst - stop) <= 0:
            exit_price = stop if sign * (o - stop) > 0 else o          # a gap through the stop fills at the open
            reason = "BE" if armed else "SL"
            break
        if sign * (best - take) >= 0:
            exit_price, reason = take, "TP"
            break
        if breakeven_r and not armed and sign * (best - entry) >= breakeven_r * distance:
            armed, stop = True, be_stop
        if hold_hours and opened + BAR_MS - start >= hold_ms:
            exit_price, reason = close, "TIME"
            break
    if exit_price is None:
        return None
    gross = sign * (exit_price - entry) / distance
    cost = 2 * (fee + SLIPPAGE) * entry / distance
    return {"gross_r": round(gross, 3), "net_r": round(gross - cost, 3), "exit": reason, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2)}


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _t_stat(rows: list[dict]) -> float:
    """t of the mean net R, using one observation per day (signals of the same day move together)."""
    by_day: dict[str, list[float]] = {}
    for row in rows:
        by_day.setdefault(_day(row["seen_ms"]), []).append(row["net_r"])
    daily = [sum(v) / len(v) for v in by_day.values()]
    if len(daily) < 6:
        return float("nan")
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
    return float("nan") if var == 0 else mean / math.sqrt(var / len(daily))


class SignalLog:
    def __init__(self, path: str | Path, market, settings, clock=time.time):
        self.path, self.market, self.s, self._clock = Path(path), market, settings, clock
        self.signals: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.last_error: str | None = None
        self._tried: dict[str, int] = {}
        self._load()

    # ---- persistence ------------------------------------------------------------------------------------
    def _load(self):
        if not self.path.is_file():
            return
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                kind, key = record["k"], record["id"]
                if kind == "sig":
                    self.signals[key] = record
                elif kind == "upd":
                    self.signals[key].update({k: v for k, v in record.items() if k not in ("k", "id")})
                elif kind == "res":
                    self.results[key] = record
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable line %d in %s", number, self.path)

    def _append(self, record: dict):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:      # a full disk must not stop the trading bot
            log.error("Could not write %s: %s", self.path, error)

    # ---- recording ----------------------------------------------------------------------------------------
    def note(self, candidate: dict, status: str, trade: dict | None = None):
        """`status`: TAKEN or a SKIP_LABELS label / NOT_OPENED. Repeated scans of the same candle are one signal."""
        side = candidate["side"]
        plan = candidate["sides"][side].get("risk_plan")
        if not plan:
            return
        key, now_ms = f"{candidate['symbol']}-{candidate['date']}", int(self._clock() * 1000)
        existing = self.signals.get(key)
        trade_id = f"{trade['symbol']}-{trade['opened_ms']}" if trade else None
        if existing and (existing["status"] == TAKEN or status != TAKEN):
            return
        if existing:        # blocked earlier in this candle, opened now (a slot freed up): replay from the real entry time
            update = {"status": status, "seen_ms": now_ms, "trade_id": trade_id}
            existing.update(update)
            self._append({"k": "upd", "id": key, **update})
            return
        record = {"k": "sig", "id": key, "symbol": candidate["symbol"], "side": side, "date": candidate["date"], "seen_ms": now_ms,
                  "distance_frac": plan["risk_distance"] / plan["entry"], "score": round(float(candidate["score"]), 1),
                  "regime": candidate.get("market_regime"), "status": status, "trade_id": trade_id}
        self.signals[key] = record
        self._append(record)

    # ---- replay ---------------------------------------------------------------------------------------------
    def resolve(self):
        now_ms = int(self._clock() * 1000)
        hold_ms = int(self.s.max_hold_hours * HOUR_MS)
        errors = []
        for key, signal in self.signals.items():
            if key in self.results:
                continue
            if now_ms > signal["seen_ms"] + hold_ms + EXPIRE_MS:
                self._finish(key, {"net_r": None, "exit": "EXPIRED"})
                continue
            if now_ms - self._tried.get(key, 0) < RETRY_MS or now_ms < signal["seen_ms"] + BAR_MS * 3:
                continue
            self._tried[key] = now_ms
            try:
                bars = self.market.klines(signal["symbol"], BAR, 700, start_ms=signal["seen_ms"], end_ms=now_ms)
                bars = [b for b in bars if int(b[0]) + BAR_MS <= now_ms]          # closed bars only
                outcome = simulate(signal["side"], signal["distance_frac"], bars, tp_r=self.s.tp_r, breakeven_r=self.s.breakeven_r,
                                   hold_hours=self.s.max_hold_hours, fee=self.s.taker_fee)
            except Exception as error:
                errors.append(f"{signal['symbol']}: {type(error).__name__}: {error}")
                continue
            if outcome:
                self._finish(key, outcome)
        self.last_error = "; ".join(errors[:3]) if errors else None
        if errors:
            log.warning("Signal log: %s", self.last_error)

    def _finish(self, key: str, outcome: dict):
        self.results[key] = {"k": "res", "id": key, **outcome}
        self._append(self.results[key])

    # ---- /atlanan ----------------------------------------------------------------------------------------------
    def rows(self) -> list[dict]:
        return [{**s, **self.results[k]} for k, s in self.signals.items() if k in self.results and self.results[k].get("net_r") is not None]

    def format_summary(self, journal=None) -> str:
        rows = self.rows()
        pending = len(self.signals) - len(self.results)
        lines = [f"🧪 <b>Atlanan sinyaller (simülasyon)</b> · {len(self.signals)} sinyal, {len(rows)} sonuçlandı, {pending} bekliyor"]
        if not rows:
            lines.append("Henüz sonuçlanmış sinyal yok: bir işlemin simülasyonu en fazla 48 saat sürer.")
            return "\n".join(lines)
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(row["status"], []).append(row)
        order = [TAKEN] + [label for label in list(SKIP_LABELS.values()) + [NOT_OPENED] if label in groups]
        for name in order:
            if name in groups:
                lines.append(self._line(name, groups[name]))
        check = self._check_against_real(groups.get(TAKEN, []), journal)
        if check:
            lines += ["", check]
        lines += ["", self._conclusion(groups)]
        return "\n".join(lines)

    @staticmethod
    def _line(name: str, rows: list[dict]) -> str:
        mean = sum(r["net_r"] for r in rows) / len(rows)
        wins = sum(r["net_r"] > 0 for r in rows)
        days = len({_day(r["seen_ms"]) for r in rows})
        return f"• {name}: {len(rows)} sinyal ({days} gün) · kazanma %{100 * wins / len(rows):.0f} · ortalama {mean:+.2f}R"

    @staticmethod
    def _check_against_real(taken: list[dict], journal) -> str:
        """The replay must resemble what the real trades returned, otherwise its verdict on skipped signals means little."""
        if journal is None:
            return ""
        real = {r["id"]: r["r"] for r in journal.records if r.get("r") is not None}
        pairs = [(row["net_r"], real[row["trade_id"]]) for row in taken if row.get("trade_id") in real]
        if len(pairs) < 5:
            return ""
        sim, act = (sum(p[i] for p in pairs) / len(pairs) for i in (0, 1))
        return f"🔍 Doğrulama: açılan {len(pairs)} işlemde simülasyon ortalaması {sim:+.2f}R, gerçek {act:+.2f}R."

    @staticmethod
    def _conclusion(groups: dict[str, list[dict]]) -> str:
        skipped = groups.get("yön limiti", [])
        taken = groups.get(TAKEN, [])
        days = len({_day(r["seen_ms"]) for r in skipped})
        if len(skipped) < MIN_SKIPPED or days < MIN_DAYS:
            return (f"ℹ️ Yön limitinden atlanan {len(skipped)}/{MIN_SKIPPED} sinyal, {days}/{MIN_DAYS} gün: "
                    "henüz yorum için erken, rakamlara bakıp limiti değiştirmeyin.")
        mean = sum(r["net_r"] for r in skipped) / len(skipped)
        t = _t_stat(skipped)
        if mean <= 0:
            return (f"💡 Yön limiti ile atlanan sinyaller ortalama {mean:+.2f}R getirirdi: limit zarar eden işlemleri engelliyor. "
                    "Gevşetmek için bir neden yok.")
        if not t >= 1.65:
            return (f"💡 Atlananlar ortalama {mean:+.2f}R ama bu, şansla açıklanabilir (gün-kümeli t={t:.1f}). "
                    "Limiti gevşetmeye yetecek kanıt yok.")
        better = not taken or mean > sum(r["net_r"] for r in taken) / len(taken)
        return (f"💡 Atlananlar ortalama {mean:+.2f}R (gün-kümeli t={t:.1f}){', açılanlardan iyi' if better else ''}: limit fırsat "
                "kaçırtıyor olabilir. Bu bir işaret, kanıt değil; yine de limiti gevşetmeden önce birlikte bakalım.")
