# app/channel_engine.py
# Pivot-based parallel price channel engine.
# Pure functions — no imports from live bot code, no real orders.

from __future__ import annotations

from typing import List, Tuple

TOUCH_TOL = 0.003   # pivot within 0.3 % of line price counts as a touch
PEN_TOL   = 0.001   # candle beyond 0.1 % of line price counts as a penetration


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def detect_pivot_lows(lows: List[float], left: int = 3, right: int = 3) -> List[int]:
    """
    Return indices of pivot lows.
    Index i is a pivot low when every bar in [i-left, i-1] and [i+1, i+right]
    has a strictly higher low value than lows[i].
    """
    result: List[int] = []
    n = len(lows)
    for i in range(left, n - right):
        v = lows[i]
        if (all(lows[j] > v for j in range(i - left, i)) and
                all(lows[j] > v for j in range(i + 1, i + right + 1))):
            result.append(i)
    return result


def detect_pivot_highs(highs: List[float], left: int = 3, right: int = 3) -> List[int]:
    """
    Return indices of pivot highs.
    Index i is a pivot high when every bar in [i-left, i-1] and [i+1, i+right]
    has a strictly lower high value than highs[i].
    """
    result: List[int] = []
    n = len(highs)
    for i in range(left, n - right):
        v = highs[i]
        if (all(highs[j] < v for j in range(i - left, i)) and
                all(highs[j] < v for j in range(i + 1, i + right + 1))):
            result.append(i)
    return result


# ---------------------------------------------------------------------------
# Channel building
# ---------------------------------------------------------------------------

def _invalid(reason: str) -> dict:
    return {
        "valid":             False,
        "reason":            reason,
        "lower_slope":       0.0,
        "lower_intercept":   0.0,
        "upper_slope":       0.0,
        "upper_intercept":   0.0,
        "anchor_idx1":       0,
        "anchor_idx2":       0,
        "anchor_price1":     0.0,
        "anchor_price2":     0.0,
        "touch_count_lower": 0,
        "touch_count_upper": 0,
        "penetration_count": 0,
        "score":             0.0,
        "width_at_end":      0.0,
        "n_bars":            0,
    }


def build_parallel_channel(
    highs: List[float],
    lows:  List[float],
    left:  int = 3,
    right: int = 3,
) -> dict:
    """
    Build a pivot-based parallel price channel from OHLCV low/high arrays.

    Lower line  — fitted through the two most recent pivot lows.
    Upper line  — parallel to lower, offset to pass through the pivot high
                  that has the greatest vertical distance above the lower line.

    Returns a channel dict with 'valid' = True on success, False otherwise.
    Negative CP values (price below lower band) and CP > 1 (above upper band)
    are fully supported by channel_position(); they are NOT errors.
    """
    n = len(lows)
    if len(highs) != n:
        return _invalid("highs_lows_length_mismatch")
    if n < left + right + 2:
        return _invalid("insufficient_bars")

    pl = detect_pivot_lows(lows,   left, right)
    ph = detect_pivot_highs(highs, left, right)

    if len(pl) < 2:
        return _invalid("insufficient_pivot_lows")
    if len(ph) < 1:
        return _invalid("no_pivot_highs")

    # Lower line anchored on the two most recent pivot lows
    idx1, idx2 = pl[-2], pl[-1]
    p1,   p2   = lows[idx1], lows[idx2]

    if idx1 == idx2:
        return _invalid("duplicate_anchor")

    slope     = (p2 - p1) / (idx2 - idx1)
    intercept = p1 - slope * idx1

    # Offset upper line to the highest pivot high above the lower line
    max_offset = 0.0
    for i in ph:
        lo_at_i = slope * i + intercept
        offset  = highs[i] - lo_at_i
        if offset > max_offset:
            max_offset = offset

    if max_offset <= 0.0:
        return _invalid("no_pivot_high_above_lower_line")

    upper_intercept = intercept + max_offset

    # Degenerate width check at last bar
    lo_end = slope * (n - 1) + intercept
    up_end = slope * (n - 1) + upper_intercept
    width  = up_end - lo_end
    if width <= 0.0:
        return _invalid("non_positive_width")

    # Scoring: count touches and penetrations
    touch_lower = 0
    for i in pl:
        line = slope * i + intercept
        if line > 0 and abs(lows[i] - line) / line <= TOUCH_TOL:
            touch_lower += 1

    touch_upper = 0
    for i in ph:
        line = slope * i + upper_intercept
        if line > 0 and abs(highs[i] - line) / line <= TOUCH_TOL:
            touch_upper += 1

    penetrations = 0
    for i in range(n):
        lo_line = slope * i + intercept
        up_line = slope * i + upper_intercept
        if lo_line > 0 and lows[i]  < lo_line * (1.0 - PEN_TOL):
            penetrations += 1
        if up_line > 0 and highs[i] > up_line * (1.0 + PEN_TOL):
            penetrations += 1

    score = (touch_lower + touch_upper) * 2.0 - penetrations * 0.3

    ch = _invalid("")
    ch.update({
        "valid":             True,
        "reason":            "",
        "lower_slope":       slope,
        "lower_intercept":   intercept,
        "upper_slope":       slope,          # parallel — same slope
        "upper_intercept":   upper_intercept,
        "anchor_idx1":       idx1,
        "anchor_idx2":       idx2,
        "anchor_price1":     p1,
        "anchor_price2":     p2,
        "touch_count_lower": touch_lower,
        "touch_count_upper": touch_upper,
        "penetration_count": penetrations,
        "score":             score,
        "width_at_end":      width,
        "n_bars":            n,
    })
    return ch


# ---------------------------------------------------------------------------
# Channel evaluation helpers
# ---------------------------------------------------------------------------

def evaluate_channel_at(ch: dict, idx: int) -> Tuple[float, float]:
    """Return (lower_price, upper_price) of the channel at bar index `idx`."""
    lower = ch["lower_slope"] * idx + ch["lower_intercept"]
    upper = ch["upper_slope"] * idx + ch["upper_intercept"]
    return lower, upper


def channel_position(price: float, lower: float, upper: float) -> float:
    """
    CP = (price - lower) / (upper - lower).

    Values below 0 (price below the lower band) and above 1 (above upper band)
    are intentionally allowed.  Raises ValueError only for a degenerate channel
    where upper <= lower.
    """
    width = upper - lower
    if width <= 0.0:
        raise ValueError(f"degenerate channel: upper={upper} <= lower={lower}")
    return (price - lower) / width
