# tick_context.py
# Uranus V2 – TickContext
# Recovery-alapú, reference_base elvű kontextusépítő.

from __future__ import annotations

from typing import Dict, Any, Optional
from datetime import datetime, timezone


DEFAULT_RULES = {
    "std_sell_pct": 1.0,
    "panic_sell_pct": 1.0,
    "catastrophe_sell_pct": 10.0,
    "std_buy_pct": 1.0,
    "panic_buy_pct": 1.0,
    "catastrophe_buy_pct": 10.0,
    "recovery_buy_rebound_pct": 0.3,
    "recovery_sell_retrace_pct": 0.3,
    "recovery_arm_timeout_bars": 240,
    "min_rebound_confirm_pct": 0.3,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return bool(value)


class TickContextBuilder:
    """
    NEM dönt.
    Csak V2-kompatibilis ctx-et készít elő a rule_engine számára.
    """

    @staticmethod
    def build(
        *,
        pair: str,
        state: Dict[str, Any],
        last_price: float,
        in_position: bool
    ) -> Dict[str, Any]:

        params = state.get("params") or {}
        rules = DEFAULT_RULES.copy()

        for k in list(rules.keys()):
            v = params.get(k)
            if v is not None:
                try:
                    rules[k] = float(v)
                except Exception:
                    pass

        std_sell_pct = rules["std_sell_pct"] / 100.0
        panic_sell_pct = rules["panic_sell_pct"] / 100.0
        catastrophe_sell_pct = rules["catastrophe_sell_pct"] / 100.0

        std_buy_pct = rules["std_buy_pct"] / 100.0
        panic_buy_pct = rules["panic_buy_pct"] / 100.0
        catastrophe_buy_pct = rules["catastrophe_buy_pct"] / 100.0

        recovery_buy_rebound_pct = rules["recovery_buy_rebound_pct"] / 100.0
        recovery_sell_retrace_pct = rules["recovery_sell_retrace_pct"] / 100.0

        prev_last = _safe_float(state.get("prev_last"))
        base = _safe_float(state.get("base"))
        previous_base = _safe_float(state.get("previous_base"))
        peak = _safe_float(state.get("peak"))
        trough = _safe_float(state.get("trough"))

        if base is None:
            base = float(last_price)

        if previous_base is None:
            previous_base = base

        if in_position:
            if peak is None or float(last_price) > peak:
                peak = float(last_price)
            trough = None
        else:
            if trough is None or float(last_price) < trough:
                trough = float(last_price)
            peak = None

        reference_base = max(float(base), float(previous_base))

        std_sell = peak * (1.0 - std_sell_pct) if peak is not None else None
        panic_sell = reference_base * (1.0 - panic_sell_pct)
        catastrophe_sell = reference_base * (1.0 - catastrophe_sell_pct)

        std_buy = trough * (1.0 + std_buy_pct) if trough is not None else None
        panic_buy = reference_base * (1.0 + panic_buy_pct)
        catastrophe_buy = reference_base * (1.0 + catastrophe_buy_pct)

        recovery_sell = None
        if peak is not None:
            recovery_sell_raw = peak * (1.0 - recovery_sell_retrace_pct)
            recovery_sell = max(recovery_sell_raw, std_sell if std_sell is not None else recovery_sell_raw)

        recovery_buy = None
        if trough is not None:
            recovery_buy_raw = trough * (1.0 + recovery_buy_rebound_pct)
            recovery_buy = min(recovery_buy_raw, std_buy if std_buy is not None else recovery_buy_raw)

        flags = state.setdefault("flags", {})
        cycle = state.setdefault("cycle", {})

        ma_positive = _safe_bool(state.get("ma_positive"), False)
        ma_negative = _safe_bool(state.get("ma_negative"), False)
        ma_filter_enabled = _safe_bool(state.get("ma_filter_enabled"), True)

        flags["ma_positive"] = ma_positive
        flags["ma_negative"] = ma_negative
        flags["ma_filter_enabled"] = ma_filter_enabled
        flags["buy_hierarchy_ok"] = bool(std_buy is not None and catastrophe_buy > panic_buy > base > std_buy)
        flags["sell_hierarchy_ok"] = bool(std_sell is not None and std_sell > base > panic_sell > catastrophe_sell)

        levels = {
            "reference_base": reference_base,
            "std_sell": std_sell,
            "recovery_sell": recovery_sell,
            "panic_sell": panic_sell,
            "catastrophe_sell": catastrophe_sell,
            "std_buy": std_buy,
            "recovery_buy": recovery_buy,
            "panic_buy": panic_buy,
            "catastrophe_buy": catastrophe_buy,
            "computed_at": now_iso(),
        }

        state["trading_rules"] = rules

        return {
            "pair": pair,
            "last": float(last_price),
            "prev_last": prev_last,
            "base": base,
            "previous_base": previous_base,
            "peak": peak,
            "trough": trough,
            "levels": levels,
            "flags": flags,
            "cycle": cycle,
            "in_position": in_position,
            "rules_meta": rules,
        }
