"""
Zone Engine — FAZIS 3 audit classifier.

No orders. No live trading. No Binance/Freqtrade calls.
candidate_signal is an audit label only.

Zone boundaries:
  BELOW_RANGE : rpagg < 0.00
  LOW_ZONE    : 0.00 <= rpagg <= 0.20
  MID_ZONE    : 0.20 <  rpagg <  0.80
  HIGH_ZONE   : 0.80 <= rpagg <= 1.00
  ABOVE_RANGE : rpagg > 1.00
  NO_DATA     : rpagg missing

Candidate logic:
  LOW_ZONE   + UPWARD_BIAS   -> BUY_CANDIDATE  / low_zone_upward_bias
  BELOW_RANGE + UPWARD_BIAS  -> BUY_CANDIDATE  / below_range_recovery
  HIGH_ZONE  + DOWNWARD_BIAS -> SELL_CANDIDATE / high_zone_downward_bias
  ABOVE_RANGE + DOWNWARD_BIAS-> SELL_CANDIDATE / above_range_reversal
  anything else              -> NEUTRAL        / no_signal
"""
from __future__ import annotations


def classify_zone(rpagg) -> str:
    """Return zone label for a single rpagg value (float or empty string)."""
    if rpagg == "" or rpagg is None:
        return "NO_DATA"
    try:
        v = float(rpagg)
    except (TypeError, ValueError):
        return "NO_DATA"

    if v < 0.00:
        return "BELOW_RANGE"
    if v <= 0.20:
        return "LOW_ZONE"
    if v < 0.80:
        return "MID_ZONE"
    if v <= 1.00:
        return "HIGH_ZONE"
    return "ABOVE_RANGE"


def classify_candidate(zone: str, bias: str) -> tuple[str, str]:
    """
    Return (candidate_signal, signal_reason).
    Pure function — no side effects, no orders.
    """
    if zone in ("LOW_ZONE",) and bias == "UPWARD_BIAS":
        return "BUY_CANDIDATE", "low_zone_upward_bias"
    if zone == "BELOW_RANGE" and bias == "UPWARD_BIAS":
        return "BUY_CANDIDATE", "below_range_recovery"
    if zone == "HIGH_ZONE" and bias == "DOWNWARD_BIAS":
        return "SELL_CANDIDATE", "high_zone_downward_bias"
    if zone == "ABOVE_RANGE" and bias == "DOWNWARD_BIAS":
        return "SELL_CANDIDATE", "above_range_reversal"
    return "NEUTRAL", "no_signal"
