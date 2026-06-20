from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import math


# ============================================================
# Uranus v2.1 canonical rule engine
# ------------------------------------------------------------
# Main goals:
# - STANDARD / RECOVERY / PANIC / CATASTROPHE decision model
# - Trend-aware PANIC rules
# - Profit invariant for STANDARD and RECOVERY exits
# - Panic context tracking
# - Safe defaults and robust missing-key handling
# - Multiple entry-point aliases for easier drop-in integration
# ============================================================


# -----------------------------
# Helpers
# -----------------------------
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _b(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _clean_float(value: float) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return float(value)


def _pct(value: Any, default: float = 0.0) -> float:
    """
    Accepts both:
    - 0.03  => 3%
    - 3     => 3%
    Converts everything to decimal form.
    """
    v = _f(value, default)
    if abs(v) >= 1.0:
        return v / 100.0
    return v


def _first_present(d: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _max_valid(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _min_valid(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


# -----------------------------
# Data models
# -----------------------------
@dataclass
class EngineContext:
    base: float
    last: float
    prev_last: float
    peak: float
    trough: float
    in_position: bool

    ma_short: float
    ma_long: float
    ma_sideways_band_pct: float

    fee_pct_per_side: float
    fee_total_pct: float
    fee_buffer_pct: float

    std_sell_pct: float
    recovery_sell_retrace_pct: float
    panic_sell_pct: float
    catastrophe_sell_pct: float

    std_buy_pct: float
    recovery_buy_rebound_pct: float
    panic_buy_pct: float
    catastrophe_buy_pct: float

    sell_reversal_min_pct: float

    recovery_context: bool
    panic_context: str
    last_panic_loss: float
    last_panic_sell_price: Optional[float]
    last_panic_buy_price: Optional[float]
    recovery_anchor_price: Optional[float]

    required_next_buy_mode: str
    required_next_sell_mode: str

    expected_recovery_sell_override: Optional[float]

    allow_sideways_standard: bool
    allow_sideways_recovery: bool

    symbol: str
    timeframe: str


@dataclass
class Candidate:
    action: str
    level_name: str
    level_value: float
    reason: str
    priority_rank: int
    profit_ok: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------
# Context builder
# -----------------------------
def _build_context(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> EngineContext:
    ctx = ctx or {}
    cycle = cycle or {}

    merged: Dict[str, Any] = {}
    merged.update(cycle)
    merged.update(ctx)

    base = _f(_first_present(merged, ["base", "entry_price", "position_entry_price", "avg_entry_price"], 0.0))
    last = _f(_first_present(merged, ["last", "price", "last_price", "close"], 0.0))
    prev_last = _f(_first_present(merged, ["prev_last", "prev_price", "previous_price", "previous_close"], last))
    peak = _f(_first_present(merged, ["peak", "cycle_peak", "position_peak"], max(base, last)))
    trough = _f(_first_present(merged, ["trough", "cycle_trough", "flat_trough"], min(base if base > 0 else last, last)))

    in_position = _b(_first_present(merged, ["in_position", "has_position", "position_open"], False))

    ma_short = _f(_first_present(merged, ["ma_short", "ma_fast", "short_ma", "ema_short"], last))
    ma_long = _f(_first_present(merged, ["ma_long", "ma_slow", "long_ma", "ema_long"], last))
    ma_sideways_band_pct = _f(_first_present(merged, ["ma_sideways_band_pct", "ma_sideways_band"], 0.0005))

    fee_pct_per_side = _pct(_first_present(merged, ["fee_pct_per_side", "fee_pct", "maker_taker_fee_pct"], 0.00075))
    fee_total_pct = _pct(_first_present(merged, ["fee_total_pct"], fee_pct_per_side * 2))
    fee_buffer_pct = _pct(_first_present(merged, ["fee_buffer_pct", "fee_buffer"], max(fee_total_pct, 0.002)))

    std_sell_pct = _pct(_first_present(merged, ["std_sell_pct"], 0.03))
    recovery_sell_retrace_pct = _pct(_first_present(merged, ["recovery_sell_retrace_pct", "recovery_sell_pct"], 0.017))
    panic_sell_pct = _pct(_first_present(merged, ["panic_sell_pct"], 0.05))
    catastrophe_sell_pct = _pct(_first_present(merged, ["catastrophe_sell_pct"], 0.10))

    std_buy_pct = _pct(_first_present(merged, ["std_buy_pct"], 0.03))
    recovery_buy_rebound_pct = _pct(_first_present(merged, ["recovery_buy_rebound_pct", "recovery_buy_pct"], 0.017))
    panic_buy_pct = _pct(_first_present(merged, ["panic_buy_pct"], 0.05))
    catastrophe_buy_pct = _pct(_first_present(merged, ["catastrophe_buy_pct"], 0.10))

    sell_reversal_min_pct = _pct(_first_present(merged, ["sell_reversal_min_pct"], 0.0))

    panic_context = _s(_first_present(merged, ["panic_context"], "NONE"), "NONE") or "NONE"
    last_panic_loss = _f(_first_present(merged, ["last_panic_loss"], 0.0))
    last_panic_sell_price = _first_present(merged, ["last_panic_sell_price"], None)
    if last_panic_sell_price is not None:
        last_panic_sell_price = _f(last_panic_sell_price)

    last_panic_buy_price = _first_present(merged, ["last_panic_buy_price"], None)
    if last_panic_buy_price is not None:
        last_panic_buy_price = _f(last_panic_buy_price)

    recovery_anchor_price = _first_present(merged, ["recovery_anchor_price"], None)
    if recovery_anchor_price is not None:
        recovery_anchor_price = _f(recovery_anchor_price)

    required_next_buy_mode = _s(_first_present(merged, ["required_next_buy_mode"], ""), "")
    required_next_sell_mode = _s(_first_present(merged, ["required_next_sell_mode"], ""), "")

    explicit_recovery_context = _first_present(merged, ["recovery_context"], None)
    if explicit_recovery_context is None:
        recovery_context = panic_context in ("AFTER_SELL_PANIC", "AFTER_BUY_PANIC") or bool(required_next_buy_mode) or bool(required_next_sell_mode)
    else:
        recovery_context = _b(explicit_recovery_context, False)

    expected_recovery_sell_override = _first_present(
        merged,
        ["expected_recovery_sell", "expected_recovery_sell_override"],
        None
    )
    if expected_recovery_sell_override is not None:
        expected_recovery_sell_override = _f(expected_recovery_sell_override)

    allow_sideways_standard = _b(_first_present(merged, ["allow_sideways_standard"], True))
    allow_sideways_recovery = _b(_first_present(merged, ["allow_sideways_recovery"], True))

    symbol = _s(_first_present(merged, ["symbol", "pair"], ""), "")
    timeframe = _s(_first_present(merged, ["timeframe"], ""), "")

    return EngineContext(
        base=base,
        last=last,
        prev_last=prev_last,
        peak=peak,
        trough=trough,
        in_position=in_position,
        ma_short=ma_short,
        ma_long=ma_long,
        ma_sideways_band_pct=ma_sideways_band_pct,
        fee_pct_per_side=fee_pct_per_side,
        fee_total_pct=fee_total_pct,
        fee_buffer_pct=fee_buffer_pct,
        std_sell_pct=std_sell_pct,
        recovery_sell_retrace_pct=recovery_sell_retrace_pct,
        panic_sell_pct=panic_sell_pct,
        catastrophe_sell_pct=catastrophe_sell_pct,
        std_buy_pct=std_buy_pct,
        recovery_buy_rebound_pct=recovery_buy_rebound_pct,
        panic_buy_pct=panic_buy_pct,
        catastrophe_buy_pct=catastrophe_buy_pct,
        sell_reversal_min_pct=sell_reversal_min_pct,
        recovery_context=recovery_context,
        panic_context=panic_context,
        last_panic_loss=last_panic_loss,
        last_panic_sell_price=last_panic_sell_price,
        last_panic_buy_price=last_panic_buy_price,
        recovery_anchor_price=recovery_anchor_price,
        required_next_buy_mode=required_next_buy_mode,
        required_next_sell_mode=required_next_sell_mode,
        expected_recovery_sell_override=expected_recovery_sell_override,
        allow_sideways_standard=allow_sideways_standard,
        allow_sideways_recovery=allow_sideways_recovery,
        symbol=symbol,
        timeframe=timeframe,
    )


# -----------------------------
# Level calculations
# -----------------------------
def _trend_state(ec: EngineContext) -> str:
    """
    MA trendállapot.

    ec.ma_short = aktuális MA
    ec.ma_long  = előző MA

    ma_sideways_band_pct belső értéke decimális:
    0.0005 = 0.05%
    """
    if ec.ma_long is None or ec.ma_long <= 0:
        return "SIDEWAYS"

    slope = (ec.ma_short - ec.ma_long) / ec.ma_long

    if abs(slope) <= ec.ma_sideways_band_pct:
        return "SIDEWAYS"
    if slope > 0:
        return "RISING"
    return "FALLING"


def _build_levels(ec: EngineContext) -> Dict[str, Optional[float]]:
    std_sell_level = ec.peak * (1.0 - ec.std_sell_pct) if ec.peak > 0 else None
    recovery_sell_retrace_level = ec.peak * (1.0 - ec.recovery_sell_retrace_pct) if ec.peak > 0 else None
    panic_sell_level = ec.base * (1.0 - ec.panic_sell_pct) if ec.base > 0 else None
    catastrophe_sell_level = ec.base * (1.0 - ec.catastrophe_sell_pct) if ec.base > 0 else None

    std_buy_level = ec.trough * (1.0 + ec.std_buy_pct) if ec.trough > 0 else None
    recovery_buy_rebound_level = ec.trough * (1.0 + ec.recovery_buy_rebound_pct) if ec.trough > 0 else None
    panic_buy_level = ec.base * (1.0 + ec.panic_buy_pct) if ec.base > 0 else None
    catastrophe_buy_level = ec.base * (1.0 + ec.catastrophe_buy_pct) if ec.base > 0 else None

    recovery_buy_limit = None
    if (
        ec.last_panic_sell_price is not None
        and ec.recovery_anchor_price is not None
        and (1.0 + ec.fee_total_pct) > 0
    ):
        recovery_buy_limit = (
            ((2.0 * ec.last_panic_sell_price * (1.0 - ec.fee_total_pct))
             - (ec.recovery_anchor_price * (1.0 + ec.fee_total_pct)))
            / (1.0 + ec.fee_total_pct)
        )

    recovery_sell_limit = None
    if (
        ec.last_panic_buy_price is not None
        and ec.recovery_anchor_price is not None
        and (1.0 - ec.fee_total_pct) > 0
    ):
        recovery_sell_limit = (
            ((2.0 * ec.last_panic_buy_price * (1.0 + ec.fee_total_pct))
             - (ec.recovery_anchor_price * (1.0 - ec.fee_total_pct)))
            / (1.0 - ec.fee_total_pct)
        )

    recovery_sell_level = recovery_sell_retrace_level
    recovery_buy_level = recovery_buy_rebound_level

    expected_recovery_sell = ec.expected_recovery_sell_override
    if expected_recovery_sell is None:
        expected_recovery_sell = _max_valid([std_sell_level, recovery_sell_level, recovery_sell_limit])

    return {
        "std_sell_level": _clean_float(std_sell_level),
        "recovery_sell_level": _clean_float(recovery_sell_level),
        "recovery_sell_retrace_level": _clean_float(recovery_sell_retrace_level),
        "recovery_sell_limit": _clean_float(recovery_sell_limit),
        "panic_sell_level": _clean_float(panic_sell_level),
        "catastrophe_sell_level": _clean_float(catastrophe_sell_level),
        "std_buy_level": _clean_float(std_buy_level),
        "recovery_buy_level": _clean_float(recovery_buy_level),
        "recovery_buy_rebound_level": _clean_float(recovery_buy_rebound_level),
        "recovery_buy_limit": _clean_float(recovery_buy_limit),
        "panic_buy_level": _clean_float(panic_buy_level),
        "catastrophe_buy_level": _clean_float(catastrophe_buy_level),
        "expected_recovery_sell": _clean_float(expected_recovery_sell),
    }


def _profit_after_fees_sell(entry_price: float, exit_price: float, fee_total_pct: float) -> float:
    """
    Konzervatív fee utáni profit.
    A fee_total_pct teljes kör díja, ezért fele belépésre, fele kilépésre kerül.
    STANDARD és RECOVERY SELL csak akkor engedhető, ha ez >= 0.
    """
    if entry_price <= 0 or exit_price <= 0:
        return -1e18
    half_fee = max(0.0, fee_total_pct) / 2.0
    entry_cost = entry_price * (1.0 + half_fee)
    exit_net = exit_price * (1.0 - half_fee)
    return exit_net - entry_cost


# -----------------------------
# Candidate builders
# -----------------------------
def _sell_candidates(ec: EngineContext, levels: Dict[str, Optional[float]], trend_state: str) -> Tuple[List[Candidate], List[str]]:
    debug: List[str] = []
    cands: List[Candidate] = []

    std_sell_level = levels["std_sell_level"]
    recovery_sell_level = levels["recovery_sell_level"]
    recovery_sell_retrace_level = levels.get("recovery_sell_retrace_level")
    recovery_sell_limit = levels.get("recovery_sell_limit")
    panic_sell_level = levels["panic_sell_level"]
    catastrophe_sell_level = levels["catastrophe_sell_level"]

    if not ec.in_position:
        debug.append("sell-side skipped: not in position")
        return cands, debug

    falling_now = ec.last < ec.prev_last

    reversal_pct = 0.0
    if ec.prev_last > 0:
        reversal_pct = max(0.0, (ec.prev_last - ec.last) / ec.prev_last)

    reversal_ok = reversal_pct >= ec.sell_reversal_min_pct

    debug.append(
        f"SELL_CONTEXT in_position={ec.in_position} "
        f"last={ec.last} prev_last={ec.prev_last} "
        f"falling_now={falling_now} "
        f"reversal_pct={reversal_pct:.6f} "
        f"sell_reversal_min_pct={ec.sell_reversal_min_pct:.6f} "
        f"reversal_ok={reversal_ok}"
    )

    # 1) SELL_PANIC
    if panic_sell_level is not None:
        panic_sell_price_gate_ok = ec.ma_long > 0 and ec.last < ec.ma_long
        recovery_sell_forced = (
            ec.recovery_context
            and ec.required_next_sell_mode.upper() == "RECOVERY"
            and ec.panic_context == "AFTER_SELL_PANIC"
        )

        panic_sell_ok = (
            falling_now
            and ec.last <= panic_sell_level
            and trend_state == "FALLING"
            and panic_sell_price_gate_ok
            and not recovery_sell_forced
        )

        if panic_sell_ok:
            cands.append(Candidate(
                action="SELL_PANIC",
                level_name="panic_sell_level",
                level_value=panic_sell_level,
                reason="falling and last <= panic_sell_level and ma_trend_state=FALLING and last < ma_long",
                priority_rank=2,
                profit_ok=True,
            ))
            debug.append("SELL_PANIC candidate added")
        else:
            panic_reasons = []
            if not falling_now:
                panic_reasons.append("not_falling_now")
            if ec.last > panic_sell_level:
                panic_reasons.append("last_above_panic_sell_level")
            if trend_state != "FALLING":
                panic_reasons.append(f"ma_trend_state={trend_state}")
            if not panic_sell_price_gate_ok:
                panic_reasons.append("price_not_below_ma_long")
            if recovery_sell_forced:
                panic_reasons.append("blocked_after_buy_recovery_required_recovery_sell")
            debug.append("SELL_PANIC blocked: " + ", ".join(panic_reasons) if panic_reasons else "SELL_PANIC blocked")

    # 2) SELL_CATASTROPHE
    if catastrophe_sell_level is not None and ec.in_position and falling_now and ec.last <= catastrophe_sell_level:
        cands.append(Candidate(
            action="SELL_CATASTROPHE",
            level_name="catastrophe_sell_level",
            level_value=catastrophe_sell_level,
            reason="falling and last <= catastrophe_sell_level",
            priority_rank=1,
            profit_ok=True,
        ))
        debug.append("SELL_CATASTROPHE candidate added")

    # Profit-invariant applies to STANDARD and RECOVERY.
    # PANIC/CATASTROPHE may be loss-making, STANDARD/RECOVERY may not.
    profit_after_fees = _profit_after_fees_sell(ec.base, ec.last, ec.fee_total_pct)
    profit_ok_normal_sell = (profit_after_fees >= 0.0)

    # 3) SELL_RECOVERY
    if recovery_sell_level is not None and std_sell_level is not None:
        recovery_structure_ok = recovery_sell_level > std_sell_level
        fee_floor_ok = ec.last >= ec.base * (1.0 + ec.fee_buffer_pct)
        recovery_fee_ok = fee_floor_ok
        mode_ok = (
            ec.recovery_context
            or ec.required_next_sell_mode.upper() == "RECOVERY"
            or ec.panic_context == "AFTER_BUY_PANIC"
        )

        recovery_limit_ok = True
        if recovery_sell_limit is not None:
            recovery_limit_ok = ec.last >= recovery_sell_limit


        recovery_window_ok = True
        if recovery_sell_limit is not None and recovery_sell_retrace_level is not None:
            recovery_window_ok = (recovery_sell_limit <= recovery_sell_retrace_level)
        recovery_cross_ok = True
        if recovery_sell_retrace_level is not None:
            recovery_cross_ok = (
                ec.prev_last > recovery_sell_retrace_level
                and ec.last <= recovery_sell_retrace_level
            )

        standard_not_ready = True
        if std_sell_level is not None:
            standard_not_ready = ec.last > std_sell_level

        if (
            falling_now
            and reversal_ok
            and ec.last <= recovery_sell_level
            and recovery_structure_ok
            and mode_ok
            and recovery_fee_ok
            and profit_ok_normal_sell
            and recovery_limit_ok
            and recovery_window_ok
            and recovery_cross_ok
        ):
            cands.append(Candidate(
                action="SELL_RECOVERY",
                level_name="recovery_sell_level",
                level_value=recovery_sell_level,
                reason="falling and crossed recovery_sell_retrace_level while standard sell not ready and recovery constraints ok",
                priority_rank=3,
                profit_ok=True,
            ))
            debug.append("SELL_RECOVERY candidate added")
        else:
            blockers = []
            if not falling_now:
                blockers.append("not_falling_now")
            if not reversal_ok:
                blockers.append("reversal_not_enough")
            if ec.last > recovery_sell_level:
                blockers.append("last_above_recovery_sell_level")
            if not recovery_structure_ok:
                blockers.append("recovery_sell_not_above_standard_sell")
            if not mode_ok:
                blockers.append("recovery_mode_not_active")
            if not recovery_fee_ok:
                blockers.append("recovery_fee_floor_not_met")
            if not profit_ok_normal_sell:
                blockers.append("profit_not_ok")
            if recovery_sell_limit is not None and ec.last < recovery_sell_limit:
                blockers.append("below_recovery_sell_limit")
            if not recovery_window_ok:
                blockers.append("recovery_window_impossible")
            if not recovery_cross_ok:
                blockers.append("recovery_retrace_not_crossed")
            if not standard_not_ready:
                blockers.append("standard_sell_already_ready")
            debug.append("SELL_RECOVERY blocked: " + ", ".join(blockers) if blockers else "SELL_RECOVERY blocked")

    # 4) SELL_STANDARD
    if std_sell_level is not None and recovery_sell_level is not None:
        fee_floor_ok = ec.last >= ec.base * (1.0 + ec.fee_buffer_pct)

        if (
            falling_now
            and reversal_ok
            and ec.last <= std_sell_level
            and fee_floor_ok
            and profit_ok_normal_sell
        ):
            cands.append(Candidate(
                action="SELL_STANDARD",
                level_name="std_sell_level",
                level_value=std_sell_level,
                reason="falling and last <= std_sell_level and profit invariant ok",
                priority_rank=4,
                profit_ok=True,
            ))
            debug.append("SELL_STANDARD candidate added")
        else:
            std_blockers = []
            if not falling_now:
                std_blockers.append("not_falling_now")
            if not reversal_ok:
                std_blockers.append("reversal_not_enough")
            if ec.last > std_sell_level:
                std_blockers.append("last_above_std_sell_level")
            if not fee_floor_ok:
                std_blockers.append("fee_floor_not_ok")
            if not profit_ok_normal_sell:
                std_blockers.append("profit_not_ok")
            debug.append("SELL_STANDARD blocked: " + (", ".join(std_blockers) if std_blockers else "unknown"))

    return cands, debug


def _buy_candidates(ec: EngineContext, levels: Dict[str, Optional[float]], trend_state: str) -> Tuple[List[Candidate], List[str]]:
    debug: List[str] = []
    cands: List[Candidate] = []

    std_buy_level = levels["std_buy_level"]
    recovery_buy_level = levels["recovery_buy_level"]
    recovery_buy_rebound_level = levels.get("recovery_buy_rebound_level")
    recovery_buy_limit = levels.get("recovery_buy_limit")
    panic_buy_level = levels["panic_buy_level"]
    catastrophe_buy_level = levels["catastrophe_buy_level"]
    expected_recovery_sell = levels["expected_recovery_sell"]

    if ec.in_position:
        debug.append("buy-side skipped: already in position")
        return cands, debug

    rising_now = ec.last > ec.prev_last

    # 1) CATASTROPHE BUY (top override)
    if catastrophe_buy_level is not None and ec.last >= catastrophe_buy_level:
        cands.append(Candidate(
            action="BUY_CATASTROPHE",
            level_name="catastrophe_buy_level",
            level_value=catastrophe_buy_level,
            reason="last >= catastrophe_buy_level",
            priority_rank=1,
            profit_ok=True,
        ))
        debug.append("BUY_CATASTROPHE candidate added")

    # 2) BUY_PANIC
    if panic_buy_level is not None:
        panic_buy_price_gate_ok = ec.ma_long > 0 and ec.last > ec.ma_long

        panic_buy_ok = (
            rising_now
            and ec.last >= panic_buy_level
            and trend_state == "RISING"
            and panic_buy_price_gate_ok
        )

        if panic_buy_ok:
            cands.append(Candidate(
                action="BUY_PANIC",
                level_name="panic_buy_level",
                level_value=panic_buy_level,
                reason="rising and last >= panic_buy_level and ma_trend_state=RISING and last > ma_long",
                priority_rank=2,
                profit_ok=True,
            ))
            debug.append("BUY_PANIC candidate added")
        else:
            panic_reasons = []
            if not rising_now:
                panic_reasons.append("not_rising_now")
            if ec.last < panic_buy_level:
                panic_reasons.append("last_below_panic_buy_level")
            if trend_state != "RISING":
                panic_reasons.append(f"ma_trend_state={trend_state}")
            if not panic_buy_price_gate_ok:
                panic_reasons.append("price_not_above_ma_long")
            debug.append("BUY_PANIC blocked: " + ", ".join(panic_reasons) if panic_reasons else "BUY_PANIC blocked")

    # 3) BUY_RECOVERY
    if recovery_buy_level is not None:
        recovery_mode_ok = (
            ec.recovery_context
            or ec.required_next_buy_mode.upper() == "RECOVERY"
            or ec.required_next_buy_mode.upper() == "RECOVERY_OR_LOWER_STANDARD"
            or ec.panic_context == "AFTER_SELL_PANIC"
        )

        recovery_expected_ok = True
        if ec.panic_context == "AFTER_SELL_PANIC":
            need = ec.last_panic_loss + (ec.last * ec.fee_total_pct)
            recovery_expected_ok = (expected_recovery_sell is not None and expected_recovery_sell >= need)

        recovery_limit_ok = True
        if recovery_buy_limit is not None:
            recovery_limit_ok = ec.last <= recovery_buy_limit


        recovery_window_ok = True
        if recovery_buy_limit is not None and recovery_buy_rebound_level is not None:
            recovery_window_ok = (recovery_buy_limit >= recovery_buy_rebound_level)
        recovery_cross_ok = True
        if recovery_buy_rebound_level is not None:
            recovery_cross_ok = (
                ec.prev_last < recovery_buy_rebound_level
                and ec.last >= recovery_buy_rebound_level
            )


        if (
            rising_now
            and ec.last >= recovery_buy_level
            and recovery_mode_ok
            and recovery_expected_ok
            and recovery_limit_ok
            and recovery_window_ok
            and recovery_cross_ok
            
        ):
            cands.append(Candidate(
                action="BUY_RECOVERY",
                level_name="recovery_buy_level",
                level_value=recovery_buy_level,
                reason="recovery rebound crossed, standard not ready, recovery context/limit ok",
                priority_rank=3,
                profit_ok=True,
            ))
            debug.append("BUY_RECOVERY candidate added")
        else:
            blockers = []
            if not rising_now:
                blockers.append("not_rising_now")
            if ec.last < recovery_buy_level:
                blockers.append("below_recovery_buy_level")
            if not recovery_mode_ok:
                blockers.append("recovery_mode_not_active")
            if not recovery_expected_ok:
                blockers.append("expected_recovery_sell_not_enough")
            if recovery_buy_limit is not None and ec.last > recovery_buy_limit:
                blockers.append("above_recovery_buy_limit")
            if not recovery_window_ok:
                blockers.append("recovery_window_impossible")
            if not recovery_cross_ok:
                blockers.append("recovery_rebound_not_crossed")
            debug.append("BUY_RECOVERY blocked: " + ", ".join(blockers) if blockers else "BUY_RECOVERY blocked")

    # 4) BUY_STANDARD
    if std_buy_level is not None and recovery_buy_level is not None:
        panic_after_sell_cap_ok = True
        panic_after_sell_cap = None

        if ec.panic_context == "AFTER_SELL_PANIC":
            panic_after_sell_cap = recovery_buy_limit
            if panic_after_sell_cap is None:
                panic_after_sell_cap = getattr(ec, "recovery_target_entry_cap", None)

            if panic_after_sell_cap is not None:
                panic_after_sell_cap_ok = ec.last <= panic_after_sell_cap

        if (
            rising_now
            and ec.last >= std_buy_level
            and panic_after_sell_cap_ok
        ):
            cands.append(Candidate(
                action="BUY_STANDARD",
                level_name="std_buy_level",
                level_value=std_buy_level,
                reason="rising and last >= std_buy_level and panic recovery cap ok",
                priority_rank=4,
                profit_ok=True,
            ))
            debug.append("BUY_STANDARD candidate added")
        else:
            blockers = []
            if not rising_now:
                blockers.append("not_rising_now")
            if ec.last < std_buy_level:
                blockers.append("below_std_buy_level")
            if not panic_after_sell_cap_ok:
                blockers.append("above_panic_recovery_buy_cap")
            debug.append("BUY_STANDARD blocked: " + ", ".join(blockers) if blockers else "BUY_STANDARD blocked")

    return cands, debug


# -----------------------------
# Selection
# -----------------------------

def _select_candidate(ec: EngineContext, candidates: List[Candidate]) -> Optional[Candidate]:
    if not candidates:
        return None

    # csak profitképes jelöltek
    valid = [c for c in candidates if c.profit_ok]

    if not valid:
        return None

    # KANONIKUS SZABÁLYHIERARCHIA:
    # kisebb priority_rank = erősebb elsőbbség
    return min(valid, key=lambda c: c.priority_rank)

# -----------------------------
# State transitions
# -----------------------------
def _apply_state_transition(
    ec: EngineContext,
    decision: Optional[Candidate],
    levels: Dict[str, Optional[float]]
) -> Dict[str, Any]:
    panic_context = ec.panic_context
    last_panic_loss = ec.last_panic_loss
    recovery_context = ec.recovery_context
    required_next_buy_mode = ec.required_next_buy_mode
    required_next_sell_mode = ec.required_next_sell_mode

    if decision is None:
        return {
            "panic_context": panic_context,
            "last_panic_loss": last_panic_loss,
            "recovery_context": recovery_context,
            "required_next_buy_mode": required_next_buy_mode,
            "required_next_sell_mode": required_next_sell_mode,
        }

    action = decision.action

    if action in ("SELL_PANIC", "SELL_CATASTROPHE"):
        panic_context = "AFTER_SELL_PANIC"
        last_panic_loss = max(0.0, ec.base - ec.last)
        recovery_context = True
        required_next_buy_mode = "RECOVERY_OR_LOWER_STANDARD"

    elif action in ("BUY_PANIC", "BUY_CATASTROPHE"):
        panic_context = "AFTER_BUY_PANIC"
        recovery_context = True
        required_next_sell_mode = "RECOVERY"

    elif action == "BUY_RECOVERY":
        # recovery cycle continues until recovery sell closes it
        recovery_context = True
        required_next_sell_mode = "RECOVERY"

    elif action == "SELL_RECOVERY":
        # recovery cycle considered completed
        panic_context = "NONE"
        last_panic_loss = 0.0
        recovery_context = False
        required_next_buy_mode = ""
        required_next_sell_mode = ""

    elif action == "SELL_STANDARD":
        # normal profitable exit resets cycle
        panic_context = "NONE"
        last_panic_loss = 0.0
        recovery_context = False
        required_next_buy_mode = ""
        required_next_sell_mode = ""

    elif action == "BUY_STANDARD":
        # standard buy does not itself reset prior sell panic cycle unless no explicit recovery forced
        if ec.required_next_buy_mode.upper() != "RECOVERY":
            required_next_buy_mode = ""

    return {
        "panic_context": panic_context,
        "last_panic_loss": last_panic_loss,
        "recovery_context": recovery_context,
        "required_next_buy_mode": required_next_buy_mode,
        "required_next_sell_mode": required_next_sell_mode,
    }


# -----------------------------
# Public engine
# -----------------------------
def evaluate_rule_engine(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ec = _build_context(ctx=ctx, cycle=cycle)
    trend_state = _trend_state(ec)
    levels = _build_levels(ec)

    debug: List[str] = []
    sell_cands, sell_debug = _sell_candidates(ec, levels, trend_state)
    buy_cands, buy_debug = _buy_candidates(ec, levels, trend_state)
    debug.extend(sell_debug)
    debug.extend(buy_debug)

    all_candidates = sell_cands if ec.in_position else buy_cands
    selected = _select_candidate(ec, all_candidates)

    if selected is None:
        action = "HOLD"
        reason = "NO_SELL_CONDITION_MET" if ec.in_position else "NO_BUY_CONDITION_MET"
    else:
        action = selected.action
        reason = selected.reason

    state_updates = _apply_state_transition(ec, selected, levels)

    result = {
        "action": action,
        "decision": action,
        "last_result": action,
        "reason": reason,
        "selected_level_name": selected.level_name if selected else None,
        "selected_level": selected.level_value if selected else None,
        "trend_state": trend_state,
        "ma_trend_state": trend_state,
        "ma_sideways_band_pct": ec.ma_sideways_band_pct,
        "in_position": ec.in_position,
        "symbol": ec.symbol,
        "timeframe": ec.timeframe,
        "base": ec.base,
        "last": ec.last,
        "prev_last": ec.prev_last,
        "peak": ec.peak,
        "trough": ec.trough,
        "panic_context": state_updates["panic_context"],
        "last_panic_loss": state_updates["last_panic_loss"],
        "recovery_context": state_updates["recovery_context"],
        "required_next_buy_mode": state_updates["required_next_buy_mode"],
        "required_next_sell_mode": state_updates["required_next_sell_mode"],
        "thresholds": levels,
        "candidates": [c.to_dict() for c in all_candidates],
        "debug": debug + [f"sell_reversal_min_pct={ec.sell_reversal_min_pct}"],        
        "engine_version": "uranus_v2.1_price_priority",
    }

    return result


# -----------------------------
# Compatibility aliases
# -----------------------------
def evaluate_rules(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return evaluate_rule_engine(ctx=ctx, cycle=cycle)


def decide(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return evaluate_rule_engine(ctx=ctx, cycle=cycle)


def run_rule_engine(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return evaluate_rule_engine(ctx=ctx, cycle=cycle)


def rule_engine(ctx: Optional[Dict[str, Any]] = None, cycle: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return evaluate_rule_engine(ctx=ctx, cycle=cycle)


# -----------------------------
# Minimal self-test entry point
# -----------------------------
if __name__ == "__main__":
    sample = {
        "base": 100.0,
        "last": 103.0,
        "prev_last": 102.0,
        "peak": 110.0,
        "trough": 95.0,
        "in_position": False,
        "ma_short": 101.0,
        "ma_long": 100.0,
        "panic_context": "AFTER_SELL_PANIC",
        "last_panic_loss": 4.0,
        "recovery_context": True,
        "std_sell_pct": 0.03,
        "recovery_sell_retrace_pct": 0.017,
        "panic_sell_pct": 0.05,
        "catastrophe_sell_pct": 0.10,
        "std_buy_pct": 0.03,
        "recovery_buy_rebound_pct": 0.017,
        "panic_buy_pct": 0.05,
        "catastrophe_buy_pct": 0.10,
    }
    out = evaluate_rule_engine(sample, {})
    import json
    print(json.dumps(out, indent=2, ensure_ascii=False))

# ------------------------------------------------------------
# Backward-compatible helpers for monitor / lab imports
# ------------------------------------------------------------
def compute_levels(ctx: dict | None = None, cycle: dict | None = None) -> dict:
    ec = _build_context(ctx=ctx, cycle=cycle)
    return _build_levels(ec)


def normalize_state(ctx: dict | None = None, cycle: dict | None = None) -> dict:
    ec = _build_context(ctx=ctx, cycle=cycle)
    levels = _build_levels(ec)
    return {
        "base": ec.base,
        "last": ec.last,
        "prev_last": ec.prev_last,
        "peak": ec.peak,
        "trough": ec.trough,
        "in_position": ec.in_position,
        "ma_short": ec.ma_short,
        "ma_long": ec.ma_long,
        "ma_sideways_band_pct": ec.ma_sideways_band_pct,
        "trend_state": _trend_state(ec),
        "panic_context": ec.panic_context,
        "last_panic_loss": ec.last_panic_loss,
        "recovery_context": ec.recovery_context,
        "required_next_buy_mode": ec.required_next_buy_mode,
        "required_next_sell_mode": ec.required_next_sell_mode,
        "thresholds": levels,
    }
