"""Backtest: replay the live strategy on Binance's public historical futures klines (data.binance.vision).

The signal path is the live one: `calculate()` -> `evaluate()` -> `risk_plan()`, nothing re-implemented.
What a backtest cannot know is marked in the report: historical funding (a constant is used), news (absent, so the
score is normalised over the remaining factors exactly as it is live when a coin has no headlines), order book
depth, and the portfolio caps (every signal is traded, one position per symbol at a time).

Conservative conventions: entry at the OPEN of the candle after the signal; when stop and target are both inside one
candle the stop is assumed to hit first; the breakeven stop only applies from the next candle.
"""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import calendar
import io
import logging
import math
import zipfile
import numpy as np
import pandas as pd
from .config import INTERVAL_SECONDS, Settings
from .indicators import calculate
from .net import make_session
from .strategy import evaluate, market_regime

log = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision/data/futures/um"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
                   "DOTUSDT", "LTCUSDT", "TRXUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "ATOMUSDT",
                   "UNIUSDT", "AAVEUSDT", "FILUSDT", "INJUSDT", "ETCUSDT", "BCHUSDT")
WARMUP_DAYS = 60            # EMA200 on the higher timeframe needs ~35 days of 4h candles
MAX_CSV_BYTES = 50_000_000
ACTIONABLE = ("LONG_CANDIDATE", "SHORT_CANDIDATE", "STRONG_LONG", "STRONG_SHORT")
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"]
NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base"]


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    split: date                # first day of the out-of-sample ("test") period
    fee: float = 0.0005        # taker fee per side
    slippage: float = 0.0002   # per side, applied to entries and to every (market) exit
    funding: float = 0.0001    # constant funding rate fed to the scorer (history is not modelled)


# ---- download ---------------------------------------------------------------------------------------------
def fetch_plan(first: date, end: date) -> list[tuple[str, bool, list[date]]]:
    """-> [(YYYY-MM, monthly file is complete, days of that month up to `end`)] covering first..end."""
    plan, cursor = [], first.replace(day=1)
    while cursor <= end:
        last_day = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        days = [cursor + timedelta(i) for i in range((min(last_day, end) - cursor).days + 1)]
        plan.append((f"{cursor:%Y-%m}", last_day <= end, days))
        cursor = last_day + timedelta(1)
    return plan


def _download(session, cache: Path, symbol: str, interval: str, kind: str, period: str) -> Path | None:
    target = cache / kind / f"{symbol}-{interval}-{period}.zip"
    if target.exists():
        return target
    response = session.get(f"{BASE_URL}/{kind}/klines/{symbol}/{interval}/{symbol}-{interval}-{period}.zip", timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".part")
    temporary.write_bytes(response.content)
    temporary.replace(target)
    return target


def ensure_files(session, cache: Path, symbol: str, interval: str, plan) -> list[Path]:
    """Monthly archives where complete, daily ones for the running month or when a monthly file is not out yet."""
    paths = []
    for month, monthly_complete, days in plan:
        path = _download(session, cache, symbol, interval, "monthly", month) if monthly_complete else None
        if path:
            paths.append(path)
            continue
        paths.extend(p for day in days if (p := _download(session, cache, symbol, interval, "daily", day.isoformat())))
    return paths


def to_ms(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch milliseconds regardless of the index's internal resolution (pandas 3 does not guarantee ns)."""
    return ((index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)).to_numpy()


# ---- parsing (downloaded files are untrusted: read in memory, never extracted) ----------------------------
def read_klines_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) != 1:
            raise ValueError(f"{path.name}: expected exactly one file in the archive")
        info = infos[0]
        if not info.filename.endswith(".csv") or "/" in info.filename or "\\" in info.filename or ".." in info.filename:
            raise ValueError(f"{path.name}: unexpected archive member {info.filename!r}")
        if info.file_size > MAX_CSV_BYTES:
            raise ValueError(f"{path.name}: archive member too large")
        raw = archive.read(info)
    df = pd.read_csv(io.BytesIO(raw), header=None, names=COLS)   # newer files carry a header row: dropped below
    df = df[pd.to_numeric(df.open_time, errors="coerce").notna()]
    return df.astype({"open_time": "int64", "close_time": "int64", **{c: "float64" for c in NUMERIC}})


