"""
Replay Validator — tick-by-tick range validation, no buy/sell logic.

For each candle i (with a valid range):
  - Looks at next candle's close (i+1)
  - HIT      : range_low <= next_close <= range_high
  - MISS     : otherwise
  - Overshoot: distance outside the range (0 if HIT)

Performance: fully vectorised with NumPy — no Python loop over candles.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.pivot_engine import find_pivots
from app.swing_engine import build_swing_arrays
from app.atr_engine import compute_atr


def run_replay(
    df: pd.DataFrame,
    pivot_left: int = 3,
    pivot_right: int = 3,
    atr_period: int = 14,
    atr_multiplier: float = 0.5,
) -> pd.DataFrame:
    """
    Returns a DataFrame with one row per evaluated candle:
      timestamp, close, swing_low, swing_high, atr,
      range_low, range_high, range_width, next_close, hit, overshoot
    """
    df = find_pivots(df, left=pivot_left, right=pivot_right)

    # --- O(N) precomputation ---
    atr_arr = compute_atr(df, period=atr_period).to_numpy(dtype=np.float64)
    swing_low_arr, swing_high_arr = build_swing_arrays(df)

    closes = df["close"].to_numpy(dtype=np.float64)
    timestamps = df["timestamp"].to_numpy()

    warmup = max(pivot_left + pivot_right, atr_period)
    n = len(df)

    # Candidate index range: [warmup, n-2] (need i+1 to exist)
    idx = np.arange(warmup, n - 1)
    if len(idx) == 0:
        return _empty_df()

    sl = swing_low_arr[idx]
    sh = swing_high_arr[idx]
    atr = atr_arr[idx]

    # Valid mask: no NaN, atr > 0, swing_low < swing_high
    valid = (
        ~np.isnan(sl) &
        ~np.isnan(sh) &
        ~np.isnan(atr) &
        (atr > 0) &
        (sl < sh)
    )

    idx = idx[valid]
    sl = sl[valid]
    sh = sh[valid]
    atr = atr[valid]

    if len(idx) == 0:
        return _empty_df()

    half_atr = atr_multiplier * atr
    range_low = sl - half_atr
    range_high = sh + half_atr
    range_width = range_high - range_low

    next_close = closes[idx + 1]

    hit = (next_close >= range_low) & (next_close <= range_high)

    overshoot = np.where(
        next_close > range_high,
        next_close - range_high,
        np.where(next_close < range_low, range_low - next_close, 0.0),
    )

    return pd.DataFrame({
        "timestamp": timestamps[idx],
        "close": closes[idx],
        "swing_low": sl,
        "swing_high": sh,
        "atr": atr,
        "range_low": range_low,
        "range_high": range_high,
        "range_width": range_width,
        "next_close": next_close,
        "hit": hit,
        "overshoot": overshoot,
    })


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "timestamp", "close", "swing_low", "swing_high", "atr",
        "range_low", "range_high", "range_width", "next_close", "hit", "overshoot",
    ])
