"""Model shadow: a gradient-boosting direction model that records LONG / SHORT decisions without trading.

Separate service (own container, own state folder); it never touches the trading bot, its keys or Telegram. Every hour it
reads the closed 1h candles of the liquid universe, builds the same kind of indicator features the bot uses, asks the model
for P(down) / P(flat) / P(up) over the next 24h and, when the model is confident, writes a LONG or SHORT *decision* to
`model_events.jsonl`. Once 24h have passed the outcome is computed from real candles and funding (same cost model as
`shadow.py`) and compared with three naive baselines on the very same events.

Honesty rules baked in:
* Walk-forward only: the model is trained on rows whose 24h label was already known at the cutoff (purged), retrained weekly.
* The target is not price level but "where did price go in 24h relative to its own volatility" (3 classes).
* The decision rule below is FROZEN before any forward result exists. Do not tune it on outcomes.
* `backtest` replays the exact same code on history so the idea can be judged long before 90 live days have passed.

Not covered: funding in the backtest (history is not fetched), order-book depth, real fills.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import math
import pickle
import time
from pathlib import Path
import numpy as np
import pandas as pd
from .binance import MarketClient, MAINNET_URL
from .config import load_settings
from .data import parse_exchange_info, parse_klines, select_universe
from .indicators import calculate
from .shadow import _day_clustered_t

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
# ---- frozen definition (2026-11-12) ---------------------------------------------------------------------------
HORIZON = 24                  # hours held; entry is the open of the hour AFTER the decision candle
BAND_K = 0.5                  # class band = BAND_K * atr_pct * sqrt(HORIZON): |move| below it is "flat"
P_MIN, MARGIN = 0.42, 0.12    # LONG if p_up >= P_MIN and p_up - p_down >= MARGIN (SHORT mirrored); chance level is ~0.31
COST_BPS = 14.0               # round trip: 2 x 5 bps fee + 2 x 2 bps slippage (same as shadow.py)
RETRAIN_DAYS = 7
TRAIN_DAYS = 365              # history used for fitting
MIN_TRAIN_ROWS = 5_000
FEATURES = ["r1", "r3", "r6", "r12", "r24", "atr_pct", "rsi", "macd_h", "adx", "di_diff", "vol_ratio", "taker",
            "ext20", "ext50", "ext200", "bbp", "rs6", "rs24", "rs72", "btc_r6", "btc_r24"]
# ---- pre-registered decision rule for the forward test -----------------------------------------------------------
DECISION = {"min_events": 150, "min_days": 90, "min_mean_bps": 15.0, "min_t": 2.0, "min_positive_months": 0.6,
            "min_paired_t": 1.65}


# ---------------------------------------------------------------------------------------------------------- features
def _log_ret(close: pd.Series, bars: int) -> pd.Series:
    return np.log(close / close.shift(bars))


def build_features(f1: pd.DataFrame, btc: pd.DataFrame | None) -> pd.DataFrame:
    """One row per candle; every value at row t uses only rows <= t (f1 comes from indicators.calculate)."""
    close = f1.close
    btc_close = (btc.close if btc is not None else close).reindex(f1.index)
    out = pd.DataFrame(index=f1.index)
    for bars in (1, 3, 6, 12, 24):
        out[f"r{bars}"] = _log_ret(close, bars)
    out["atr_pct"] = f1.atr / close
    out["rsi"] = f1.rsi
    out["macd_h"] = f1.macd_hist / f1.atr
    out["adx"] = f1.adx
    out["di_diff"] = f1.pdi - f1.mdi
    out["vol_ratio"] = f1.volume_ratio
    out["taker"] = f1.taker_ratio
    for period in (20, 50, 200):
        out[f"ext{period}"] = (close - f1[f"ema{period}"]) / f1.atr
    out["bbp"] = f1.bb_pctb
    for bars in (6, 24, 72):
        out[f"rs{bars}"] = _log_ret(close, bars) - _log_ret(btc_close, bars)
    out["btc_r6"] = _log_ret(btc_close, 6)
    out["btc_r24"] = _log_ret(btc_close, 24)
    return out.replace([np.inf, -np.inf], np.nan)[FEATURES]


def forward_return_bps(opens: pd.Series, horizon: int = HORIZON) -> pd.Series:
    """Return held from the open of the NEXT candle to the open `horizon` candles later, in bps; NaN when not yet known."""
    return np.log(opens.shift(-(horizon + 1)) / opens.shift(-1)) * 1e4


def make_labels(opens: pd.Series, atr_pct: pd.Series, horizon: int = HORIZON) -> pd.Series:
    """0 = down, 1 = flat, 2 = up, relative to the coin's own volatility. NaN where the outcome is still unknown."""
    r = forward_return_bps(opens, horizon)
    band = BAND_K * atr_pct * math.sqrt(horizon) * 1e4
    y = pd.Series(1.0, index=opens.index)
    y[r > band], y[r < -band] = 2.0, 0.0
    y[r.isna() | band.isna()] = np.nan
    return y


