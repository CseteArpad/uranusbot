"""
Pivot Engine — confirmed pivot highs and lows using rolling window comparisons.

Vectorised with NumPy rolling min/max — O(N), no Python loop over candles.

Rules:
  Pivot LOW  [i]: low[i]  < min(low[i-left : i])   AND low[i]  <= min(low[i+1 : i+right+1])
  Pivot HIGH [i]: high[i] > max(high[i-left : i])  AND high[i] >= max(high[i+1 : i+right+1])
"""
import numpy as np
import pandas as pd


def find_pivots(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """
    Returns a copy of df with two new bool columns:
      pivot_low  — confirmed pivot low
      pivot_high — confirmed pivot high
    """
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Pivot Engine: missing columns {missing}")

    df = df.copy().reset_index(drop=True)

    low = df["low"].to_numpy(dtype=np.float64)
    high = df["high"].to_numpy(dtype=np.float64)
    n = len(low)

    # --- Rolling lookback (past `left` bars, exclusive of current) ---
    # prev_min_low[i]  = min(low[i-left : i])
    # prev_max_high[i] = max(high[i-left : i])
    # Use pandas rolling with min_periods=left, then shift(1) to exclude current bar.
    low_s = pd.Series(low)
    high_s = pd.Series(high)

    prev_min_low = low_s.rolling(left, min_periods=left).min().shift(1).to_numpy()
    prev_max_high = high_s.rolling(left, min_periods=left).max().shift(1).to_numpy()

    # --- Rolling lookahead (next `right` bars, exclusive of current) ---
    # Reverse the series, compute rolling min/max, then reverse back and shift.
    next_min_low = (
        low_s[::-1].rolling(right, min_periods=right).min().shift(1)[::-1].to_numpy()
    )
    next_max_high = (
        high_s[::-1].rolling(right, min_periods=right).max().shift(1)[::-1].to_numpy()
    )

    # Mask positions where the lookback / lookahead window is incomplete
    valid = np.ones(n, dtype=bool)
    valid[: left] = False       # need `left` bars before
    valid[n - right :] = False  # need `right` bars after

    pivot_low = (
        valid &
        ~np.isnan(prev_min_low) &
        ~np.isnan(next_min_low) &
        (low < prev_min_low) &
        (low <= next_min_low)
    )

    pivot_high = (
        valid &
        ~np.isnan(prev_max_high) &
        ~np.isnan(next_max_high) &
        (high > prev_max_high) &
        (high >= next_max_high)
    )

    df["pivot_low"] = pivot_low
    df["pivot_high"] = pivot_high
    return df
