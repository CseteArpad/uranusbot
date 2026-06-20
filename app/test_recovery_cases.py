#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from typing import Any

sys.path.insert(0, "/opt/bots/uranus/app")
import rule_engine


def ok(msg: str) -> None:
    print(f"OK  - {msg}")


def fail(msg: str) -> None:
    print(f"ERR - {msg}")
    raise SystemExit(2)


def assert_true(cond: bool, ok_msg: str, err_msg: str) -> None:
    if cond:
        ok(ok_msg)
    else:
        fail(err_msg)


def run_case(name: str, ctx: dict[str, Any], cycle: dict[str, Any]) -> dict[str, Any]:
    print(f"\n=== {name} ===")
    res = rule_engine.decide(ctx, cycle)

    print("ACTION =", res.get("action"))
    print("REASON =", res.get("reason"))

    thresholds = res.get("thresholds", {}) if isinstance(res.get("thresholds"), dict) else {}
    if thresholds:
        print("THRESHOLDS:")
        for k in [
            "std_sell_level",
            "recovery_sell_level",
            "recovery_sell_retrace_level",
            "recovery_sell_limit",
            "panic_sell_level",
            "std_buy_level",
            "recovery_buy_level",
            "recovery_buy_rebound_level",
            "recovery_buy_limit",
            "panic_buy_level",
            "expected_recovery_sell",
        ]:
            print(f"  {k} = {thresholds.get(k)}")

    debug = res.get("debug", [])
    if isinstance(debug, list):
        print("DEBUG:")
        for d in debug:
            print(" -", d)

    return res


def test_buy_recovery_positive() -> None:
    ctx = {
        "base": 100.0,
        "last": 93.30,
        "prev_last": 93.00,
        "peak": None,
        "trough": 93.0,
        "in_position": False,
        "ma_short": 93.25,
        "ma_long": 93.10,
        "fee_pct_per_side": 0.0005,
        "fee_total_pct": 0.001,
        "fee_buffer_pct": 0.002,
        "std_sell_pct": 0.004,
        "recovery_sell_retrace_pct": 0.002,
        "panic_sell_pct": 0.01,
        "catastrophe_sell_pct": 0.10,
        "std_buy_pct": 0.004,
        "recovery_buy_rebound_pct": 0.003,
        "panic_buy_pct": 0.01,
        "catastrophe_buy_pct": 0.10,
        "sell_reversal_min_pct": 0.0,
        "recovery_context": True,
        "panic_context": "AFTER_SELL_PANIC",
        "last_panic_loss": 0.0,
        "last_panic_sell_price": 97.0,
        "last_panic_buy_price": None,
        "recovery_anchor_price": 100.0,
        "required_next_buy_mode": "RECOVERY",
        "required_next_sell_mode": "",
        "symbol": "XRP/USDC",
        "timeframe": "1m",
        "expected_recovery_sell_override": None,
    }

    cycle = {
        "recovery_mode": True,
        "recovery_anchor_price": 100.0,
        "last_panic_sell_price": 97.0,
        "required_next_buy_mode": "RECOVERY_OR_LOWER_STANDARD",
        "required_next_sell_mode": None,
    }

    res = run_case("BUY_RECOVERY POSITIVE", ctx, cycle)

    assert_true(
        res.get("action") == "BUY_RECOVERY",
        "BUY_RECOVERY positive -> action BUY_RECOVERY",
        f"BUY_RECOVERY positive -> rossz action: {res.get('action')}",
    )


def test_buy_recovery_impossible_window() -> None:
    ctx = {
        "base": 100.0,
        "last": 93.28,
        "prev_last": 93.00,
        "peak": None,
        "trough": 93.0,
        "in_position": False,
        "ma_short": 93.25,
        "ma_long": 93.10,
        "fee_pct_per_side": 0.001,
        "fee_total_pct": 0.002,
        "fee_buffer_pct": 0.002,
        "std_sell_pct": 0.004,
        "recovery_sell_retrace_pct": 0.002,
        "panic_sell_pct": 0.01,
        "catastrophe_sell_pct": 0.10,
        "std_buy_pct": 0.004,
        "recovery_buy_rebound_pct": 0.003,
        "panic_buy_pct": 0.01,
        "catastrophe_buy_pct": 0.10,
        "sell_reversal_min_pct": 0.0,
        "recovery_context": True,
        "panic_context": "AFTER_SELL_PANIC",
        "last_panic_loss": 0.0,
        "last_panic_sell_price": 97.0,
        "last_panic_buy_price": None,
        "recovery_anchor_price": 100.0,
        "required_next_buy_mode": "RECOVERY",
        "required_next_sell_mode": "",
        "symbol": "XRP/USDC",
        "timeframe": "1m",
        "expected_recovery_sell_override": None,
    }

    cycle = {
        "recovery_mode": True,
        "recovery_anchor_price": 100.0,
        "last_panic_sell_price": 97.0,
        "required_next_buy_mode": "RECOVERY_OR_LOWER_STANDARD",
        "required_next_sell_mode": None,
    }

    res = run_case("BUY_RECOVERY IMPOSSIBLE WINDOW", ctx, cycle)

    debug = res.get("debug", []) if isinstance(res.get("debug"), list) else []
    debug_text = " | ".join(str(x) for x in debug)

    assert_true(
        res.get("action") == "HOLD",
        "BUY impossible window -> action HOLD",
        f"BUY impossible window -> rossz action: {res.get('action')}",
    )
    assert_true(
        "recovery_window_impossible" in debug_text,
        "BUY impossible window -> debug contains recovery_window_impossible",
        "BUY impossible window -> debug nem tartalmazza: recovery_window_impossible",
    )


