"""Shadow recording of ONE frozen hypothesis: "open interest collapsed over 24h while price fell -> LONG".

No orders and no Telegram pushes. Every hour the bot checks the universe; when the pattern appears the event is written to
`shadow.jsonl`, and once the holding periods (8h / 24h / 48h) are over the outcome is computed from real candles and funding.
The point is honest forward evidence: the signal looked strong in 2023-mid 2025 (+50 bps net at 24h, t=2.9) and faded afterwards
(+3 bps, t=0.9) in a historical study. Whether it is alive now is answered by data nobody has seen yet.

The definition below is FROZEN. Changing it after seeing forward results would void the test.
"""
from datetime import datetime, timezone
import html
import json
import logging
import math
import time
from pathlib import Path
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
SIGNAL = "OI çöküşü + fiyat düştü → LONG"
# ---- frozen definition (2026-10-08) ---------------------------------------------------------------------------
OI_PERIOD, OI_LIMIT = "1h", 500        # Binance open-interest statistics, hourly, ~20 days
QUANTILE, WINDOW, MIN_PERIODS = 0.05, 480, 288   # 24h OI change below its own past 5th percentile (20-day window)
HORIZONS = (8, 24, 48)                 # hours held; entry is the open of the hour AFTER detection
COST_BPS = 14.0                        # round trip: 2 x 5 bps fee + 2 x 2 bps slippage
# ---- pre-registered decision rule for the forward test --------------------------------------------------------
DECISION = {"min_events": 150, "min_days": 90, "min_mean_bps": 15.0, "min_t": 1.65, "min_positive_months": 0.6}
HISTORY = {"train_24h": "+51", "test_24h": "+3"}      # from the historical study, for context only


def _day_clustered_t(days: pd.Series, values: pd.Series) -> float:
    daily = values.groupby(days).mean()
    if len(daily) < 6 or daily.std(ddof=1) == 0:
        return float("nan")
    return float(daily.mean() / (daily.std(ddof=1) / math.sqrt(len(daily))))


