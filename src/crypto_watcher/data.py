"""Market data: closed-candle klines, tickers, funding, universe selection, synthetic demo."""
from dataclasses import dataclass
import re
import time
import zlib
import numpy as np
import pandas as pd
from .binance import BinanceError, MarketClient, MAINNET_URL, TESTNET_URL
from .config import INTERVAL_SECONDS, Settings

KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
                 "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base"]


def parse_klines(rows: list, now_ms: int) -> pd.DataFrame:
    """Raw Binance klines -> validated DataFrame of CLOSED candles indexed by UTC open time."""
    if not rows:
        raise ValueError("No klines returned")
    df = pd.DataFrame(rows, columns=KLINE_COLUMNS[:len(rows[0])])
    df[NUMERIC] = df[NUMERIC].astype(float)
    df["open_time"], df["close_time"] = df.open_time.astype("int64"), df.close_time.astype("int64")
    df = df[df.close_time < now_ms]  # drop the still-forming candle
    if df.empty:
        raise ValueError("No closed candles")
    df = df.set_index(pd.to_datetime(df.open_time, unit="ms", utc=True))
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        raise ValueError("Klines are not strictly increasing")
    values = df[NUMERIC].to_numpy()
    if not np.isfinite(values).all() or (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Non-finite or non-positive prices")
    if (df.high < df[["open", "close", "low"]].max(axis=1)).any() or (df.low > df[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("Inconsistent OHLC candle")
    if (df.volume < 0).any():
        raise ValueError("Negative volume")
    return df[NUMERIC + ["close_time"]]


class BinanceMarketData:
    """Real-market provider. Pure read-only (no keys)."""

    def __init__(self, client: MarketClient, clock=time.time):
        self.client, self._clock = client, clock

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        return parse_klines(self.client.klines(symbol, interval, limit), int(self._clock() * 1000))

    def tickers(self) -> dict[str, dict]:
        return {row["symbol"]: {"last": float(row["lastPrice"]), "quote_volume": float(row["quoteVolume"]),
                                "change_pct": float(row["priceChangePercent"])}
                for row in self.client.ticker_24h() if row["symbol"].endswith("USDT")}

    def funding(self) -> dict[str, float]:
        return {row["symbol"]: float(row["lastFundingRate"] or 0.0)
                for row in self.client.premium_index() if row["symbol"].endswith("USDT")}


class CachedProvider:
    """Klines only change when a candle closes, so re-scans between closes are free."""

    def __init__(self, inner, clock=time.time):
        self.inner, self._clock, self._cache = inner, clock, {}

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        key = (symbol, interval, limit)
        hit = self._cache.get(key)
        if hit and self._clock() * 1000 < hit[0]:
            return hit[1]
        df = self.inner.klines(symbol, interval, limit)
        next_close = int(df.close_time.iloc[-1]) + 1 + INTERVAL_SECONDS[interval] * 1000
        self._cache[key] = (next_close, df)
        return df

    def tickers(self):
        return self.inner.tickers()

    def funding(self):
        return self.inner.funding()


def choose_market_source(setting: str, session=None) -> tuple[BinanceMarketData, str, str | None]:
    """-> (provider, source name, warning). `auto` prefers mainnet's real volumes and prices."""
    def build(url, name):
        return BinanceMarketData(MarketClient(url, session=session)), name
    if setting in ("auto", "mainnet"):
        provider, name = build(MAINNET_URL, "mainnet")
        try:
            provider.client.ping()
            return provider, name, None
        except BinanceError as error:
            if setting == "mainnet":
                raise
            warning = (f"Mainnet market data unreachable ({error}); falling back to testnet data. "
                       "Testnet prices and volumes are synthetic and unreliable.")
    else:
        warning = "Using testnet market data: prices and volumes are synthetic and unreliable."
    provider, name = build(TESTNET_URL, "testnet")
    return provider, name, warning


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    tick: float
    step: float
    min_qty: float
    max_qty: float
    min_notional: float
    onboard_ms: int


def parse_exchange_info(info: dict) -> dict[str, SymbolInfo]:
    """Tradable USDT perpetuals with their order filters."""
    result = {}
    for s in info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
            continue
        if s.get("underlyingType") == "INDEX":  # e.g. BTCDOMUSDT: not a single tradable coin
            continue
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        try:
            lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
            result[s["symbol"]] = SymbolInfo(
                s["symbol"], s["baseAsset"], float(filters["PRICE_FILTER"]["tickSize"]), float(lot["stepSize"]),
                float(lot["minQty"]), float(lot["maxQty"]),
                float(filters.get("MIN_NOTIONAL", {}).get("notional", 5.0)), int(s.get("onboardDate", 0)))
        except (KeyError, ValueError):
            continue
    return result


def select_universe(settings: Settings, tradable: dict[str, SymbolInfo], tickers: dict[str, dict], now_ms: int) -> list[str]:
    """Most liquid tradable perpetuals; ranking uses the market-data venue's real volumes."""
    excluded = set(settings.exclude_bases)
    min_age = settings.min_listing_days * 86_400_000
    ranked = []
    for symbol, info in tradable.items():
        ticker = tickers.get(symbol)
        if ticker is None or info.base in excluded or not re.fullmatch(r"[A-Z0-9]+", info.base):
            continue
        if info.onboard_ms and now_ms - info.onboard_ms < min_age:
            continue
        if ticker["quote_volume"] >= settings.min_quote_volume:
            ranked.append((ticker["quote_volume"], symbol))
    chosen = [s for _, s in sorted(ranked, reverse=True)[:settings.max_symbols]]
    for symbol in settings.include_symbols:
        if symbol in tradable and symbol not in chosen:
            chosen.append(symbol)
    if "BTCUSDT" in chosen:  # keep the benchmark first; it also drives the regime
        chosen.remove("BTCUSDT")
        chosen.insert(0, "BTCUSDT")
    return chosen


# --------------------------------------------------------------------------
# Synthetic demo data (deterministic). Not real prices; for trying the pipeline offline.
DEMO_BASES = {"BTC": (0.00010, 60000.0), "ETH": (0.00018, 3000.0), "SOL": (0.00030, 150.0),
              "XRP": (-0.00025, 0.55), "DOGE": (0.00035, 0.12), "ADA": (-0.00030, 0.45),
              "LINK": (0.00005, 14.0), "AVAX": (-0.00010, 30.0)}


class DemoProvider:
    def __init__(self, now_ms: int | None = None):
        self.now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    def symbols(self) -> list[str]:
        return [f"{base}USDT" for base in DEMO_BASES]

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        base = symbol.removesuffix("USDT")
        if base not in DEMO_BASES:
            raise ValueError(f"Unknown demo symbol {symbol}")
        drift, start = DEMO_BASES[base]
        step = INTERVAL_SECONDS[interval] * 1000
        last_open = (self.now_ms // step) * step - step  # last fully closed candle
        n = limit
        rng = np.random.default_rng(zlib.crc32(f"{symbol}".encode()))
        scale = np.sqrt(step / 3_600_000)
        returns = rng.normal(drift * (step / 3_600_000), 0.006 * scale, n)
        close = start * np.exp(np.cumsum(returns))
        open_ = np.concatenate([[start], close[:-1]])
        spread = np.abs(rng.normal(0.004 * scale, 0.002 * scale, n)) + 0.001
        high, low = np.maximum(open_, close) * (1 + spread), np.minimum(open_, close) * (1 - spread)
        volume = rng.uniform(800, 1200, n) * (1 + 3 * np.abs(returns) / 0.006)
        taker = volume * np.clip(0.5 + np.sign(returns) * rng.uniform(0, 0.15, n), 0.05, 0.95)
        open_times = last_open - step * np.arange(n - 1, -1, -1)
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume,
                           "quote_volume": volume * close, "taker_buy_base": taker,
                           "close_time": open_times + step - 1}, index=pd.to_datetime(open_times, unit="ms", utc=True))
        return df

    def tickers(self):
        out = {}
        for symbol in self.symbols():
            df = self.klines(symbol, "1h", 300)
            day = df.iloc[-24:]
            out[symbol] = {"last": float(df.close.iloc[-1]), "quote_volume": float(day.quote_volume.sum()) * 1e3,
                           "change_pct": float((day.close.iloc[-1] / day.close.iloc[0] - 1) * 100)}
        return out

    def funding(self):
        return {symbol: 0.0001 for symbol in self.symbols()}
