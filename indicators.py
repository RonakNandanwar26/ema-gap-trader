"""indicators.py — EMA 9/21, SuperTrend, RSI(14), EMA Gap %.

No external TA library — inline computation matches test_rsi_ema_gap.py
for exact number consistency with backtest results.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Compute SuperTrend indicator. Returns (supertrend_series, direction_series)."""
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(df)

    # True Range
    tr = [high[0] - low[0]]
    for i in range(1, n):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))

    # ATR
    atr = [None] * n
    for i in range(period - 1, n):
        atr[i] = sum(tr[i - period + 1 : i + 1]) / period

    # Basic bands
    upper, lower = [None] * n, [None] * n
    for i in range(n):
        if atr[i] is not None:
            hl2 = (high[i] + low[i]) / 2
            upper[i] = hl2 + multiplier * atr[i]
            lower[i] = hl2 - multiplier * atr[i]

    # SuperTrend computation
    st, direction = [None] * n, [0] * n
    first = period - 1
    if first >= n:
        return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)
    st[first] = upper[first]
    direction[first] = -1

    for i in range(first + 1, n):
        if upper[i] is None:
            st[i] = st[i - 1]
            direction[i] = direction[i - 1]
            continue

        # Final lower band
        if not (lower[i] > (lower[i - 1] or 0)):
            if close[i - 1] >= (lower[i - 1] or 0):
                lower[i] = max(lower[i], lower[i - 1] or 0)
        # Final upper band
        if not (upper[i] < (upper[i - 1] or float("inf"))):
            if close[i - 1] <= (upper[i - 1] or float("inf")):
                upper[i] = min(upper[i], upper[i - 1] or float("inf"))

        # Direction
        if direction[i - 1] == 1:
            if close[i] < lower[i]:
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = lower[i]
        else:
            if close[i] > upper[i]:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = upper[i]

    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns: ema9, ema21, st, st_dir, ema_gap_pct, ema_gap_expanding, rsi."""
    # EMA 9/21
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

    # SuperTrend
    df["st"], df["st_dir"] = compute_supertrend(df)

    # EMA Gap %
    df["ema_gap_pct"] = (df["ema9"] - df["ema21"]).abs() / df["ema21"].replace(0, np.nan) * 100
    df["ema_gap_pct"] = df["ema_gap_pct"].fillna(0)
    df["ema_gap_expanding"] = df["ema_gap_pct"] > df["ema_gap_pct"].shift(1)

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss_s = delta.where(delta < 0, 0).abs().rolling(14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss_s
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    return df
