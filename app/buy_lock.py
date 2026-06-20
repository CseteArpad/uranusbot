# /opt/bots/uranus/app/buy_lock.py
# BUY trigger lock:
# Megakadályozza, hogy "ár már a szint felett van" (restart/prev_last anomália) helyzetben
# többször is BUY döntés szülessen ugyanarra a BUY szintre.
#
# Elv:
# - BUY triggerkor -> lock(level_name)=True (state.json-ben)
# - lock feloldása ha:
#   (A) in_position True -> minden buy lock felold
#   (B) last < level_value -> adott level lock felold (újra "alulról" lehet keresztezni)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


BUY_LEVELS = ("std_buy", "recovery_buy", "panic_buy")


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def ensure_struct(state: Dict[str, Any]) -> None:
    """
    Biztosítja a state['locks']['buy'] struktúrát.
    """
    if not isinstance(state, dict):
        raise TypeError("state must be dict")

    locks = state.get("locks")
    if not isinstance(locks, dict):
        locks = {}
        state["locks"] = locks

    buy = locks.get("buy")
    if not isinstance(buy, dict):
        buy = {}
        locks["buy"] = buy


def is_locked(state: Dict[str, Any], level_name: str) -> bool:
    ensure_struct(state)
    v = state["locks"]["buy"].get(level_name)
    return bool(isinstance(v, dict) and v.get("locked") is True)


def lock_level(
    state: Dict[str, Any],
    level_name: str,
    *,
    reason: str,
    last: Optional[float],
    level_value: Optional[float],
) -> None:
    """
    Lockol egy BUY szintet (std_buy / recovery_buy / panic_buy).
    """
    ensure_struct(state)
    if level_name not in BUY_LEVELS:
        return

    state["locks"]["buy"][level_name] = {
        "locked": True,
        "locked_at": _utc_now_iso(),
        "reason": str(reason or "BUY_TRIGGERED"),
        "last": float(last) if last is not None else None,
        "level_value": float(level_value) if level_value is not None else None,
    }


def unlock_level(
    state: Dict[str, Any],
    level_name: str,
    *,
    reason: str,
    last: Optional[float],
    level_value: Optional[float],
) -> None:
    """
    Felold és eltávolít egy BUY lockot.
    """
    ensure_struct(state)
    if level_name not in BUY_LEVELS:
        return

    buy = state["locks"]["buy"]
    if level_name in buy:
        buy.pop(level_name, None)

    meta = state.get("locks_meta")
    if not isinstance(meta, dict):
        meta = {}
        state["locks_meta"] = meta
    meta["last_unlock"] = {
        "level": level_name,
        "unlocked_at": _utc_now_iso(),
        "reason": str(reason or "UNLOCK"),
        "last": float(last) if last is not None else None,
        "level_value": float(level_value) if level_value is not None else None,
    }


def clear_all(state: Dict[str, Any], *, reason: str = "CLEAR_ALL") -> None:
    ensure_struct(state)
    state["locks"]["buy"] = {}
    meta = state.get("locks_meta")
    if not isinstance(meta, dict):
        meta = {}
        state["locks_meta"] = meta
    meta["cleared_at"] = _utc_now_iso()
    meta["cleared_reason"] = str(reason)


def apply_unlock_rules(
    state: Dict[str, Any],
    *,
    in_position: bool,
    last: Optional[float],
    levels: Dict[str, Any],
) -> None:
    """
    Unlock szabályok:
    (A) Ha in_position True -> minden buy lock töröl
    (B) Ha last < level_value -> adott szint lock töröl
    """
    ensure_struct(state)

    if in_position:
        clear_all(state, reason="IN_POSITION")
        return

    try:
        last_f = float(last) if last is not None else None
    except Exception:
        last_f = None
    if last_f is None:
        return

    if not isinstance(levels, dict):
        levels = {}

    for lvl in BUY_LEVELS:
        if not is_locked(state, lvl):
            continue

        v = levels.get(lvl)
        try:
            v_f = float(v) if v is not None else None
        except Exception:
            v_f = None

        if v_f is None:
            continue

        if last_f < v_f:
            unlock_level(
                state,
                lvl,
                reason="PRICE_BACK_BELOW_LEVEL",
                last=last_f,
                level_value=v_f,
            )
