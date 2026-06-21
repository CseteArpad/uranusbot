"""
RP Engine — Range Position per replay row.

RP = (price - range_low) / (range_high - range_low)

  RP = 0.00  -> bottom of range
  RP = 0.50  -> mid-range
  RP = 1.00  -> top of range
  RP < 0.00  -> below range
  RP > 1.00  -> above range

Price used: next_close (the candle being evaluated against the range).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_rp(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds a 'rp' column to a replay result DataFrame.
    Requires: next_close, range_low, range_high.
    Returns a copy.
    """
    df = df.copy()
    width = df["range_high"] - df["range_low"]
    # Guard against zero-width range (shouldn't happen after replay_validator checks)
    rp = np.where(
        width > 0,
        (df["next_close"].to_numpy() - df["range_low"].to_numpy()) / width.to_numpy(),
        np.nan,
    )
    df["rp"] = np.round(rp, 6)
    return df