class ShadowRecorder:
    def __init__(self, market, provider, path: str | Path, clock=time.time):
        self.market, self.provider, self.path, self._clock = market, provider, Path(path), clock
        self.events: dict[str, dict] = {}
        self.outcomes: dict[tuple[str, int], dict] = {}
        self.start_ms: int | None = None
        self.last_error: str | None = None
        self._checked: set[tuple[str, int]] = set()
        self._load()
        if self.start_ms is None:
            self.start_ms = int(self._clock() * 1000)
            self._append({"k": "start", "ms": self.start_ms})

    # ---- persistence ---------------------------------------------------------------------------------------
    def _load(self):
        if not self.path.is_file():
            return
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                kind = record["k"]
                if kind == "start" and self.start_ms is None:
                    self.start_ms = int(record["ms"])
                elif kind == "event":
                    self.events[record["id"]] = record
                elif kind == "outcome":
                    self.outcomes[(record["id"], int(record["H"]))] = record
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable line %d in %s", number, self.path)

    def _append(self, record: dict):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:      # a full disk must not stop the trading bot
            log.error("Could not write %s: %s", self.path, error)

    # ---- detection ------------------------------------------------------------------------------------------
    def tick(self, symbols: list[str]):
        now_ms = int(self._clock() * 1000)
        hour = now_ms // HOUR_MS * HOUR_MS
        self._checked = {key for key in self._checked if key[1] == hour}
        errors = []
        for symbol in symbols:
            if (symbol, hour) in self._checked:
                continue
            try:
                self._detect(symbol, hour)
                self._checked.add((symbol, hour))
            except Exception as error:      # one symbol's problem must not hide the others
                errors.append(f"{symbol}: {type(error).__name__}: {error}")
        if errors:
            self.last_error = "; ".join(errors[:3]) + (f" (+{len(errors) - 3} more)" if len(errors) > 3 else "")
            log.warning("Shadow recorder: %s", self.last_error)
        else:
            self.last_error = None
        try:
            self._resolve(now_ms)
        except Exception as error:
            self.last_error = f"outcome resolution: {type(error).__name__}: {error}"
            log.warning("Shadow recorder: %s", self.last_error)

    def _detect(self, symbol: str, hour: int):
        if f"{symbol}-{hour}" in self.events:
            return
        rows = self.market.open_interest_hist(symbol, OI_PERIOD, OI_LIMIT)
        try:
            series = pd.Series({int(r["timestamp"]): float(r["sumOpenInterestValue"]) for r in rows}).sort_index()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"unexpected open-interest response shape ({error!r}); first row: {str(rows[:1])[:200]}") from error
        if len(series) < 24 + MIN_PERIODS + 1:
            raise ValueError(f"only {len(series)} open-interest points")
        if int(self._clock() * 1000) - int(series.index[-1]) > 2 * HOUR_MS:
            raise ValueError("open-interest data is stale")
        if (np.diff(series.index.to_numpy()[-25:]) != HOUR_MS).any():
            raise ValueError("gaps in the last 24h of open-interest data")
        change = series / series.shift(24) - 1
        threshold = change.shift(1).rolling(WINDOW, min_periods=MIN_PERIODS).quantile(QUANTILE)
        current, limit = float(change.iloc[-1]), float(threshold.iloc[-1])
        if not (math.isfinite(current) and math.isfinite(limit)) or current >= limit:
            return
        candles = self.provider.klines(symbol, "1h", 300)
        last_open = int((candles.index[-1] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1))
        if last_open != hour - HOUR_MS or len(candles) < 26:
            raise ValueError("price candles are not up to date")
        ret24 = math.log(float(candles.close.iloc[-1]) / float(candles.close.iloc[-25]))
        if ret24 >= 0:
            return
        record = {"k": "event", "id": f"{symbol}-{hour}", "symbol": symbol, "hour": hour, "oi24": current, "threshold": limit,
                  "ret24": ret24, "price": float(candles.close.iloc[-1]), "oi_value": float(series.iloc[-1])}
        self.events[record["id"]] = record
        self._append(record)
        log.info("SHADOW event %s: OI24 %.1f%% (limit %.1f%%), price24 %.1f%%", record["id"], 100 * current, 100 * limit, 100 * ret24)

    # ---- outcomes -----------------------------------------------------------------------------------------------
    def _resolve(self, now_ms: int):
        for event in sorted(self.events.values(), key=lambda e: e["hour"]):
            for horizon in HORIZONS:
                key = (event["id"], horizon)
                entry_ms = event["hour"] + HOUR_MS
                exit_ms = entry_ms + horizon * HOUR_MS
                if key in self.outcomes or now_ms < exit_ms + HOUR_MS + 60_000:     # the exit candle must have closed
                    continue
                candles = self.provider.klines(event["symbol"], "1h", 300)
                opens = candles.open
                entry_t, exit_t = (pd.Timestamp(ms, unit="ms", tz="UTC") for ms in (entry_ms, exit_ms))
                if entry_t not in opens.index or exit_t not in opens.index:
                    if now_ms - exit_ms > 250 * HOUR_MS:      # candles scrolled out of reach (bot was down for long)
                        self._store_outcome(event, horizon, {"lost": True})
                    continue
                funding = self.market.funding_rates(event["symbol"], entry_ms, exit_ms - 1)
                paid = sum(float(r["fundingRate"]) for r in funding if entry_ms <= int(r["fundingTime"]) < exit_ms)
                gross = math.log(float(opens[exit_t]) / float(opens[entry_t])) * 1e4
                fund = -paid * 1e4                                   # a long pays positive funding
                self._store_outcome(event, horizon, {"gross": gross, "fund": fund, "net": gross + fund - COST_BPS})

    def _store_outcome(self, event: dict, horizon: int, values: dict):
        record = {"k": "outcome", "id": event["id"], "H": horizon, **values}
        self.outcomes[(event["id"], horizon)] = record
        self._append(record)

    # ---- analysis --------------------------------------------------------------------------------------------------
    def non_overlapping(self, horizon: int) -> list[dict]:
        """Same rule as the historical study: one position per symbol at a time."""
        free, chosen = {}, []
        for event in sorted(self.events.values(), key=lambda e: e["hour"]):
            if event["hour"] < free.get(event["symbol"], -1):
                continue
            chosen.append(event)
            free[event["symbol"]] = event["hour"] + (1 + horizon) * HOUR_MS
        return chosen

    def stats(self, horizon: int) -> dict:
        rows = []
        for event in self.non_overlapping(horizon):
            outcome = self.outcomes.get((event["id"], horizon))
            if outcome and "net" in outcome:
                rows.append((event["hour"], outcome["net"]))
        if not rows:
            return {"n": 0}
        frame = pd.DataFrame(rows, columns=["hour", "net"])
        when = pd.to_datetime(frame.hour, unit="ms", utc=True)
        months = frame.net.groupby(when.dt.strftime("%Y-%m")).agg(["size", "mean"])
        months = months[months["size"] >= 3]
        return {"n": len(frame), "mean": float(frame.net.mean()), "win": float((frame.net > 0).mean()),
                "t": _day_clustered_t(when.dt.floor("D"), frame.net),
                "months_positive": float((months["mean"] > 0).mean()) if len(months) else float("nan"), "months": len(months)}

    def verdict(self, now_ms: int | None = None) -> str:
        now_ms = now_ms or int(self._clock() * 1000)
        s, days = self.stats(24), (now_ms - self.start_ms) / DAY_MS
        rule = DECISION
        if s["n"] < rule["min_events"] or days < rule["min_days"]:
            return f"yetersiz veri ({s['n']}/{rule['min_events']} olay, {days:.0f}/{rule['min_days']} gün)"
        passed = (s["mean"] >= rule["min_mean_bps"] and s["t"] >= rule["min_t"]
                  and s["months_positive"] >= rule["min_positive_months"])
        return "KURALI GEÇTİ: bir sonraki aşamaya (kâğıt üzerinde işlem) aday" if passed else "KURALI GEÇMEDİ: sinyal rafa kaldırılmalı"

    def format_summary(self) -> str:
        now_ms = int(self._clock() * 1000)
        days = (now_ms - self.start_ms) / DAY_MS
        started = datetime.fromtimestamp(self.start_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        lines = [f"🕶 <b>Gölge kayıt</b> · {html.escape(SIGNAL)} (işlem açılmıyor)",
                 f"Başlangıç {started} · {days:.0f} gün · {len(self.events)} ham olay"]
        for horizon in HORIZONS:
            s = self.stats(horizon)
            if not s["n"]:
                lines.append(f"{horizon:>2}sa: henüz sonuçlanan olay yok")
                continue
            months = f" · ay+ {s['months_positive'] * 100:.0f}%" if s["months"] else ""
            t = f"{s['t']:+.1f}" if s["t"] == s["t"] else "—"
            lines.append(f"{horizon:>2}sa: n={s['n']} · ort <b>{s['mean']:+.0f}</b> bp · kazanma {s['win'] * 100:.0f}% · t {t}{months}")
        lines += [f"Geçmiş çalışma (24sa): eğitim {HISTORY['train_24h']} bp, test {HISTORY['test_24h']} bp",
                  f"Karar: {self.verdict(now_ms)}",
                  f"Kural: ≥{DECISION['min_events']} olay, ≥{DECISION['min_days']} gün, ort ≥ +{DECISION['min_mean_bps']:.0f} bp, "
                  f"t ≥ {DECISION['min_t']}, aylar ≥ %{DECISION['min_positive_months'] * 100:.0f} pozitif (24sa)"]
        if self.last_error:
            lines.append("⚠️ Son sorun: " + html.escape(self.last_error[:220]))
        return "\n".join(lines)
