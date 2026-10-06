"""Explainable long/short scoring and the risk plan. Pure functions: no network, no clock."""
import math
import re
import numpy as np
import pandas as pd
from .binance import ROUND_HALF_UP, round_step
from .config import Settings
from .data import SymbolInfo

WEIGHTS = {"trend": 20, "momentum": 15, "bollinger": 10, "volume": 15, "derivatives": 5,
           "higher_timeframe": 10, "relative_strength": 10, "market_regime": 5, "news": 10}
REQUIRED_1H = ("close", "ema20", "ema50", "ema200", "rsi", "macd", "macd_signal", "macd_hist", "bb_pctb", "bb_upper",
               "bb_lower", "bb_width", "atr", "adx", "pdi", "mdi", "volume_ratio", "obv", "obv_ema", "taker_ratio",
               "turnover20")
REQUIRED_HTF = ("close", "ema20", "ema50", "macd", "macd_signal")
RS_BARS = ((6, 2), (24, 4), (72, 4))


def base_asset(symbol: str) -> str:
    return re.sub(r"^(1000000|1000)(?=[A-Z])", "", symbol.removesuffix("USDT"))


def market_regime(btc: pd.DataFrame, btc_htf: pd.DataFrame, settings: Settings) -> str:
    row, high = btc.iloc[-1], btc_htf.iloc[-1]
    if row.atr / row.close > settings.btc_high_vol_atr_pct:
        return "HIGH_VOLATILITY"
    if row.close > row.ema50 > row.ema200 and row.macd > 0 and high.close > high.ema50:
        return "BULL"
    if row.close < row.ema50 < row.ema200 and row.macd < 0 and high.close < high.ema50:
        return "BEAR"
    return "NEUTRAL"


def risk_plan(side: str, entry: float, atr: float, capital: float, settings: Settings,
              info: SymbolInfo | None = None, leverage: int | None = None) -> dict:
    """Stop at ATR multiple, take-profit at tp_r * R. Quantity = min(risk budget, margin budget)."""
    leverage = leverage or settings.leverage
    sign = 1 if side == "LONG" else -1
    distance = atr * settings.atr_stop_multiplier
    if not all(math.isfinite(v) and v > 0 for v in (entry, atr, distance, capital)):
        raise ValueError("Invalid entry/ATR/capital")
    stop, take = entry - sign * distance, entry + sign * settings.tp_r * distance
    if stop <= 0 or take <= 0:
        raise ValueError("Stop or target would be non-positive")
    if distance > 0.6 * entry / leverage:
        raise ValueError(f"Stop too wide for {leverage}x leverage (liquidation risk)")
    round_trip = 2 * settings.taker_fee * entry
    if round_trip / distance > settings.max_cost_to_stop:
        raise ValueError("Round-trip fees too large relative to stop distance")
    quantity = min(capital * settings.risk_fraction / distance, capital * settings.max_margin_fraction * leverage / entry)
    if info is not None:
        stop, take = round_step(stop, info.tick, ROUND_HALF_UP), round_step(take, info.tick, ROUND_HALF_UP)
        quantity = min(round_step(quantity, info.step), info.max_qty)
        if quantity < info.min_qty or quantity * entry < info.min_notional:
            raise ValueError("Risk/margin budget cannot fund the minimum order size")
    return {"side": side, "entry": entry, "stop": stop, "take_profit": take, "risk_distance": distance,
            "quantity": quantity, "notional": quantity * entry, "margin": quantity * entry / leverage,
            "leverage": leverage, "planned_risk": quantity * distance, "risk_reward": settings.tp_r,
            "estimated_fees": quantity * round_trip}


def _relative_strength(close: pd.Series, btc_close: pd.Series, d: int) -> list[tuple[bool, float, str]]:
    rules = []
    for bars, weight in RS_BARS:
        diff = float(close.iloc[-1] / close.iloc[-bars - 1] - btc_close.iloc[-1] / btc_close.iloc[-bars - 1])
        word = "Outperforms" if d > 0 else "Underperforms"
        rules.append((d * diff > 0, weight, f"{word} BTC over {bars} candles"))
    return rules