def prepare(bars: pd.DataFrame, btc_bars: pd.DataFrame | None) -> pd.DataFrame:
    """Candles -> features + label + forward return for one symbol."""
    f1 = calculate(bars)
    frame = build_features(f1, calculate(btc_bars) if btc_bars is not None else None)
    frame["y"] = make_labels(bars.open, frame.atr_pct)
    frame["fwd_bps"] = forward_return_bps(bars.open)
    frame["open"] = bars.open
    return frame


def training_set(frames: dict[str, pd.DataFrame], cutoff: pd.Timestamp) -> tuple[pd.DataFrame, pd.Series]:
    """Rows usable at `cutoff` (the first candle whose open is NOT known yet to be part of any label).

    A row at candle t has its label decided at open(t + 1 + HORIZON); it may be trained on only if that open is <= cutoff.
    This purge is what keeps walk-forward honest."""
    last_ok = cutoff - pd.Timedelta(hours=HORIZON + 1)
    start = cutoff - pd.Timedelta(days=TRAIN_DAYS)
    parts = []
    for frame in frames.values():
        part = frame.loc[(frame.index <= last_ok) & (frame.index >= start)]
        parts.append(part.dropna(subset=FEATURES + ["y"]))
    data = pd.concat(parts) if parts else pd.DataFrame(columns=FEATURES + ["y"])
    return data[FEATURES], data["y"].astype(int)


def fit(X: pd.DataFrame, y: pd.Series):
    from sklearn.ensemble import HistGradientBoostingClassifier
    if len(X) < MIN_TRAIN_ROWS or y.nunique() < 3:
        raise ValueError(f"not enough training data ({len(X)} rows)")
    model = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=1.0,
                                           min_samples_leaf=200, random_state=0)
    return model.fit(X.to_numpy(dtype=float), y.to_numpy())


def probabilities(model, X: pd.DataFrame) -> np.ndarray:
    """Columns: P(down), P(flat), P(up)."""
    raw = model.predict_proba(X.to_numpy(dtype=float))
    out = np.zeros((len(X), 3))
    for column, label in enumerate(model.classes_):
        out[:, int(label)] = raw[:, column]
    return out


def decide(p_down: float, p_flat: float, p_up: float) -> str | None:
    if p_up >= P_MIN and p_up - p_down >= MARGIN:
        return "LONG"
    if p_down >= P_MIN and p_down - p_up >= MARGIN:
        return "SHORT"
    return None


# --------------------------------------------------------------------------------------------------------- analysis
def non_overlapping(rows: list[dict]) -> list[dict]:
    free, chosen = {}, []
    for row in sorted(rows, key=lambda r: r["hour"]):
        if row["hour"] < free.get(row["symbol"], -1):
            continue
        chosen.append(row)
        free[row["symbol"]] = row["hour"] + (1 + HORIZON) * HOUR_MS
    return chosen


