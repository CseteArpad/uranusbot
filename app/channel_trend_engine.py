# app/channel_trend_engine.py
# Trend scoring and signal engine — no live trading, no orders.
# Consumes per-timeframe CP values from channel_engine and returns
# human-readable trend state, trend score, and candidate action.

from __future__ import annotations

from typing import Optional

# Trend score weights (4H + 12H only; 1D is informational)
TREND_SCORE_W4H  = 0.4
TREND_SCORE_W12H = 0.6

# TrendScore thresholds for TrendState classification
LONG_THRESHOLD  = 0.65
SHORT_THRESHOLD = 0.35

# CPagg thresholds for candidate action
BUY_ZONE_THRESHOLD  = 0.20
SELL_ZONE_THRESHOLD = 0.80


def compute_trend_score(
    cp_4h: Optional[float],
    cp_12h: Optional[float],
) -> Optional[float]:
    """
    TrendScore = 0.4 * CP_4H + 0.6 * CP_12H

    Returns None if either input is missing.
    Negative values (price below channel lower band) and values > 1
    (above upper band) are fully valid.
    """
    if cp_4h is None or cp_12h is None:
        return None
    return TREND_SCORE_W4H * cp_4h + TREND_SCORE_W12H * cp_12h


def compute_trend_state(
    cp_4h: Optional[float],
    cp_12h: Optional[float],
) -> str:
    """
    Classify trend direction via TrendScore = 0.4*CP_4H + 0.6*CP_12H.

    Returns one of: LONG | SHORT | SIDEWAYS | UNKNOWN

    LONG     — TrendScore >= 0.65
    SHORT    — TrendScore <= 0.35
    SIDEWAYS — 0.35 < TrendScore < 0.65
    UNKNOWN  — either CP is None (TrendScore cannot be computed)
    """
    score = compute_trend_score(cp_4h, cp_12h)
    if score is None:
        return "UNKNOWN"
    if score >= LONG_THRESHOLD:
        return "LONG"
    if score <= SHORT_THRESHOLD:
        return "SHORT"
    return "SIDEWAYS"


def compute_candidate_action(cpagg: Optional[float]) -> str:
    """
    Map the aggregate channel position (CPagg) to a candidate trading zone.

    Returns one of: BUY_ZONE | SELL_ZONE | HOLD_ZONE | UNKNOWN

    BUY_ZONE  — CPagg <= 0.20  (price near the lower band aggregate)
    SELL_ZONE — CPagg >= 0.80  (price near the upper band aggregate)
    HOLD_ZONE — 0.20 < CPagg < 0.80
    UNKNOWN   — CPagg is None (no valid channels available)
    """
    if cpagg is None:
        return "UNKNOWN"
    if cpagg <= BUY_ZONE_THRESHOLD:
        return "BUY_ZONE"
    if cpagg >= SELL_ZONE_THRESHOLD:
        return "SELL_ZONE"
    return "HOLD_ZONE"