def score_side(side: str, f1: pd.DataFrame, f4: pd.DataFrame, btc: pd.DataFrame | None, funding: float,
               news, regime: str, settings: Settings) -> dict:
    d = 1 if side == "LONG" else -1
    row, prior, high = f1.iloc[-1], f1.iloc[-2], f4.iloc[-1]
    components, reasons, available = {}, {"positive": [], "negative": [], "unavailable": []}, dict(WEIGHTS)

    def points(name, rules):
        total = 0.0
        for passed, weight, text in rules:
            total += weight if passed else 0
            reasons["positive" if passed else "negative"].append(f"{text} {'✓' if passed else '✗'}")
        components[name] = round(total, 4)

    persistence = row.trend_persistence_up if d > 0 else row.trend_persistence_down
    points("trend", [(d * (row.close - row.ema20) > 0, 2, f"Close {'>' if d > 0 else '<'} EMA20"),
                     (d * (row.close - row.ema50) > 0, 3, f"Close {'>' if d > 0 else '<'} EMA50"),
                     (d * (row.close - row.ema200) > 0, 3, f"Close {'>' if d > 0 else '<'} EMA200"),
                     (d * (row.ema20 - row.ema50) > 0, 2, f"EMA20 {'>' if d > 0 else '<'} EMA50"),
                     (d * (row.ema50 - row.ema200) > 0, 3, f"EMA50 {'>' if d > 0 else '<'} EMA200"),
                     (row.adx > 20 and d * (row.pdi - row.mdi) > 0, 2, "ADX > 20 with directional dominance")])
    components["trend"] += float(5 * persistence)
    band = (50, 70) if d > 0 else (30, 50)
    points("momentum", [(band[0] <= row.rsi <= band[1], 5, f"RSI in {band[0]}–{band[1]} range"),
                        (d * (row.macd - row.macd_signal) > 0, 5, f"MACD {'>' if d > 0 else '<'} signal"),
                        (d * (row.macd_hist - prior.macd_hist) > 0, 5, f"MACD histogram {'rising' if d > 0 else 'falling'}")])
    pctb_ok = 0.5 < row.bb_pctb <= 1.1 if d > 0 else -0.1 <= row.bb_pctb < 0.5
    beyond = row.close > row.bb_upper if d > 0 else row.close < row.bb_lower
    points("bollinger", [(pctb_ok, 5, f"Bollinger %B on the {'upper' if d > 0 else 'lower'} half"),
                         (beyond and row.volume_ratio > 1.5, 5,
                          f"Bollinger {'upper' if d > 0 else 'lower'}-band breakout with volume > 1.5x")])
    points("volume", [(row.volume_ratio > 1.2 and d * (row.close - row.open) > 0 and d * (row.close - prior.close) > 0, 5,
                       f"{'Up' if d > 0 else 'Down'} candle with volume > 1.2x"),
                      (d * (row.obv - row.obv_ema) > 0, 5, f"OBV {'above' if d > 0 else 'below'} its EMA"),
                      (d * (row.taker_ratio - 0.5) > 0.02, 5,
                       f"Taker {'buy' if d > 0 else 'sell'} pressure (3-candle ratio {row.taker_ratio:.2f})")])
    points("derivatives", [(d * funding < settings.crowded_funding, 5,
                            f"Funding {funding * 100:+.3f}% not crowded against {side}")])
    points("higher_timeframe", [(d * (high.close - high.ema50) > 0, 4, f"{settings.htf} close {'>' if d > 0 else '<'} EMA50"),
                                (d * (high.ema20 - high.ema50) > 0, 3, f"{settings.htf} EMA20 {'>' if d > 0 else '<'} EMA50"),
                                (d * (high.macd - high.macd_signal) > 0, 3, f"{settings.htf} MACD {'>' if d > 0 else '<'} signal")])
    if btc is None or len(f1) <= 72 or len(btc) <= 72:
        available.pop("relative_strength")
        reasons["unavailable"].append("relative_strength (benchmark is BTC itself or history too short)")
    else:
        points("relative_strength", _relative_strength(f1.close.reset_index(drop=True), btc.close.reset_index(drop=True), d))
    components["market_regime"] = {"BULL": 5.0, "NEUTRAL": 2.5, "BEAR": 0.0, "HIGH_VOLATILITY": 0.0}[regime] if d > 0 else \
                                  {"BEAR": 5.0, "NEUTRAL": 2.5, "BULL": 0.0, "HIGH_VOLATILITY": 0.0}[regime]
    (reasons["positive"] if components["market_regime"] >= 2.5 else reasons["negative"]).append(f"BTC regime {regime}")
    if news is None or not news.available:
        available.pop("news")
        reasons["unavailable"].append("news (no recent headline mentions this coin)")
    else:
        components["news"] = round(10 * (0.5 + 0.5 * d * news.sentiment), 4)
        (reasons["positive"] if d * news.sentiment > 0.1 else reasons["negative"]).append(
            f"News sentiment {news.sentiment:+.2f} from {news.count} headline(s)")
    raw, total = sum(components.values()), sum(available.values())
    return {"side": side, "score": round(raw / total * 100, 2), "raw_points": round(raw, 2), "available_points": total,
            "coverage_pct": total, "components": components, "component_maxima": available, "reasons": reasons}