def test_sell_recovery_positive() -> None:
    ctx = {
        "base": 100.0,
        "last": 109.60,
        "prev_last": 109.95,
        "peak": 110.0,
        "trough": 100.0,
        "in_position": True,
        "ma_short": 109.70,
        "ma_long": 109.90,
        "fee_pct_per_side": 0.001,
        "fee_total_pct": 0.002,
        "fee_buffer_pct": 0.002,
        "std_sell_pct": 0.004,
        "recovery_sell_retrace_pct": 0.002,
        "panic_sell_pct": 0.01,
        "catastrophe_sell_pct": 0.10,
        "std_buy_pct": 0.004,
        "recovery_buy_rebound_pct": 0.003,
        "panic_buy_pct": 0.01,
        "catastrophe_buy_pct": 0.10,
        "sell_reversal_min_pct": 0.0,
        "recovery_context": True,
        "panic_context": "AFTER_BUY_PANIC",
        "last_panic_loss": 0.0,
        "last_panic_sell_price": None,
        "last_panic_buy_price": 103.0,
        "recovery_anchor_price": 103.0,
        "required_next_buy_mode": "",
        "required_next_sell_mode": "RECOVERY",
        "symbol": "XRP/USDC",
        "timeframe": "1m",
        "expected_recovery_sell_override": None,
    }

    cycle = {
        "recovery_mode": True,
        "recovery_anchor_price": 103.0,
        "last_panic_buy_price": 103.0,
        "required_next_buy_mode": None,
        "required_next_sell_mode": "RECOVERY_OR_HIGHER_STANDARD",
    }

    res = run_case("SELL_RECOVERY POSITIVE", ctx, cycle)

    assert_true(
        res.get("action") == "SELL_RECOVERY",
        "SELL_RECOVERY positive -> action SELL_RECOVERY",
        f"SELL_RECOVERY positive -> rossz action: {res.get('action')}",
    )


def test_sell_recovery_impossible_window() -> None:
    ctx = {
        "base": 100.0,
        "last": 109.60,
        "prev_last": 109.95,
        "peak": 110.0,
        "trough": 100.0,
        "in_position": True,
        "ma_short": 109.70,
        "ma_long": 109.90,
        "fee_pct_per_side": 0.01,
        "fee_total_pct": 0.02,
        "fee_buffer_pct": 0.02,
        "std_sell_pct": 0.004,
        "recovery_sell_retrace_pct": 0.002,
        "panic_sell_pct": 0.01,
        "catastrophe_sell_pct": 0.10,
        "std_buy_pct": 0.004,
        "recovery_buy_rebound_pct": 0.003,
        "panic_buy_pct": 0.01,
        "catastrophe_buy_pct": 0.10,
        "sell_reversal_min_pct": 0.0,
        "recovery_context": True,
        "panic_context": "AFTER_BUY_PANIC",
        "last_panic_loss": 0.0,
        "last_panic_sell_price": None,
        "last_panic_buy_price": 103.0,
        "recovery_anchor_price": 103.0,
        "required_next_buy_mode": "",
        "required_next_sell_mode": "RECOVERY",
        "symbol": "XRP/USDC",
        "timeframe": "1m",
        "expected_recovery_sell_override": None,
    }

    cycle = {
        "recovery_mode": True,
        "recovery_anchor_price": 103.0,
        "last_panic_buy_price": 103.0,
        "required_next_buy_mode": None,
        "required_next_sell_mode": "RECOVERY_OR_HIGHER_STANDARD",
    }

    res = run_case("SELL_RECOVERY IMPOSSIBLE WINDOW", ctx, cycle)

    debug = res.get("debug", []) if isinstance(res.get("debug"), list) else []
    debug_text = " | ".join(str(x) for x in debug)

    assert_true(
        res.get("action") == "HOLD",
        "SELL impossible window -> action HOLD",
        f"SELL impossible window -> rossz action: {res.get('action')}",
    )
    assert_true(
        "recovery_window_impossible" in debug_text,
        "SELL impossible window -> debug contains recovery_window_impossible",
        "SELL impossible window -> debug nem tartalmazza: recovery_window_impossible",
    )


def main() -> int:
    print("URANUS CANONICAL RECOVERY TESTS")
    print("API = rule_engine.decide(ctx, cycle)")

    test_buy_recovery_positive()
    test_buy_recovery_impossible_window()
    test_sell_recovery_positive()
    test_sell_recovery_impossible_window()

    print("\nALL OK - canonical recovery cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
