#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import copy
from typing import Any, Dict


SCHEMA_VERSION = 2


def _deep_merge_missing(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in src.items():
        if k not in dst:
            dst[k] = copy.deepcopy(v)
            continue

        if isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge_missing(dst[k], v)
    return dst


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,

        "bot": {
            "name": "Uranus",
            "enabled": True,
            "mode": "live",
        },

        "pair": "XRP/USDC",
        "timeframe": "1m",
        "exchange": "binance",

        "in_position": False,
        "active_trade_id": None,
        "position_entry_rule": None,

        "base": None,
        "base_price": None,
        "entry_price": None,
        "buy_price": None,
        "previous_base": None,
        "reference_base": None,

        "last": None,
        "prev_last": None,
        "last_price": None,
        "close": None,
        "open": None,
        "high": None,
        "low": None,
        "peak": None,
        "trough": None,
        "volume": None,

        "_flat_anchor": None,
        "_write_test_ts": None,

        "panic_context": "NONE",
        "last_panic_loss": 0.0,
        "recovery_context": False,

        "recovery_mode": False,
        "recovery_loss_pct": 0.0,
        "recovery_anchor_price": None,
        "recovery_target_entry_cap": None,
        "required_next_buy_mode": None,
        "required_next_sell_mode": None,

        "decision": {},
        "decision_action": "HOLD",
        "decision_reason": None,
        "decision_rule": None,
        "decision_level": "none",

        "last_decision": {},
        "last_action": None,
        "last_reason": None,

        "engine": {},

        "levels": {
            "reference_base": None,
            "std_sell": None,
            "recovery_sell": None,
            "panic_sell": None,
            "catastrophe_sell": None,
            "std_buy": None,
            "recovery_buy": None,
            "panic_buy": None,
            "catastrophe_buy": None,
            "computed_at": None,
        },

        "cycle": {
            "recovery_mode": False,
            "recovery_loss_pct": 0.0,
            "recovery_anchor_price": None,
            "required_next_buy_mode": None,
            "required_next_sell_mode": None,
            "recovery_target_entry_cap": None,
            "recovery_buy_armed": False,
            "recovery_sell_armed": False,
            "recovery_buy_arm_age": 0,
            "recovery_sell_arm_age": 0,
        },

        "flags": {
            "buy_hierarchy_ok": False,
            "sell_hierarchy_ok": False,
            "had_enough_drop": False,
            "had_enough_rise": False,
            "not_enough_drop": False,
            "panic_mode": False,
            "ma_filter_enabled": False,
            "ma_positive": False,
            "ma_negative": False,
        },

        "cooldowns": {},
        "locks": {},
        "locks_meta": {},

        "market": {
            "pair": "XRP/USDC",
            "timeframe": "1m",
            "last": None,
            "prev_last": None,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": None,
            "last_candle_time": None,
            "ts": None,
            "updated_at": None,
        },

        "prices": {
            "pair": "XRP/USDC",
            "source": "freqtrade_api",
            "base": None,
            "last": None,
            "prev_last": None,
            "open": None,
            "peak": None,
            "trough": None,
            "low": None,
            "tick_ts_utc": None,
            "updated_at": None,
        },

        "wallet": {},
        "stake": {
            "dry_run": False,
            "stake_amount": None,
            "stake_currency": None,
        },

        "execution": {},
        "freqtrade": {},
        "ft": {},

        "guardrail": {
            "blocked": False,
            "block_reason": None,
            "day": None,
            "trades_today": 0,
        },

        "stats": {
            "ticks": 0,
            "last_tick_ts": None,
        },

        "meta": {
            "created_at": None,
            "created_utc": None,
            "updated_at": None,
            "updated_utc": None,
            "ft": {},
        },

        "updated_at": None,
        "updated_utc": None,
        "time": None,
        "tick_ts": None,
        "ui_now": None,
    }


def validate_state(state: Any) -> bool:
    if not isinstance(state, dict):
        return False

    required_dict_keys = [
        "bot",
        "decision",
        "engine",
        "levels",
        "cycle",
        "flags",
        "cooldowns",
        "locks",
        "locks_meta",
        "market",
        "prices",
        "wallet",
        "stake",
        "execution",
        "freqtrade",
        "ft",
        "guardrail",
        "stats",
        "meta",
    ]

    for key in required_dict_keys:
        if key not in state or not isinstance(state[key], dict):
            return False

    if "schema_version" not in state:
        return False

    return True


def ensure_state(state: Any | None) -> Dict[str, Any]:
    base = default_state()

    if not isinstance(state, dict):
        return base

    merged = copy.deepcopy(state)

    try:
        merged["schema_version"] = int(merged.get("schema_version", SCHEMA_VERSION))
    except Exception:
        merged["schema_version"] = SCHEMA_VERSION

    _deep_merge_missing(merged, base)

    if not isinstance(merged.get("panic_context"), str) or not merged.get("panic_context"):
        merged["panic_context"] = "NONE"

    if merged.get("last_panic_loss") is None:
        merged["last_panic_loss"] = 0.0

    if merged.get("recovery_context") is None:
        merged["recovery_context"] = False

    return merged


__all__ = [
    "SCHEMA_VERSION",
    "default_state",
    "validate_state",
    "ensure_state",
]
