"""
ATR Engine — ATR(period) using Wilder's smoothing (RMA).
True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
"""
import pandas as pd


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Returns a Series of ATR values aligned to df's index.
    First (period) values will be NaN.
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder's RMA (same as pandas ewm with alpha=1/period, adjust=False)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def calculate_atr_series(df: pd.DataFrame, period: int = 14) -> "pd.Series":
    """Alias used by main.py replay loop."""
    return compute_atr(df, period=period)