def analyse(rows: list[dict]) -> dict:
    """rows: {symbol, hour, side(+1/-1), gross, fund, ret24}. Model vs three baselines on the SAME events."""
    rows = non_overlapping(rows)
    if not rows:
        return {"n": 0}
    frame = pd.DataFrame(rows)
    side = frame.side.to_numpy()
    base = frame.gross + frame.fund
    series = {"model": side * base - COST_BPS,
              "inverse": -side * base - COST_BPS,
              "momentum": np.sign(frame.ret24.to_numpy()) * base - COST_BPS,
              "always_long": base - COST_BPS}
    when = pd.to_datetime(frame.hour, unit="ms", utc=True)
    days = when.dt.floor("D")
    model = series["model"]
    months = model.groupby(when.dt.strftime("%Y-%m")).agg(["size", "mean"])
    months = months[months["size"] >= 3]
    baselines = {name: float(values.mean()) for name, values in series.items() if name != "model"}
    best = max(baselines, key=baselines.get)
    result = {"n": len(frame), "mean": float(model.mean()), "win": float((model > 0).mean()),
              "t": _day_clustered_t(days, model), "long_share": float((side > 0).mean()),
              "months_positive": float((months["mean"] > 0).mean()) if len(months) else float("nan"), "months": len(months),
              "baselines": baselines, "best_baseline": best,
              "paired_t": _day_clustered_t(days, model - series[best])}
    return result


def verdict(stats: dict, days: float) -> str:
    rule = DECISION
    if stats["n"] < rule["min_events"] or days < rule["min_days"]:
        return f"yetersiz veri ({stats['n']}/{rule['min_events']} olay, {days:.0f}/{rule['min_days']} gün)"
    passed = (stats["mean"] >= rule["min_mean_bps"] and stats["t"] >= rule["min_t"]
              and stats["months_positive"] >= rule["min_positive_months"] and stats["paired_t"] >= rule["min_paired_t"])
    return "KURALI GEÇTİ: kâğıt üzerinde işlem aşamasına aday (otomatik canlıya alınmaz)" if passed else \
        "KURALI GEÇMEDİ: model rafa kaldırılmalı"


def format_stats(stats: dict) -> str:
    if not stats["n"]:
        return "henüz sonuçlanan olay yok"
    t = f"{stats['t']:+.1f}" if stats["t"] == stats["t"] else "—"
    pt = f"{stats['paired_t']:+.1f}" if stats["paired_t"] == stats["paired_t"] else "—"
    base = " · ".join(f"{name} {value:+.0f}" for name, value in stats["baselines"].items())
    months = f" · ay+ {stats['months_positive'] * 100:.0f}%" if stats["months"] else ""
    return (f"n={stats['n']} · model ort {stats['mean']:+.0f} bp · kazanma {stats['win'] * 100:.0f}% · t {t}{months} · "
            f"long payı {stats['long_share'] * 100:.0f}%\n  tabanlar (bp): {base}\n  en iyi tabana karşı eşli t {pt}")


# --------------------------------------------------------------------------------------------------------- backtest
def walk_forward(frames: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp, step_days: int = RETRAIN_DAYS) -> list[dict]:
    """Retrain every `step_days` on purged history, predict the next block. No funding (not in the candles)."""
    rows, cutoff = [], start
    while cutoff < end:
        block_end = min(cutoff + pd.Timedelta(days=step_days), end)
        try:
            model = fit(*training_set(frames, cutoff))
        except ValueError as error:
            log.warning("skip block %s: %s", cutoff, error)
            cutoff = block_end
            continue
        for symbol, frame in frames.items():
            block = frame.loc[(frame.index >= cutoff) & (frame.index < block_end)].dropna(subset=FEATURES + ["fwd_bps"])
            if block.empty:
                continue
            probs = probabilities(model, block[FEATURES])
            for (stamp, row), p in zip(block.iterrows(), probs):
                side = decide(*p)
                if side:
                    rows.append({"symbol": symbol, "hour": int(stamp.timestamp() * 1000), "side": 1 if side == "LONG" else -1,
                                 "gross": float(row.fwd_bps), "fund": 0.0, "ret24": float(row.r24)})
        cutoff = block_end
    return rows


