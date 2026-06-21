"""
Swing Engine V1 — last confirmed pivot low/high.

Two modes:
  get_swings(df)         — single lookup on a pre-filtered window (kept for compatibility)
  build_swing_arrays(df) — O(N) forward pass; returns arrays for vectorised replay
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def get_swings(df: pd.DataFrame) -> dict:
    """Single-window lookup (used in tests / one-off calls)."""
    lows = df[df["pivot_low"]]
    highs = df[df["pivot_high"]]

    swing_low = float(lows["low"].iloc[-1]) if not lows.empty else None
    swing_low_idx = int(lows.index[-1]) if not lows.empty else None

    swing_high = float(highs["high"].iloc[-1]) if not highs.empty else None
    swing_high_idx = int(highs.index[-1]) if not highs.empty else None

    return {
        "swing_low": swing_low,
        "swing_high": swing_high,
        "swing_low_idx": swing_low_idx,
        "swing_high_idx": swing_high_idx,
    }


def build_swing_arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Single O(N) forward pass over a DataFrame that already has pivot_low / pivot_high columns.

    Returns (swing_low_arr, swing_high_arr) where each element [i] holds
    the most recent confirmed swing value visible at candle i (NaN if none yet).
    """
    n = len(df)
    swing_low_arr = np.full(n, np.nan)
    swing_high_arr = np.full(n, np.nan)

    cur_low = np.nan
    cur_high = np.nan

    pivot_low_vals = df["pivot_low"].to_numpy()
    pivot_high_vals = df["pivot_high"].to_numpy()
    lows = df["low"].to_numpy()
    highs = df["high"].to_numpy()

    for i in range(n):
        if pivot_low_vals[i]:
            cur_low = lows[i]
        if pivot_high_vals[i]:
            cur_high = highs[i]
        swing_low_arr[i] = cur_low
        swing_high_arr[i] = cur_high

    return swing_low_arr, swing_high_arr
