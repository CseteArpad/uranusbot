"""
Range Engine — builds the price range from swing levels and ATR buffer.
  RangeLow  = SwingLow  - atr_multiplier * ATR
  RangeHigh = SwingHigh + atr_multiplier * ATR
  RangeWidth = RangeHigh - RangeLow
"""


def compute_range(swing_low: float, swing_high: float, atr: float, multiplier: float = 0.5) -> dict:
    """
    All inputs must be valid floats (caller is responsible for None checks).
    """
    range_low = swing_low - multiplier * atr
    range_high = swing_high + multiplier * atr
    range_width = range_high - range_low

    return {
        "range_low": range_low,
        "range_high": range_high,
        "range_width": range_width,
    }
