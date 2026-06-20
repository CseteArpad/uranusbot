# app/channel_decision_engine.py
# Channel Decision Engine — FÁZIS 3A.
# Stateless rule evaluation: given position_state + CPagg signals, returns
# action + rule.  No live trading, no orders, no state.json access.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PANIC_BUY_THRESHOLD  = 1.01
PANIC_SELL_THRESHOLD = -0.01
BUY_ZONE_THRESHOLD   = 0.20
SELL_ZONE_THRESHOLD  = 0.80

POSITION_FLAT        = "FLAT"
POSITION_IN_POSITION = "IN_POSITION"

ACTION_BUY  = "BUY"
ACTION_SELL = "SELL"
ACTION_HOLD = "HOLD"

RULE_PANIC_BUY        = "PANIC_BUY"
RULE_STANDARD_BUY     = "STANDARD_BUY"
RULE_PANIC_SELL       = "PANIC_SELL"
RULE_STANDARD_SELL    = "STANDARD_SELL"
RULE_HOLD             = "HOLD"

REASON_MISSING_CPAGG      = "MISSING_CPAGG"
REASON_MISSING_PREV_CPAGG = "MISSING_PREV_CPAGG"
REASON_NO_SIGNAL          = "NO_SIGNAL"
REASON_PRICE_NOT_RISING   = "PRICE_NOT_RISING"
REASON_PRICE_NOT_FALLING  = "PRICE_NOT_FALLING"
REASON_PANIC_BUY_DISABLED = "PANIC_BUY_DISABLED"


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class DecisionResult:
    action:         str
    rule:           str
    reason:         str
    cpagg:          Optional[float]
    prev_cpagg:     Optional[float]
    trend_state:    Optional[str]
    position_state: str


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def evaluate(
    position_state: str,
    cpagg: Optional[float],
    prev_cpagg: Optional[float],
    trend_state: Optional[str] = None,
    panic_buy_threshold:  float = PANIC_BUY_THRESHOLD,
    panic_sell_threshold: float = PANIC_SELL_THRESHOLD,
    buy_zone_threshold:   float = BUY_ZONE_THRESHOLD,
    sell_zone_threshold:  float = SELL_ZONE_THRESHOLD,
    enable_panic_buy:     bool  = False,
) -> DecisionResult:
    """
    Evaluate the current channel position and return a trading decision.

    Position state machine:
    - FLAT        → can only produce BUY or HOLD
    - IN_POSITION → can only produce SELL or HOLD

    Rule priority (high → low):
    1. PANIC_SELL — triggered by extreme low CPagg; does not require prev_cpagg
    2. PANIC_BUY  — disabled by default (enable_panic_buy=False); FÁZIS 3C decision
    3. STANDARD   — triggered in zone with confirming momentum
    4. HOLD       — default when no rule fires

    trend_state is carried through for reporting; it does NOT gate rules
    in FÁZIS 3A/3C (trendgate comes in a later phase).
    """
    def _hold(reason: str) -> DecisionResult:
        return DecisionResult(
            action=ACTION_HOLD, rule=RULE_HOLD, reason=reason,
            cpagg=cpagg, prev_cpagg=prev_cpagg,
            trend_state=trend_state, position_state=position_state,
        )

    # Guard: missing CPagg → always HOLD regardless of position
    if cpagg is None:
        return _hold(REASON_MISSING_CPAGG)

    if position_state == POSITION_FLAT:
        # 1. Panic buy — disabled by default; skip and fall through
        if cpagg >= panic_buy_threshold:
            if enable_panic_buy:
                return DecisionResult(
                    action=ACTION_BUY, rule=RULE_PANIC_BUY,
                    reason=f"cpagg={cpagg:.4f} >= panic_buy_threshold={panic_buy_threshold}",
                    cpagg=cpagg, prev_cpagg=prev_cpagg,
                    trend_state=trend_state, position_state=position_state,
                )
            return _hold(REASON_PANIC_BUY_DISABLED)

        # 2. Standard buy — price in buy zone and rising (momentum confirmation)
        if cpagg <= buy_zone_threshold:
            if prev_cpagg is None:
                return _hold(REASON_MISSING_PREV_CPAGG)
            if cpagg > prev_cpagg:
                return DecisionResult(
                    action=ACTION_BUY, rule=RULE_STANDARD_BUY,
                    reason=f"cpagg={cpagg:.4f} <= buy_zone={buy_zone_threshold} and rising",
                    cpagg=cpagg, prev_cpagg=prev_cpagg,
                    trend_state=trend_state, position_state=position_state,
                )
            return _hold(REASON_PRICE_NOT_RISING)

        return _hold(REASON_NO_SIGNAL)

    if position_state == POSITION_IN_POSITION:
        # 1. Panic sell — price broke far below lower band
        if cpagg <= panic_sell_threshold:
            return DecisionResult(
                action=ACTION_SELL, rule=RULE_PANIC_SELL,
                reason=f"cpagg={cpagg:.4f} <= panic_sell_threshold={panic_sell_threshold}",
                cpagg=cpagg, prev_cpagg=prev_cpagg,
                trend_state=trend_state, position_state=position_state,
            )

        # 2. Standard sell — price in sell zone and falling (momentum confirmation)
        if cpagg >= sell_zone_threshold:
            if prev_cpagg is None:
                return _hold(REASON_MISSING_PREV_CPAGG)
            if cpagg < prev_cpagg:
                return DecisionResult(
                    action=ACTION_SELL, rule=RULE_STANDARD_SELL,
                    reason=f"cpagg={cpagg:.4f} >= sell_zone={sell_zone_threshold} and falling",
                    cpagg=cpagg, prev_cpagg=prev_cpagg,
                    trend_state=trend_state, position_state=position_state,
                )
            return _hold(REASON_PRICE_NOT_FALLING)

        return _hold(REASON_NO_SIGNAL)

    # Unknown position state — safe default
    return _hold(REASON_NO_SIGNAL)
