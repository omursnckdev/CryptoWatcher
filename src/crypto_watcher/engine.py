"""Scan: fetch candles for the universe, compute indicators, score both sides."""
import time
import numpy as np
import pandas as pd
from .config import Settings
from .indicators import calculate
from .strategy import base_asset, evaluate, market_regime

BENCHMARK = "BTCUSDT"
ACTIONABLE = ("LONG_CANDIDATE", "SHORT_CANDIDATE", "STRONG_LONG", "STRONG_SHORT")


def _features(provider, symbol: str, interval: str, settings: Settings) -> pd.DataFrame:
    bars = provider.klines(symbol, interval, settings.kline_limit)
    if len(bars) < 250:
        raise ValueError(f"Need >= 250 {interval} candles, got {len(bars)}")
    return calculate(bars)


def scan(provider, settings: Settings, symbols: list[str], capital: float, news=None, now_ms: int | None = None,
         source: str = "unknown", demo: bool = False) -> dict:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    btc1 = _features(provider, BENCHMARK, settings.timeframe, settings)
    btc4 = _features(provider, BENCHMARK, settings.htf, settings)
    if not np.isfinite(btc1.iloc[-2:][["atr", "ema200", "macd"]].to_numpy(dtype=float)).all():
        raise ValueError("BTC indicators undefined")
    regime = market_regime(btc1, btc4, settings)
    tickers, funding = provider.tickers(), provider.funding()
    signals, errors = [], []
    for symbol in symbols:
        try:
            f1 = btc1 if symbol == BENCHMARK else _features(provider, symbol, settings.timeframe, settings)
            f4 = btc4 if symbol == BENCHMARK else _features(provider, symbol, settings.htf, settings)
            last = tickers.get(symbol, {}).get("last") or float(f1.close.iloc[-1])
            coin_news = news.for_coin(base_asset(symbol)) if news is not None else None
            signals.append(evaluate(symbol, f1, f4, None if symbol == BENCHMARK else btc1, regime,
                                    funding.get(symbol, 0.0), float(last), coin_news, settings, capital,
                                    None if demo else now_ms))
        except Exception as error:  # one bad symbol must not stop the scan
            errors.append({"symbol": symbol, "error": f"{type(error).__name__}: {error}"})
    signals.sort(key=lambda r: (-r["score"], r["symbol"]))
    return {"mode": "DEMO_SYNTHETIC" if demo else "LIVE_ANALYSIS", "data_source": source,
            "as_of": pd.Timestamp(now_ms, unit="ms", tz="UTC").isoformat(), "timeframe": settings.timeframe,
            "htf": settings.htf, "market_regime": regime, "capital_usdt": capital,
            "signals": signals, "errors": errors}


def actionable(report: dict) -> list[dict]:
    return [s for s in report["signals"] if s["signal"] in ACTIONABLE]