def evaluate(symbol: str, f1: pd.DataFrame, f4: pd.DataFrame, btc: pd.DataFrame | None, regime: str, funding: float,
             last_price: float, news, settings: Settings, capital: float, now_ms: int | None = None) -> dict:
    """Score both sides and pick the better allowed one; every veto is reported as a blocker."""
    for frame, columns, name in ((f1, REQUIRED_1H, settings.timeframe), (f4, REQUIRED_HTF, settings.htf)):
        if not np.isfinite(frame.iloc[-2:][list(columns)].to_numpy(dtype=float)).all():
            raise ValueError(f"Undefined {name} indicators (insufficient history or zero volume)")
    row = f1.iloc[-1]
    atr_pct = float(row.atr / row.close)
    drift = abs(last_price - row.close) / row.atr
    sides = {}
    for side, allowed in (("LONG", settings.allow_long), ("SHORT", settings.allow_short)):
        if not allowed:
            continue
        d = 1 if side == "LONG" else -1
        result = score_side(side, f1, f4, btc, funding, news, regime, settings)
        blockers = []
        if regime == "HIGH_VOLATILITY":
            blockers.append("BTC volatility too high for new entries")
        if d * funding >= settings.extreme_funding:
            blockers.append(f"Extreme funding {funding * 100:+.3f}% against {side}")
        if not settings.min_atr_pct <= atr_pct <= settings.max_atr_pct:
            blockers.append(f"ATR {atr_pct * 100:.2f}% of price outside [{settings.min_atr_pct * 100:.2f}%, {settings.max_atr_pct * 100:.2f}%]")
        if drift > settings.max_drift_atr:
            blockers.append(f"Price moved {drift:.2f} ATR since the signal candle closed")
        if news is not None and news.available and d * news.sentiment <= -settings.news_veto:
            blockers.append(f"Adverse news sentiment {news.sentiment:+.2f}")
        if now_ms is not None and now_ms - int(row.close_time) > 2 * (int(row.close_time) - int(f1.close_time.iloc[-2])):
            blockers.append("Stale candles")
        try:
            result["risk_plan"] = risk_plan(side, float(row.close), float(row.atr), capital, settings)
        except ValueError as error:
            result["risk_plan"] = None
            blockers.append(str(error))
        counter = (side == "LONG" and regime == "BEAR") or (side == "SHORT" and regime == "BULL")
        result["threshold"] = settings.entry_score + (settings.counter_regime_penalty if counter else 0)
        result["blockers"] = blockers
        sides[side] = result
    best = max(sides.values(), key=lambda r: r["score"])
    score = best["score"]
    if best["blockers"] or score < settings.watch_score:
        signal = "NO_TRADE"
    elif score >= max(settings.strong_score, best["threshold"]):
        signal = f"STRONG_{best['side']}"
    elif score >= best["threshold"]:
        signal = f"{best['side']}_CANDIDATE"
    else:
        signal = "WATCH"
    return {"symbol": symbol, "date": str(f1.index[-1]), "price": last_price, "signal": signal, "side": best["side"],
            "score": score, "market_regime": regime, "funding_rate": funding, "atr_pct": atr_pct,
            "news": None if news is None else {"count": news.count, "sentiment": round(news.sentiment, 3),
                                                "headlines": news.headlines},
            "sides": sides, "turnover20": float(row.turnover20),
            "indicators": {k: float(row[k]) for k in ("close", "ema20", "ema50", "ema200", "rsi", "macd", "macd_signal",
                                                       "macd_hist", "bb_pctb", "bb_upper", "bb_lower", "atr", "adx",
                                                       "volume_ratio", "taker_ratio")}}