def load_frame(paths: list[Path], interval: str, since: pd.Timestamp, until: pd.Timestamp) -> pd.DataFrame:
    df = pd.concat([read_klines_zip(p) for p in paths]).drop_duplicates("open_time").sort_values("open_time")
    df = df.set_index(pd.to_datetime(df.open_time, unit="ms", utc=True))
    df = df[(df.index >= since) & (df.index < until)][NUMERIC + ["close_time"]]
    values = df[NUMERIC].to_numpy()
    if df.empty or not np.isfinite(values).all() or (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("no usable candles (empty, non-finite or non-positive prices)")
    df.attrs["gaps"] = int((np.diff(to_ms(df.index)) != INTERVAL_SECONDS[interval] * 1000).sum())
    return df


# ---- simulation ---------------------------------------------------------------------------------------------
def simulate_trade(side: str, entry: float, distance: float, high, low, close, start: int, settings: Settings,
                   max_candles: int) -> tuple[float, str, int]:
    """Walk candles from `start` (the entry candle). -> (exit price before slippage, reason, candles held)."""
    s = 1 if side == "LONG" else -1
    stop, target = entry - s * distance, entry + s * settings.tp_r * distance
    armed = False
    for k in range(start, len(high)):
        held = k - start + 1
        if (low[k] <= stop) if s > 0 else (high[k] >= stop):          # stop first when both are inside one candle
            return stop, "BREAKEVEN_STOP" if armed else "STOP_LOSS", held
        if (high[k] >= target) if s > 0 else (low[k] <= target):
            return target, "TAKE_PROFIT", held
        favourable = (high[k] if s > 0 else low[k]) - entry
        if settings.breakeven_r and not armed and s * favourable >= settings.breakeven_r * distance:
            armed, stop = True, entry * (1 + s * 2 * settings.taker_fee)   # effective from the next candle
        if held >= max_candles:
            return close[k], "TIME_STOP", held
    return close[-1], "END", len(high) - start


def settle(side: str, raw_open: float, entry: float, raw_exit: float, distance: float, cfg: BacktestConfig) -> tuple[float, float, float]:
    """Cost accounting of one trade. `entry` already includes entry slippage; the exit gets its slippage here.

    -> (net R, total cost in R = fees + slippage, exit fill price)."""
    sign = 1 if side == "LONG" else -1
    exit_fill = raw_exit * (1 - sign * cfg.slippage)
    fees = cfg.fee * (entry + exit_fill)
    net = (sign * (exit_fill - entry) - fees) / distance
    cost = (fees + cfg.slippage * (raw_open + exit_fill)) / distance
    return net, cost, exit_fill


def regimes_for(btc1: pd.DataFrame, btc_htf: pd.DataFrame, settings: Settings) -> list[str]:
    htf_close = btc_htf.close_time.to_numpy()
    out = []
    for i in range(len(btc1)):
        j = int(np.searchsorted(htf_close, btc1.close_time.iloc[i], side="right"))
        ready = j > 0 and np.isfinite(btc1.iloc[i][["atr", "ema200", "macd"]].to_numpy(dtype=float)).all()
        out.append(market_regime(btc1.iloc[i:i + 1], btc_htf.iloc[j - 1:j], settings) if ready else "NEUTRAL")
    return out


def backtest_symbol(symbol: str, h1: pd.DataFrame, h4: pd.DataFrame, btc1: pd.DataFrame, regimes: list[str],
                    settings: Settings, cfg: BacktestConfig) -> list[dict]:
    interval_ms = INTERVAL_SECONDS[settings.timeframe] * 1000
    max_candles = max(1, math.ceil(settings.max_hold_hours * 3600 / INTERVAL_SECONDS[settings.timeframe])) \
        if settings.max_hold_hours else 10**9
    f1, f4 = calculate(h1), calculate(h4)
    o, hi, lo, c = (f1[k].to_numpy() for k in ("open", "high", "low", "close"))
    close_time, htf_close = f1.close_time.to_numpy(), f4.close_time.to_numpy()
    open_ms = to_ms(f1.index)
    first = int(f1.index.searchsorted(pd.Timestamp(cfg.start, tz="UTC")))
    last_allowed = f1.index.searchsorted(pd.Timestamp(cfg.end, tz="UTC") + pd.Timedelta(days=1)) - 2
    trades, free_at = [], -1
    for i in range(first, min(len(f1) - 2, last_allowed)):
        if close_time[i] < free_at or open_ms[i + 1] - open_ms[i] != interval_ms:
            continue
        ib = btc1.index.get_indexer([f1.index[i]])[0]
        if ib < 0:
            continue
        j = int(np.searchsorted(htf_close, close_time[i], side="right"))
        try:
            result = evaluate(symbol, f1.iloc[:i + 1], f4.iloc[:j], None if symbol == "BTCUSDT" else btc1.iloc[:ib + 1],
                              regimes[ib], cfg.funding, float(c[i]), None, settings, settings.capital_usdt or 10_000.0, None)
        except ValueError:                      # undefined indicators: not enough history yet
            continue
        if result["signal"] not in ACTIONABLE:
            continue
        side = result["side"]
        record = {"symbol": symbol, "ts": f1.index[i], "side": side, "signal": result["signal"], "score": result["score"],
                  "regime": regimes[ib], "atr_pct": result["atr_pct"]}
        exit_index = i
        for tag, trade_side in (("", side), ("inv_", "SHORT" if side == "LONG" else "LONG")):
            sign = 1 if trade_side == "LONG" else -1
            entry = o[i + 1] * (1 + sign * cfg.slippage)
            distance = result["atr_pct"] * o[i + 1] * settings.atr_stop_multiplier
            raw_exit, reason, held = simulate_trade(trade_side, entry, distance, hi, lo, c, i + 1, settings, max_candles)
            record[tag + "R"], record[tag + "cost_R"], _ = settle(trade_side, o[i + 1], entry, raw_exit, distance, cfg)
            record[tag + "reason"], record[tag + "held"] = reason, held
            if not tag:
                exit_index = i + held
        trades.append(record)
        free_at = close_time[min(exit_index, len(close_time) - 1)] + settings.cooldown_minutes * 60_000   # live cooldown
    return trades


def _worker(args) -> list[dict]:
    return backtest_symbol(*args)


# ---- orchestration ------------------------------------------------------------------------------------------
def run_backtest(settings: Settings, cfg: BacktestConfig, symbols, cache: Path, workers: int = 1, session=None,
                 progress=print) -> tuple[pd.DataFrame, list[str]]:
    symbols = list(dict.fromkeys(["BTCUSDT", *symbols]))
    first = cfg.start - timedelta(days=WARMUP_DAYS)
    since = pd.Timestamp(first, tz="UTC")
    until = pd.Timestamp(cfg.end, tz="UTC") + pd.Timedelta(days=1)
    plan = fetch_plan(first, cfg.end)
    session = session or make_session()
    intervals = (settings.timeframe, settings.htf)
    progress(f"Veri hazırlanıyor ({len(symbols)} sembol × {len(intervals)} zaman dilimi; önbellek: {cache}) ...")
    with ThreadPoolExecutor(8) as pool:
        futures = {(s, iv): pool.submit(ensure_files, session, cache, s, iv, plan) for s in symbols for iv in intervals}
        files = {key: future.result() for key, future in futures.items()}
    frames, skipped = {}, []
    for symbol in symbols:
        try:
            if not all(files[(symbol, iv)] for iv in intervals):
                raise ValueError("no data files (listed later than the start date?)")
            frames[symbol] = tuple(load_frame(files[(symbol, iv)], iv, since, until) for iv in intervals)
            if gaps := sum(f.attrs["gaps"] for f in frames[symbol]):
                skipped.append(f"{symbol}: {gaps} candle gap(s) in the data (signals next to a gap are skipped)")
        except ValueError as error:
            skipped.append(f"{symbol}: {error}")
    if "BTCUSDT" not in frames:
        raise ValueError("BTCUSDT data is required (regime and relative strength)")
    btc1, btc4 = calculate(frames["BTCUSDT"][0]), calculate(frames["BTCUSDT"][1])
    regimes = regimes_for(btc1, btc4, settings)
    jobs = [(s, *frames[s], btc1, regimes, settings, cfg) for s in frames]
    progress(f"{len(jobs)} sembol simüle ediliyor (workers={workers}) ...")
    if workers > 1:
        with ProcessPoolExecutor(workers) as pool:
            results = list(pool.map(_worker, jobs))
    else:
        results = [_worker(job) for job in jobs]
    rows = [row for result in results for row in result]
    trades = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True) if rows else pd.DataFrame()
    return trades, skipped


