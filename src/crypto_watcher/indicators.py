"""Indicators on closed OHLCV candles. Every value at row t uses only rows <= t."""
import numpy as np
import pandas as pd


def wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """SMA-seeded Wilder smoothing; the warm-up stays NaN."""
    values = series.to_numpy(dtype=float)
    result = np.full(len(values), np.nan)
    previous = np.nan
    for i in range(period - 1, len(values)):
        if not np.isfinite(values[i]):
            previous = np.nan
        elif not np.isfinite(previous):
            window = values[i - period + 1:i + 1]
            if np.isfinite(window).all():
                previous = float(window.mean())
        else:
            previous = (previous * (period - 1) + values[i]) / period
        result[i] = previous
    return pd.Series(result, index=series.index)


def calculate(bars: pd.DataFrame) -> pd.DataFrame:
    """Expects columns open, high, low, close, volume, quote_volume, taker_buy_base."""
    df = bars.copy()
    close = df.close
    for period in (20, 50, 100, 200):
        df[f"ema{period}"] = close.ewm(span=period, adjust=False, min_periods=period).mean()
    change = close.diff()
    gain, loss = wilder(change.clip(lower=0)), wilder(-change.clip(upper=0))
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    df.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    df.loc[(loss == 0) & (gain == 0), "rsi"] = 50
    fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
    slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
    df["macd"] = fast - slow
    df["macd_signal"] = df.macd.ewm(span=9, adjust=False, min_periods=9).mean()
    df["macd_hist"] = df.macd - df.macd_signal
    df["bb_middle"] = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    df["bb_upper"], df["bb_lower"] = df.bb_middle + 2 * std, df.bb_middle - 2 * std
    band = (df.bb_upper - df.bb_lower).replace(0, np.nan)
    df["bb_pctb"] = ((close - df.bb_lower) / band).fillna(0.5)
    df["bb_width"] = (band / df.bb_middle).fillna(0.0)
    tr = pd.concat([df.high - df.low, (df.high - close.shift()).abs(), (df.low - close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = wilder(tr)
    up, down = df.high.diff(), -df.low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    plus.iloc[0] = minus.iloc[0] = np.nan
    atr = df.atr.replace(0, np.nan)
    df["pdi"], df["mdi"] = (100 * wilder(plus) / atr).fillna(0.0), (100 * wilder(minus) / atr).fillna(0.0)
    denominator = df.pdi + df.mdi
    dx = (100 * (df.pdi - df.mdi).abs() / denominator.replace(0, np.nan)).fillna(0.0)
    df["adx"] = wilder(dx.where(df.atr.notna()))
    # Baseline is the previous 20 candles so a spike does not dilute itself.
    df["volume_ratio"] = df.volume / df.volume.shift().rolling(20).mean().replace(0, np.nan)
    df["turnover20"] = df.quote_volume.rolling(20).mean()
    obv = (np.sign(change.fillna(0.0)) * df.volume).cumsum()
    df["obv"], df["obv_ema"] = obv, obv.ewm(span=20, adjust=False).mean()
    df["taker_ratio"] = (df.taker_buy_base.rolling(3).sum() / df.volume.rolling(3).sum().replace(0, np.nan))
    df["trend_persistence_up"] = (close > df.ema50).astype(float).rolling(10).mean()
    df["trend_persistence_down"] = (close < df.ema50).astype(float).rolling(10).mean()
    return df
