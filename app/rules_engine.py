# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, Optional


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def apply_hierarchy_and_flags(targets: Dict[str, Any]) -> Dict[str, Any]:
    """
    Kötelező hierarchia + active zászlók.

    SELL: standard_sell > profitless_sell > panic_sell
    BUY:  panic_buy > profitless_buy > standard_buy

    Ha hierarchia sérülne -> érintett szabály célárát NULL-ra tesszük, active=False,
    és indokot írunk az inactive_reasons alá.
    """
    if not isinstance(targets, dict):
        return targets

    # Pull values
    std_sell = _as_float(targets.get("std_sell_price"))
    profitless_sell = _as_float(targets.get("profitless_sell_price"))
    panic_sell = _as_float(targets.get("panic_sell_price"))

    std_buy = _as_float(targets.get("std_buy_price"))
    profitless_buy = _as_float(targets.get("profitless_buy_price"))
    panic_buy = _as_float(targets.get("panic_buy_price"))

    active = {
        "std_sell": std_sell is not None,
        "profitless_sell": profitless_sell is not None,
        "panic_sell": panic_sell is not None,
        "std_buy": std_buy is not None,
        "profitless_buy": profitless_buy is not None,
        "panic_buy": panic_buy is not None,
    }
    reasons: Dict[str, str] = {}

    # --- SELL hierarchia: std_sell > profitless_sell > panic_sell ---
    # 1) standard_sell csak profitless fölött lehet
    if std_sell is not None and profitless_sell is not None:
        if not (std_sell > profitless_sell):
            targets["std_sell_price"] = None
            active["std_sell"] = False
            reasons["std_sell"] = "Inaktív: std_sell_price <= profitless_sell_price (hierarchia sérülne)."

    # 2) profitless_sell csak panic fölött lehet
    if profitless_sell is not None and panic_sell is not None:
        if not (profitless_sell > panic_sell):
            # Biztonsági elv: a panic legyen a legalacsonyabb; ha nem, inkább a panic legyen inaktív
            targets["panic_sell_price"] = None
            active["panic_sell"] = False
            reasons["panic_sell"] = "Inaktív: profitless_sell_price <= panic_sell_price (panic nem lehet a profitless felett)."

    # --- BUY hierarchia: panic_buy > profitless_buy > std_buy ---
    # 1) standard_buy csak profitless alatt lehet
    if std_buy is not None and profitless_buy is not None:
        if not (std_buy < profitless_buy):
            targets["std_buy_price"] = None
            active["std_buy"] = False
            reasons["std_buy"] = "Inaktív: std_buy_price >= profitless_buy_price (hierarchia sérülne)."

    # 2) panic_buy csak profitless fölött lehet
    if panic_buy is not None and profitless_buy is not None:
        if not (panic_buy > profitless_buy):
            targets["panic_buy_price"] = None
            active["panic_buy"] = False
            reasons["panic_buy"] = "Inaktív: panic_buy_price <= profitless_buy_price (panic nem lehet a profitless alatt)."

    # Írjuk vissza a meta mezőket (JSON-serializable)
    targets["active"] = active
    targets["inactive_reasons"] = reasons

    return targets
