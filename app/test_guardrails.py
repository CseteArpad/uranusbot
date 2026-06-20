#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import copy
from pathlib import Path

sys.path.insert(0, "/opt/bots/uranus/app")

import tick_runner


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


def make_state() -> dict:
    today = tick_runner.now_utc_iso()[:10]
    return {
        "in_position": False,
        "market": {
            "pair": "XRP/USDC",
            "timeframe": "1m",
            "last": 100.0,
            "prev_last": 99.5,
        },
        "execution": {},
        "guardrail": {
            "day": today,
            "trades_today": 0,
            "blocked": False,
            "block_reason": None,
        },
        "locks": {},
        "cycle": {},
        "entry_price": 100.0,
    }

def make_buy_decision() -> dict:
    return {
        "action": "BUY",
        "rule": "BUY_STANDARD",
        "reason": "test_buy_signal",
    }


def make_sell_decision() -> dict:
    return {
        "action": "SELL",
        "rule": "SELL_STANDARD",
        "reason": "test_sell_signal",
    }


def test_kill_switch_blocks() -> None:
    state = make_state()
    decision = make_buy_decision()

    orig = tick_runner._runtime_guardrail_flags
    try:
        tick_runner._runtime_guardrail_flags = lambda: (True, 10, 2.0)
        allowed, reason = tick_runner.guardrail_allows_execution(state, decision)
        assert_true(allowed is False, "KILL_SWITCH -> tiltás", "KILL_SWITCH nem tiltott")
        assert_true(reason == "KILL_SWITCH", "KILL_SWITCH ok", f"KILL_SWITCH rossz ok: {reason}")
    finally:
        tick_runner._runtime_guardrail_flags = orig


def test_max_trades_blocks() -> None:
    state = make_state()
    state["guardrail"]["trades_today"] = 10
    decision = make_buy_decision()

    orig = tick_runner._runtime_guardrail_flags
    try:
        tick_runner._runtime_guardrail_flags = lambda: (False, 10, 2.0)
        allowed, reason = tick_runner.guardrail_allows_execution(state, decision)
        assert_true(allowed is False, "MAX_TRADES_PER_DAY -> tiltás", "MAX_TRADES_PER_DAY nem tiltott")
        assert_true(reason == "MAX_TRADES_PER_DAY", "MAX_TRADES_PER_DAY ok", f"MAX_TRADES_PER_DAY rossz ok: {reason}")
    finally:
        tick_runner._runtime_guardrail_flags = orig


def test_daily_loss_cap_blocks() -> None:
    state = make_state()
    decision = make_sell_decision()

    orig_flags = tick_runner._runtime_guardrail_flags
    orig_loss = tick_runner._estimate_open_trade_loss_pct
    try:
        tick_runner._runtime_guardrail_flags = lambda: (False, 10, 2.0)
        tick_runner._estimate_open_trade_loss_pct = lambda _state: 2.5
        allowed, reason = tick_runner.guardrail_allows_execution(state, decision)
        assert_true(allowed is False, "DAILY_LOSS_CAP_PCT -> tiltás", "DAILY_LOSS_CAP_PCT nem tiltott")
        assert_true(str(reason).startswith("DAILY_LOSS_CAP_PCT:"), "DAILY_LOSS_CAP_PCT ok", f"DAILY_LOSS_CAP_PCT rossz ok: {reason}")
    finally:
        tick_runner._runtime_guardrail_flags = orig_flags
        tick_runner._estimate_open_trade_loss_pct = orig_loss


def test_buy_lock_holds_second_buy() -> None:
    state = make_state()
    d_buy = make_buy_decision()

    orig_exec_flags = tick_runner._runtime_exec_flags
    try:
        tick_runner._runtime_exec_flags = lambda: (True, False, False)

        out1 = tick_runner.apply_buy_lock(state, copy.deepcopy(d_buy))
        assert_true(out1.get("action") == "BUY", "első BUY átmegy", f"első BUY nem ment át: {out1}")
        assert_true(
            isinstance(state.get("locks", {}).get("buy_pending"), dict),
            "első BUY után buy_pending létrejön",
            f"első BUY után nincs buy_pending: {state.get('locks')}",
        )

        out2 = tick_runner.apply_buy_lock(state, copy.deepcopy(d_buy))
        assert_true(out2.get("action") == "HOLD", "második BUY -> HOLD", f"második BUY nem HOLD: {out2}")
        assert_true(out2.get("rule") == "BUY_LOCK", "második BUY -> BUY_LOCK", f"második BUY nem BUY_LOCK: {out2}")
    finally:
        tick_runner._runtime_exec_flags = orig_exec_flags

def test_api_unavailable_safe_hold() -> None:
    state = make_state()

    decision = tick_runner.normalize_decision(
        action="HOLD",
        rule="API_UNAVAILABLE",
        reason="API_UNAVAILABLE: test",
        level="none",
    )
    tick_runner.apply_decision_contract(state, decision)

    assert_true(state.get("decision_action") == "HOLD", "API_UNAVAILABLE -> HOLD", f"API_UNAVAILABLE nem HOLD: {state.get('decision_action')}")
    assert_true(state.get("decision_reason") == "API_UNAVAILABLE: test", "API_UNAVAILABLE reason ok", f"API_UNAVAILABLE reason rossz: {state.get('decision_reason')}")


def main() -> int:
    print("URANUS GUARDRAIL TESTS")

    test_kill_switch_blocks()
    test_max_trades_blocks()
    test_daily_loss_cap_blocks()
    test_buy_lock_holds_second_buy()
    test_api_unavailable_safe_hold()

    print("\nALL OK - guardrails passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
