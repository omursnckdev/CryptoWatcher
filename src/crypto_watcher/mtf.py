"""Shadow MTF confirmation: does 5m / 1h / 4h momentum agree with a signal's direction?

Frozen definition from the October 2026 study of 7,959 historical signals (2023-2026), where trades whose direction the MTF
rule confirmed did better (2023-24: +0.04R, not significant; 2025-26: +0.23R, 95% interval +0.09..+0.35R). It is RECORDED
for every signal and compared in /atlanan; it does NOT influence any trading decision. Pre-registered rule: after 90 days,
adopt it as a filter only if confirmed signals beat unconfirmed ones with a day-clustered t >= 1.65 and the same sign in
both halves of the period.

Momentum of a timeframe = z(ROC10) + 0.5 * z(MACD histogram), z over that timeframe's last 100 bars. For each of the last
12 five-minute bars (the hour before the signal) a bar confirms LONG when, with the Hurst exponent of the last 200 5m returns
above 0.5, either all three momenta are > 0.5 ("aligned", weight 8) or 4h > 0.8, 1h > 0.5 and |5m| < 0.3 ("early", weight 5);
SHORT mirrored. Higher timeframes are used only once their bar has closed (no look-ahead).
"""
import math
import numpy as np
import pandas as pd

BARS_5M = 400             # 5m history fetched per signal: 12 evaluated bars + 200 returns for Hurst + indicator warm-up
WINDOW = 12               # five-minute bars evaluated = the hour before the signal
Z_WINDOW = 100


def momentum(close: pd.Series, window: int = Z_WINDOW) -> pd.Series:
    roc = close / close.shift(10) - 1
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    z = lambda s: (s - s.rolling(window).mean()) / s.rolling(window).std()
    return z(roc) + 0.5 * z(hist)


def hurst_rs(returns: np.ndarray) -> float:
    """Rescaled-range estimate of the Hurst exponent (NaN when it cannot be estimated)."""
    x = np.asarray(returns, float)
    if len(x) < 30 or not np.isfinite(x).all():
        return float("nan")
    points = []
    for n in (10, 20, 25, 40, 50, 100, 200):
        k = len(x) // n
        if k < 1:
            continue
        chunks = x[:k * n].reshape(k, n)
        dev = np.cumsum(chunks - chunks.mean(axis=1, keepdims=True), axis=1)
        r, s = dev.max(axis=1) - dev.min(axis=1), chunks.std(axis=1)
        ok = s > 0
        if ok.any():
            points.append((n, float(np.mean(r[ok] / s[ok]))))
    if len(points) < 3:
        return float("nan")
    n, v = np.array(points).T
    return float(np.polyfit(np.log(n), np.log(v), 1)[0])


def on_grid(frame: pd.DataFrame, decision_ms: np.ndarray) -> np.ndarray:
    """Momentum of a timeframe's CLOSED bars, read at each decision time (ms)."""
    m = momentum(frame.close).to_numpy()
    ends = frame.close_time.to_numpy() + 1
    pos = np.searchsorted(ends, decision_ms, side="right") - 1           # last bar whose end <= decision time
    out = np.full(len(decision_ms), np.nan)
    ok = pos >= 0
    out[ok] = m[pos[ok]]
    return out


def bar_score(m5: float, m1h: float, m4h: float, hurst: float) -> int:
    """+8 aligned long / +5 early long, negative for short; 0 outside a trending (Hurst > 0.5) regime."""
    if not (math.isfinite(hurst) and hurst > 0.5) or not all(math.isfinite(v) for v in (m5, m1h, m4h)):
        return 0
    score = 0
    if m5 > 0.5 and m1h > 0.5 and m4h > 0.5:
        score += 8
    if m5 < -0.5 and m1h < -0.5 and m4h < -0.5:
        score -= 8
    if m4h > 0.8 and m1h > 0.5 and abs(m5) < 0.3:
        score += 5
    if m4h < -0.8 and m1h < -0.5 and abs(m5) < 0.3:
        score -= 5
    return score


def confirmation(f5: pd.DataFrame, f1h: pd.DataFrame, f4h: pd.DataFrame, hurst_fn=hurst_rs) -> dict:
    """-> {"dir": +1 / -1 / 0, "m5", "m1h", "m4h", "hurst"} from closed 5m / 1h / 4h candles (with close_time columns)."""
    if len(f5) < WINDOW + 200 + 1:
        raise ValueError(f"only {len(f5)} five-minute bars")
    decision = f5.close_time.to_numpy()[-WINDOW:] + 1
    m5 = momentum(f5.close).to_numpy()[-WINDOW:]
    m1h, m4h = on_grid(f1h, decision), on_grid(f4h, decision)
    r = np.log(f5.close).diff().to_numpy()
    long_hit = short_hit = False
    hurst_last = float("nan")
    for i in range(WINDOW):
        end = len(r) - WINDOW + i + 1                                   # returns up to and including this bar
        h = hurst_fn(r[end - 200:end])
        hurst_last = h
        s = bar_score(m5[i], m1h[i], m4h[i], h)
        long_hit |= s > 0
        short_hit |= s < 0
    direction = 1 if long_hit and not short_hit else -1 if short_hit and not long_hit else 0
    rnd = lambda v: None if not math.isfinite(v) else round(float(v), 3)
    return {"dir": direction, "m5": rnd(m5[-1]), "m1h": rnd(m1h[-1]), "m4h": rnd(m4h[-1]), "hurst": rnd(hurst_last)}


def fetch_confirmation(provider, symbol: str) -> dict:
    """Read 5m / 1h / 4h closed candles from the (mainnet) market-data provider and evaluate the rule."""
    return confirmation(provider.klines(symbol, "5m", BARS_5M), provider.klines(symbol, "1h", 300), provider.klines(symbol, "4h", 300))


def cluster_diff(values: list[float], groups: list[bool], days: list[str]) -> tuple[float, float]:
    """Difference of means (group True minus group False) and its day-clustered t (regression with a sandwich variance)."""
    y = np.asarray(values, float)
    x = np.asarray(groups, float)
    if len(y) < 10 or x.min() == x.max():
        return float("nan"), float("nan")
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta
    bread = np.linalg.inv(X.T @ X)
    meat = np.zeros((2, 2))
    for d in set(days):
        idx = [i for i, dd in enumerate(days) if dd == d]
        s = X[idx].T @ e[idx]
        meat += np.outer(s, s)
    var = bread @ meat @ bread
    se = math.sqrt(var[1, 1]) if var[1, 1] > 0 else float("nan")
    return float(beta[1]), float(beta[1] / se) if se == se and se > 0 else float("nan")
