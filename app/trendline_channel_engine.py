# app/trendline_channel_engine.py
# Dual trendline channel engine — FÁZIS 3E.
# Upper and lower bounds are independent trendlines, NOT parallel.
# Lower trendline fitted to pivot lows; upper trendline fitted to pivot highs.
# Pure functions — no live trading, no orders, no state.json access.

from __future__ import annotations

from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Invalid reason constants
# ---------------------------------------------------------------------------

REASON_INSUFFICIENT_PIVOT_LOWS    = "insufficient_pivot_lows"
REASON_INSUFFICIENT_PIVOT_HIGHS   = "insufficient_pivot_highs"
REASON_UPPER_BELOW_OR_EQUAL_LOWER = "upper_below_or_equal_lower"
REASON_DEGENERATE_WIDTH           = "degenerate_width"
REASON_OTHER_EXCEPTION            = "other_exception"

ALL_INVALID_REASONS: List[str] = [
    REASON_INSUFFICIENT_PIVOT_LOWS,
    REASON_INSUFFICIENT_PIVOT_HIGHS,
    REASON_UPPER_BELOW_OR_EQUAL_LOWER,
    REASON_DEGENERATE_WIDTH,
    REASON_OTHER_EXCEPTION,
]

MIN_WIDTH = 1e-8   # degenerate channel guard


# ---------------------------------------------------------------------------
# Pivot detection  (same strict-inequality logic as channel_engine.py)
# ---------------------------------------------------------------------------

def detect_pivot_lows(lows: List[float], left: int = 3, right: int = 3) -> List[int]:
    """Return indices where lows[i] is strictly less than all neighbours within left/right."""
    n = len(lows)
    pivots: List[int] = []
    for i in range(left, n - right):
        v = lows[i]
        if (all(v < lows[i - j] for j in range(1, left + 1)) and
                all(v < lows[i + j] for j in range(1, right + 1))):
            pivots.append(i)
    return pivots


def detect_pivot_highs(highs: List[float], left: int = 3, right: int = 3) -> List[int]:
    """Return indices where highs[i] is strictly greater than all neighbours within left/right."""
    n = len(highs)
    pivots: List[int] = []
    for i in range(left, n - right):
        v = highs[i]
        if (all(v > highs[i - j] for j in range(1, left + 1)) and
                all(v > highs[i + j] for j in range(1, right + 1))):
            pivots.append(i)
    return pivots


# ---------------------------------------------------------------------------
# Linear regression helper
# ---------------------------------------------------------------------------

def _fit_line(x_vals: List[float], y_vals: List[float]) -> Tuple[float, float]:
    """
    Ordinary least squares linear fit.
    Returns (slope, intercept) for y = slope * x + intercept.
    Falls back to a flat line through the mean when all x values are identical.
    """
    n = len(x_vals)
    if n == 1:
        return 0.0, y_vals[0]
    sx  = sum(x_vals)
    sy  = sum(y_vals)
    sxy = sum(xi * yi for xi, yi in zip(x_vals, y_vals))
    sx2 = sum(xi * xi for xi in x_vals)
    denom = n * sx2 - sx * sx
    if abs(denom) < 1e-12:
        return 0.0, sy / n
    slope     = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _eval_line(slope: float, intercept: float, idx: float) -> float:
    return slope * idx + intercept


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _invalid(
    reason: str,
    pl_count: int = 0,
    ph_count: int = 0,
    lower_slope: Optional[float] = None,
    lower_intercept: Optional[float] = None,
    upper_slope: Optional[float] = None,
    upper_intercept: Optional[float] = None,
) -> dict:
    return {
        "valid":             False,
        "reason":            reason,
        "pivot_low_count":   pl_count,
        "pivot_high_count":  ph_count,
        "lower_slope":       lower_slope,
        "lower_intercept":   lower_intercept,
        "upper_slope":       upper_slope,
        "upper_intercept":   upper_intercept,
        "lower_now":         None,
        "upper_now":         None,
        "width_now":         None,
        "cp":                None,
        "used_low_indices":  [],
        "used_high_indices": [],
        "n_bars":            0,
    }