def fetch_history(client, symbol: str, start_ms: int, end_ms: int, now_ms: int | None = None) -> pd.DataFrame:
    now_ms = now_ms or int(time.time() * 1000)
    rows, cursor = [], start_ms
    while cursor < end_ms:
        page = client.klines(symbol, "1h", 1500, start_ms=cursor, end_ms=end_ms)
        if not page:
            break
        rows += page
        nxt = int(page[-1][0]) + HOUR_MS
        if nxt <= cursor:
            break
        cursor = nxt
        if len(page) < 1500:
            break
    return parse_klines(rows, now_ms)


# ---------------------------------------------------------------------------------------------------------- live service
class ModelShadow:
    def __init__(self, client, state_dir: str | Path, symbols_fn, clock=time.time):
        self.client, self.dir, self.symbols_fn, self._clock = client, Path(state_dir), symbols_fn, clock
        self.path, self.model_path = self.dir / "model_events.jsonl", self.dir / "model.pkl"
        self.events: dict[str, dict] = {}
        self.outcomes: dict[str, dict] = {}
        self.start_ms: int | None = None
        self.evals = self.eval_hours = 0
        self.model, self.trained_ms = None, 0
        self.last_error: str | None = None
        self._hour_done = -1
        self._load()
        if self.start_ms is None:
            self.start_ms = int(self._clock() * 1000)
            self._append({"k": "start", "ms": self.start_ms})

    # persistence
    def _load(self):
        if self.model_path.is_file():
            try:
                with self.model_path.open("rb") as handle:
                    saved = pickle.load(handle)
                self.model, self.trained_ms = saved["model"], saved["trained_ms"]
            except Exception as error:           # a bad model file just means: retrain
                log.warning("Ignoring unreadable model file: %s", error)
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
                    self.outcomes[record["id"]] = record
                elif kind == "tick":
                    self.evals += int(record["n"])
                    self.eval_hours += 1
                    self._hour_done = max(self._hour_done, int(record["hour"]))
            except (ValueError, KeyError, TypeError):
                log.warning("Skipping unreadable line %d in %s", number, self.path)

    def _append(self, record: dict):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as error:
            log.error("Could not write %s: %s", self.path, error)

    # training
    def retrain(self, symbols: list[str], now_ms: int):
        cutoff = pd.Timestamp(now_ms // HOUR_MS * HOUR_MS, unit="ms", tz="UTC")
        start_ms = int((cutoff - pd.Timedelta(days=TRAIN_DAYS + 20)).timestamp() * 1000)
        btc = fetch_history(self.client, "BTCUSDT", start_ms, now_ms, now_ms)
        frames = {}
        for symbol in symbols:
            try:       # one symbol that cannot be fetched must not stop the training of all the others
                bars = btc if symbol == "BTCUSDT" else fetch_history(self.client, symbol, start_ms, now_ms, now_ms)
                frames[symbol] = prepare(bars, None if symbol == "BTCUSDT" else btc)
            except Exception as error:
                log.warning("Training without %s: %s", symbol, error)
        model = fit(*training_set(frames, cutoff))
        self.model, self.trained_ms = model, now_ms
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.model_path.open("wb") as handle:
            pickle.dump({"model": model, "trained_ms": now_ms}, handle)
        log.info("Model retrained on %d symbols", len(frames))

    # hourly step
    def tick(self):
        now_ms = int(self._clock() * 1000)
        hour = now_ms // HOUR_MS * HOUR_MS
        errors = []
        if hour != self._hour_done:
            try:
                symbols = self.symbols_fn()
                if self.model is None or now_ms - self.trained_ms > RETRAIN_DAYS * DAY_MS:
                    self.retrain(symbols, now_ms)
                self._decide(symbols, hour, now_ms, errors)
                self._hour_done = hour
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
        try:
            self._resolve(now_ms)
        except Exception as error:
            errors.append(f"outcome resolution: {type(error).__name__}: {error}")
        self.last_error = "; ".join(errors[:3]) if errors else None
        if errors:
            log.warning("Model shadow: %s", self.last_error)

    def _decide(self, symbols: list[str], hour: int, now_ms: int, errors: list):
        btc_bars = parse_klines(self.client.klines("BTCUSDT", "1h", 300), now_ms)
        evaluated = 0
        for symbol in symbols:
            try:
                bars = btc_bars if symbol == "BTCUSDT" else parse_klines(self.client.klines(symbol, "1h", 300), now_ms)
                if int(bars.index[-1].timestamp() * 1000) != hour - HOUR_MS:
                    raise ValueError("candles are not up to date")
                row = prepare(bars, None if symbol == "BTCUSDT" else btc_bars).iloc[-1]
                if row[FEATURES].isna().any():
                    raise ValueError("features undefined (short history)")
                p = probabilities(self.model, row[FEATURES].to_frame().T)[0]
                evaluated += 1
                side = decide(*p)
                key = f"{symbol}-{hour}"
                if side and key not in self.events:
                    record = {"k": "event", "id": key, "symbol": symbol, "hour": hour, "side": 1 if side == "LONG" else -1,
                              "p_down": round(float(p[0]), 4), "p_flat": round(float(p[1]), 4), "p_up": round(float(p[2]), 4),
                              "ret24": float(row.r24), "price": float(bars.close.iloc[-1])}
                    self.events[key] = record
                    self._append(record)
                    log.info("MODEL %s %s p_up %.2f p_down %.2f", side, symbol, p[2], p[0])
            except Exception as error:
                errors.append(f"{symbol}: {type(error).__name__}: {error}")
        self.evals += evaluated
        self.eval_hours += 1
        self._append({"k": "tick", "hour": hour, "n": evaluated})

    def _resolve(self, now_ms: int):
        for event in sorted(self.events.values(), key=lambda e: e["hour"]):
            entry_ms = event["hour"] + HOUR_MS
            exit_ms = entry_ms + HORIZON * HOUR_MS
            if event["id"] in self.outcomes or now_ms < exit_ms + HOUR_MS + 60_000:
                continue
            opens = parse_klines(self.client.klines(event["symbol"], "1h", 300), now_ms).open
            entry_t, exit_t = (pd.Timestamp(ms, unit="ms", tz="UTC") for ms in (entry_ms, exit_ms))
            if entry_t not in opens.index or exit_t not in opens.index:
                if now_ms - exit_ms > 250 * HOUR_MS:
                    self._store(event, {"lost": True})
                continue
            funding = self.client.funding_rates(event["symbol"], entry_ms, exit_ms - 1)
            paid = sum(float(r["fundingRate"]) for r in funding if entry_ms <= int(r["fundingTime"]) < exit_ms)
            self._store(event, {"gross": math.log(float(opens[exit_t]) / float(opens[entry_t])) * 1e4, "fund": -paid * 1e4})

    def _store(self, event: dict, values: dict):
        record = {"k": "outcome", "id": event["id"], **values}
        self.outcomes[event["id"]] = record
        self._append(record)

    # reporting
    def rows(self) -> list[dict]:
        return [{**event, "gross": self.outcomes[event["id"]]["gross"], "fund": self.outcomes[event["id"]]["fund"]}
                for event in self.events.values() if "gross" in self.outcomes.get(event["id"], {})]

    def summary(self) -> str:
        now_ms = int(self._clock() * 1000)
        days = (now_ms - self.start_ms) / DAY_MS
        stats = analyse(self.rows())
        started = datetime.fromtimestamp(self.start_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        trained = datetime.fromtimestamp(self.trained_ms / 1000, timezone.utc).strftime("%Y-%m-%d") if self.trained_ms else "—"
        rate = f"{len(self.events) / max(self.evals, 1) * 100:.1f}%" if self.evals else "—"
        lines = [f"Model gölge kaydı (işlem açılmıyor) · başlangıç {started} · {days:.0f} gün · son eğitim {trained}",
                 f"{self.evals} değerlendirme, {len(self.events)} karar (oran {rate}), {len(self.outcomes)} sonuçlanan",
                 f"{HORIZON}sa: " + format_stats(stats),
                 f"Karar: {verdict(stats, days) if stats['n'] else 'yetersiz veri'}",
                 f"Kural: ≥{DECISION['min_events']} olay, ≥{DECISION['min_days']} gün, ort ≥ +{DECISION['min_mean_bps']:.0f} bp, "
                 f"t ≥ {DECISION['min_t']}, aylar ≥ %{DECISION['min_positive_months'] * 100:.0f} pozitif, en iyi tabana karşı eşli t ≥ {DECISION['min_paired_t']}"]
        if self.last_error:
            lines.append("Son sorun: " + self.last_error[:220])
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------------------- command line
def _universe_fn(client, settings):
    def universe() -> list[str]:
        now_ms = int(time.time() * 1000)
        tradable = parse_exchange_info(client.exchange_info())
        tickers = {r["symbol"]: {"last": float(r["lastPrice"]), "quote_volume": float(r["quoteVolume"]),
                                 "change_pct": float(r["priceChangePercent"])}
                   for r in client.ticker_24h() if r["symbol"].endswith("USDT")}
        return select_universe(settings, tradable, tickers, now_ms)
    return universe


def cmd_backtest(args, client, settings) -> int:
    now_ms = int(time.time() * 1000)
    symbols = _universe_fn(client, settings)()[:args.symbols]
    start_ms = now_ms - (args.months * 30 + TRAIN_DAYS + 20) * DAY_MS
    btc = fetch_history(client, "BTCUSDT", start_ms, now_ms, now_ms)
    frames = {}
    for symbol in symbols:
        bars = btc if symbol == "BTCUSDT" else fetch_history(client, symbol, start_ms, now_ms, now_ms)
        frames[symbol] = prepare(bars, None if symbol == "BTCUSDT" else btc)
        print(f"  {symbol}: {len(bars)} mum", flush=True)
    end = pd.Timestamp(now_ms // HOUR_MS * HOUR_MS, unit="ms", tz="UTC") - pd.Timedelta(hours=HORIZON + 2)
    start = end - pd.Timedelta(days=args.months * 30)
    rows = walk_forward(frames, start, end)
    stats = analyse(rows)
    print(f"\nWalk-forward ({args.months} ay, {len(symbols)} coin, haftalık yeniden eğitim, fonlama dahil değil):")
    print(format_stats(stats))
    if stats["n"]:
        print("Karar:", verdict(stats, args.months * 30))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cryptowatcher-model", description="Model shadow: LONG/SHORT decisions without trading")
    parser.add_argument("--config", type=Path, default=Path("config/settings.toml"))
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Hourly loop (no orders)")
    sub.add_parser("report", help="Print the summary and exit")
    back = sub.add_parser("backtest", help="Walk-forward replay on history")
    back.add_argument("--months", type=int, default=12)
    back.add_argument("--symbols", type=int, default=10)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    settings = load_settings(args.config if args.config.is_file() else None)
    client = MarketClient(MAINNET_URL)
    if args.command == "report":
        print(ModelShadow(client, args.state_dir, lambda: []).summary())
        return 0
    client.ping()       # raises with a clear message when mainnet data is blocked (HTTP 451 in some countries)
    if args.command == "backtest":
        return cmd_backtest(args, client, settings)
    service = ModelShadow(client, args.state_dir, _universe_fn(client, settings))
    while True:
        service.tick()
        now = time.time()
        done = service._hour_done == int(now // 3600) * HOUR_MS
        time.sleep(max(30.0, (int(now // 3600) + 1) * 3600 + 120 - now) if done else 60.0)   # 2 min after the next hour mark; retry in 1 min on failure


if __name__ == "__main__":
    raise SystemExit(main())