# ---- reporting ------------------------------------------------------------------------------------------------
def _line(label: str, d: pd.DataFrame) -> str:
    if d.empty:
        return f"{label:<22}{'-':>6}"
    r, inv = d.R, d.inv_R
    ci = 1.96 * r.std(ddof=1) / math.sqrt(len(r)) if len(r) > 1 else float("nan")
    return (f"{label:<22}{len(r):>6}  {100 * (r > 0).mean():>5.1f}%  {r.mean():>+7.3f} ±{ci:.3f}  {(r + d.cost_R).mean():>+7.3f}"
            f"  {r.sum():>+9.1f}  {inv.mean():>+7.3f}")


def format_report(trades: pd.DataFrame, cfg: BacktestConfig, settings: Settings, symbols_used: int, skipped: list[str]) -> str:
    head = (f"CryptoWatcher backtest · {cfg.start} → {cfg.end} · {symbols_used} sembol · {settings.timeframe}/{settings.htf}\n"
            f"Maliyet: komisyon %{cfg.fee * 100:.3f} ×2 + kayma %{cfg.slippage * 100:.3f} ×2 · eğitim < {cfg.split} ≤ test\n"
            f"Strateji: stop {settings.atr_stop_multiplier:g}×ATR, hedef {settings.tp_r:g}R, breakeven {settings.breakeven_r:g}R, "
            f"zaman stopu {settings.max_hold_hours:g}sa, eşik {settings.entry_score:g}")
    if trades.empty:
        return head + "\n\nBu dönemde hiç sinyal üretilmedi."
    t = trades
    split = pd.Timestamp(cfg.split, tz="UTC")
    columns = f"{'':<22}{'işlem':>6}  {'kazanma':>6}  {'net ort.R (±%95)':>17}  {'brüt R':>7}  {'toplam R':>9}  {'TERSİ R':>7}"
    parts = [head, "", columns, "-" * len(columns), _line("TÜMÜ", t), _line("eğitim dönemi", t[t.ts < split]),
             _line("test dönemi", t[t.ts >= split])]
    parts += [_line(f"yön {k}", g) for k, g in t.groupby("side")]
    parts += [_line(f"BTC rejimi {k}", g) for k, g in t.groupby("regime")]
    parts += [_line(f"ay {k}", g) for k, g in t.groupby(t.ts.dt.strftime("%Y-%m"))]
    buckets = pd.cut(t.score, [0, 70, 75, 80, 85, 101], right=False)
    parts += [_line(f"skor {k.left:g}-{k.right:g}", g) for k, g in t.groupby(buckets, observed=True)]
    exits = t.groupby("reason").R.agg(["count", "mean"])
    parts += ["", "Çıkış sebepleri: " + " · ".join(f"{k} {int(v['count'])} ({v['mean']:+.2f}R)" for k, v in exits.iterrows())]
    breakeven_win = 1 / (1 + settings.tp_r)
    parts += ["", f"Başa baş için gereken kazanma oranı (maliyet öncesi) ≈ %{breakeven_win * 100:.0f}; gerçekleşen %{100 * (t.R > 0).mean():.1f}.",
              "Okuma: 'net ort.R' sıfırdan, güven aralığıyla birlikte, anlamlı biçimde büyük değilse strateji maliyetleri çıkaramıyor demektir.",
              "'brüt R' maliyet öncesidir; ≈0 ise kayıp tamamen işlem masrafıdır. 'TERSİ R' aynı sinyalin ters yönde işlenmesidir.",
              "Eğitim ve test dönemi aynı yönde pozitif değilse bulgu güvenilir değildir. Çok sayıda değişikliği denemek de tesadüfen iyi sonuç üretir.",
              "Bilinmeyenler: geçmiş funding (sabit), haber, emir defteri ve portföy limitleri modellenmedi."]
    if skipped:
        parts += ["", "Atlanan semboller: " + "; ".join(skipped)]
    return "\n".join(parts)


def default_dates(today: date | None = None, days: int = 180) -> tuple[date, date]:
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    return end - timedelta(days=days), end