# ---------------------------------------------------------------------------
# Core channel builder
# ---------------------------------------------------------------------------

def build_trendline_channel(
    highs:      List[float],
    lows:       List[float],
    left:       int = 3,
    right:      int = 3,
    max_pivots: int = 6,
) -> dict:
    """
    Fit two independent trendlines to the last *max_pivots* pivot lows / highs
    and return a channel evaluated at the current bar (index n-1).

    Parameters
    ----------
    highs, lows : bar arrays (completed bars only — no look-ahead).
    left, right : pivot detection window width on each side.
    max_pivots  : how many of the most-recent pivot points to use per side.

    Returns
    -------
    dict with keys: valid, reason, lower_slope/intercept, upper_slope/intercept,
    lower_now, upper_now, width_now, cp (None — call channel_position()),
    pivot_low_count, pivot_high_count, used_low_indices, used_high_indices, n_bars.
    """
    try:
        n = len(highs)

        pl_idxs  = detect_pivot_lows(lows,  left, right)
        ph_idxs  = detect_pivot_highs(highs, left, right)
        pl_count = len(pl_idxs)
        ph_count = len(ph_idxs)

        if pl_count < 2:
            return _invalid(REASON_INSUFFICIENT_PIVOT_LOWS, pl_count, ph_count)
        if ph_count < 2:
            return _invalid(REASON_INSUFFICIENT_PIVOT_HIGHS, pl_count, ph_count)

        # Use the most-recent K pivots (chronological order preserved)
        used_low_idxs  = pl_idxs[-max_pivots:]
        used_high_idxs = ph_idxs[-max_pivots:]

        # Fit lower trendline through selected pivot lows
        low_x = [float(i) for i in used_low_idxs]
        low_y = [lows[i]  for i in used_low_idxs]
        lower_slope, lower_intercept = _fit_line(low_x, low_y)

        # Fit upper trendline through selected pivot highs
        high_x = [float(i) for i in used_high_idxs]
        high_y = [highs[i] for i in used_high_idxs]
        upper_slope, upper_intercept = _fit_line(high_x, high_y)

        # Evaluate both lines at the current bar
        cur_idx   = float(n - 1)
        lower_now = _eval_line(lower_slope, lower_intercept, cur_idx)
        upper_now = _eval_line(upper_slope, upper_intercept, cur_idx)

        if upper_now <= lower_now:
            return _invalid(
                REASON_UPPER_BELOW_OR_EQUAL_LOWER, pl_count, ph_count,
                lower_slope, lower_intercept, upper_slope, upper_intercept,
            )

        width_now = upper_now - lower_now
        if width_now < MIN_WIDTH:
            return _invalid(REASON_DEGENERATE_WIDTH, pl_count, ph_count)

        return {
            "valid":             True,
            "reason":            None,
            "pivot_low_count":   pl_count,
            "pivot_high_count":  ph_count,
            "lower_slope":       lower_slope,
            "lower_intercept":   lower_intercept,
            "upper_slope":       upper_slope,
            "upper_intercept":   upper_intercept,
            "lower_now":         lower_now,
            "upper_now":         upper_now,
            "width_now":         width_now,
            "cp":                None,
            "used_low_indices":  used_low_idxs,
            "used_high_indices": used_high_idxs,
            "n_bars":            n,
        }

    except Exception:
        return _invalid(REASON_OTHER_EXCEPTION)


# ---------------------------------------------------------------------------
# Channel position
# ---------------------------------------------------------------------------

def channel_position(price: float, lower: float, upper: float) -> float:
    """
    Return normalised position of *price* within [lower, upper].
    0 = on lower line, 1 = on upper line.
    Values outside [0, 1] are valid (price outside channel).
    Raises ValueError on degenerate channel.
    """
    if upper <= lower:
        raise ValueError(f"upper ({upper}) <= lower ({lower}): degenerate channel")
    return (price - lower) / (upper - lower)
