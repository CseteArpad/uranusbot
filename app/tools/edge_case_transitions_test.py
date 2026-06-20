#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
edge_case_transitions_test.py (CANONICAL TEST TOOL)

Read-only edge-case tests for:
  - BUY_LOCK transitions (SELL -> BUY -> LOCK)
  - Dominance validation (PANIC vs others) + boundary (==) checks
  - Extra transitions: SELL under lock, TTL expiry, clean lock lifecycle

NO state.json writes, NO Freqtrade calls.
"""

import sys
import time

APP_DIR = "/opt/bots/uranus/app"
sys.path.insert(0, APP_DIR)

import tick_runner
import rule_engine


def ok(msg: str) -> None:
    print(f"OK  - {msg}")


def fail(msg: str) -> None:
    print(f"ERR - {msg}")
    raise SystemExit(2)


def assert_true(cond: bool, msg_ok: str, msg_fail: str) -> None:
    if cond:
        ok(msg_ok)
    else:
        fail(msg_fail)


def mk_levels(base: float, trough: float) -> dict:
    # use fixed pct values aligned with default tick_runner env defaults:
    # STD_SELL=3%, PROFITLESS_SELL=0.1%, PANIC_SELL=1%
    # STD_BUY=3%, PROFITLESS_BUY=-0.1%, PANIC_BUY=1%
    return {
        "std_sell": base * (1.0 - 0.03),
        "profitless_sell": base * (1.0 - 0.001),
        "panic_sell": base * (1.0 - 0.01),
        "std_buy": trough * (1.0 + 0.03),
        "profitless_buy": trough * (1.0 - 0.001),
        "panic_buy": trough * (1.0 + 0.01),
    }


def decide_ctx(*, in_position: bool, last: float, prev_last: float, base: float, peak: float | None, trough: float | None) -> dict:
    levels = mk_levels(base=base, trough=(trough if trough is not None else last))
    ctx = {
        "in_position": bool(in_position),
        "last": float(last),
        "prev_last": float(prev_last),
        "base": float(base),
        "peak": peak,
        "trough": trough,
        "levels": levels,
    }
    return rule_engine.decide(ctx)


def test_buy_lock_transitions() -> None:
    print("\n=== BUY_LOCK átmenetek (SELL → BUY → LOCK) ===")

    # Flat + BUY -> BUY allowed + lock created
    state = {"in_position": False, "market": {"pair": "XRP/USDC", "timeframe": "1m", "last": 100.0, "prev_last": 99.0}}
    d_buy = {"action": "BUY", "rule": "TEST_BUY", "reason": "BUY_SIGNAL", "level": "std_buy"}
    out1 = tick_runner.apply_buy_lock(state, d_buy)

    assert_true(out1.get("action") == "BUY",
                "Flat BUY -> decision BUY",
                "Flat BUY -> decision not BUY")

    locks = state.get("locks") if isinstance(state.get("locks"), dict) else {}
    assert_true(isinstance(locks.get("buy_pending"), dict),
                "Flat BUY -> buy_pending lock létrejött",
                "Flat BUY -> buy_pending lock missing")

    # Flat + BUY again -> HOLD with BUY_LOCK
    out2 = tick_runner.apply_buy_lock(state, d_buy)
    assert_true(out2.get("action") == "HOLD",
                "Lock mellett BUY -> HOLD",
                "Lock mellett BUY -> not HOLD")
    assert_true(out2.get("rule") == "BUY_LOCK",
                "Lock mellett BUY -> rule=BUY_LOCK",
                "Lock mellett BUY -> rule not BUY_LOCK")

    # Any non-BUY decision clears lock
    out3 = tick_runner.apply_buy_lock(state, {"action": "HOLD", "rule": "TEST_HOLD", "reason": "HOLD"})
    locks2 = state.get("locks") if isinstance(state.get("locks"), dict) else {}
    assert_true(locks2.get("buy_pending") is None,
                "HOLD mellett lock törlődik",
                "HOLD mellett lock nem törlődött")

    # In_position clears lock and does not lock decisions
    state2 = {"in_position": False, "market": {"pair": "XRP/USDC", "timeframe": "1m", "last": 100.0, "prev_last": 99.0}}
    tick_runner.apply_buy_lock(state2, d_buy)
    state2["in_position"] = True
    out4 = tick_runner.apply_buy_lock(state2, d_buy)
    locks3 = state2.get("locks") if isinstance(state2.get("locks"), dict) else {}
    assert_true(locks3.get("buy_pending") is None,
                "In_position -> lock törlődik",
                "In_position -> lock nem törlődött")
    assert_true(out4.get("action") == "BUY",
                "In_position esetén döntés nem lockkolódik",
                "In_position esetén döntés lockkolódott")


def test_dominance_validation() -> None:
    print("\n=== Dominancia validáció (RuleEngine) ===")

    base = 100.0
    peak = 110.0
    trough = 90.0
    levels = mk_levels(base=base, trough=trough)

    # PANIC SELL environment (in position, falling, last <= panic_sell)
    last = levels["panic_sell"] - 0.01
    prev_last = last + 0.5
    d1 = decide_ctx(in_position=True, last=last, prev_last=prev_last, base=base, peak=peak, trough=None)
    assert_true(d1.get("action") == "SELL",
                "panic_sell környezet -> SELL",
                f"panic_sell környezet -> not SELL ({d1})")
    assert_true(str(d1.get("rule", "")).startswith("PANIC"),
                "panic_sell -> PANIC dominancia",
                f"panic_sell -> not PANIC dominance ({d1})")

    # std+panic sell: last <= std_sell and <= panic_sell -> must be PANIC (dominance)
    last2 = min(levels["std_sell"], levels["panic_sell"]) - 0.01
    prev2 = last2 + 0.5
    d2 = decide_ctx(in_position=True, last=last2, prev_last=prev2, base=base, peak=peak, trough=None)
    assert_true(d2.get("action") == "SELL",
                "std+panic sell -> SELL",
                f"std+panic sell -> not SELL ({d2})")
    assert_true(str(d2.get("rule", "")).startswith("PANIC"),
                "std+panic sell -> PANIC dominancia",
                f"std+panic sell -> not PANIC dominance ({d2})")

    # panic_sell boundary (==) -> SELL
    last3 = levels["panic_sell"]
    prev3 = last3 + 0.5
    d3 = decide_ctx(in_position=True, last=last3, prev_last=prev3, base=base, peak=peak, trough=None)
    assert_true(d3.get("action") == "SELL",
                "panic_sell határérték (==) -> SELL",
                f"panic_sell == -> not SELL ({d3})")

    # STD SELL boundary (==) -> must still be SELL (but can be PANIC if panic also met)
    # Use value just above panic_sell but == std_sell (std_sell is lower than panic_sell? Actually std_sell=97, panic_sell=99)
    # So std_sell boundary won't trigger panic.
    last4 = levels["std_sell"]
    prev4 = last4 + 0.5
    d4 = decide_ctx(in_position=True, last=last4, prev_last=prev4, base=base, peak=peak, trough=None)
    assert_true(d4.get("action") == "SELL",
                "std_sell határérték (==) -> SELL",
                f"std_sell == -> not SELL ({d4})")

    # PANIC BUY environment (flat, rising, last >= panic_buy)
    last5 = levels["panic_buy"] + 0.01
    prev5 = last5 - 0.5
    d5 = decide_ctx(in_position=False, last=last5, prev_last=prev5, base=base, peak=None, trough=trough)
    assert_true(d5.get("action") == "BUY",
                "panic_buy környezet -> BUY",
                f"panic_buy környezet -> not BUY ({d5})")
    assert_true(str(d5.get("rule", "")).startswith("PANIC"),
                "panic_buy -> PANIC dominancia",
                f"panic_buy -> not PANIC dominance ({d5})")

    # std+panic buy: last >= std_buy and >= panic_buy -> must be PANIC
    last6 = max(levels["std_buy"], levels["panic_buy"]) + 0.01
    prev6 = last6 - 0.5
    d6 = decide_ctx(in_position=False, last=last6, prev_last=prev6, base=base, peak=None, trough=trough)
    assert_true(d6.get("action") == "BUY",
                "std+panic buy -> BUY",
                f"std+panic buy -> not BUY ({d6})")
    assert_true(str(d6.get("rule", "")).startswith("PANIC"),
                "std+panic buy -> PANIC dominancia",
                f"std+panic buy -> not PANIC dominance ({d6})")

    # panic_buy boundary (==) -> BUY
    last7 = levels["panic_buy"]
    prev7 = last7 - 0.5
    d7 = decide_ctx(in_position=False, last=last7, prev_last=prev7, base=base, peak=None, trough=trough)
    assert_true(d7.get("action") == "BUY",
                "panic_buy határérték (==) -> BUY",
                f"panic_buy == -> not BUY ({d7})")

    # STD BUY boundary (==) -> BUY (but not PANIC)
    # std_buy=92.7, panic_buy=90.9 -> std_buy boundary is above panic_buy, could satisfy panic too.
    # Avoid that by choosing trough so that panic_buy > std_buy (swap). Use trough=100 (panic_buy=101, std_buy=103).
    trough2 = 100.0
    levels2 = mk_levels(base=base, trough=trough2)
    last8 = levels2["std_buy"]
    prev8 = last8 - 0.5
    ctx8 = {
        "in_position": False,
        "last": float(last8),
        "prev_last": float(prev8),
        "base": float(base),
        "peak": None,
        "trough": float(trough2),
        "levels": levels2,
    }
    d8 = rule_engine.decide(ctx8)
    assert_true(d8.get("action") == "BUY",
                "std_buy határérték (==) -> BUY",
                f"std_buy == -> not BUY ({d8})")


def test_buy_lock_vs_sell() -> None:
    print("\n--- BUY_LOCK vs SELL ---")

    state = {"in_position": False, "market": {"pair": "XRP/USDC", "timeframe": "1m", "last": 100.0, "prev_last": 99.0}}
    d_buy = {"action": "BUY", "rule": "TEST_BUY", "reason": "BUY_SIGNAL", "level": "std_buy"}
    tick_runner.apply_buy_lock(state, d_buy)

    d_sell = {"action": "SELL", "rule": "TEST_SELL", "reason": "SELL_SIGNAL", "level": "panic_sell"}
    out = tick_runner.apply_buy_lock(state, d_sell)

    assert_true(out.get("action") == "SELL",
                "BUY_LOCK nem írja felül a SELL-t",
                f"BUY_LOCK felülírta a SELL-t ({out})")


def test_extra_transitions_ttl_and_lifecycle() -> None:
    print("\n=== Extra átmenetek (TTL + SELL under LOCK + clean lifecycle) ===")

    # Create lock
    state = {"in_position": False, "market": {"pair": "XRP/USDC", "timeframe": "1m", "last": 100.0, "prev_last": 99.0}, "locks": {}}
    d_buy = {"action": "BUY", "rule": "TEST_BUY", "reason": "BUY_SIGNAL", "level": "std_buy"}
    tick_runner.apply_buy_lock(state, d_buy)

    # SELL under lock must remain SELL
    d_sell = {"action": "SELL", "rule": "TEST_SELL", "reason": "SELL_SIGNAL", "level": "panic_sell"}
    out1 = tick_runner.apply_buy_lock(state, d_sell)
    assert_true(out1.get("action") == "SELL",
                "SELL lock mellett is SELL marad",
                f"SELL lock mellett nem SELL ({out1})")

    # TTL expiry: simulate lock older than ttl -> BUY allowed again + new lock created
    ttl = 180
    now = int(time.time())
    if isinstance(state.get("locks"), dict) and isinstance(state["locks"].get("buy_pending"), dict):
        state["locks"]["buy_pending"]["ts"] = now - (ttl + 5)

    out2 = tick_runner.apply_buy_lock(state, d_buy)
    assert_true(out2.get("action") == "BUY",
                "TTL-expired lock -> BUY átengedve",
                f"TTL-expired lock -> BUY not allowed ({out2})")

    locks = state.get("locks") if isinstance(state.get("locks"), dict) else {}
    assert_true(isinstance(locks.get("buy_pending"), dict),
                "TTL-expired lock után új buy_pending létrejön",
                "TTL-expired lock után buy_pending hiányzik")

    # Clean lifecycle: BUY -> lock exists, HOLD -> lock cleared
    out3 = tick_runner.apply_buy_lock(state, d_buy)
    assert_true(out3.get("action") in ("BUY", "HOLD"),
                "BUY -> lock létrejön",
                f"BUY -> unexpected action ({out3})")

    tick_runner.apply_buy_lock(state, {"action": "HOLD", "rule": "TEST_HOLD", "reason": "HOLD"})
    locks2 = state.get("locks") if isinstance(state.get("locks"), dict) else {}
    assert_true(locks2.get("buy_pending") is None,
                "HOLD -> lock törlődik",
                "HOLD -> lock nem törlődött")


def main() -> None:
    test_buy_lock_transitions()
    test_dominance_validation()
    test_buy_lock_vs_sell()
    test_extra_transitions_ttl_and_lifecycle()
    print("\n=== EREDMÉNY ===")
    print("STABLE: edge-case tesztek OK")


if __name__ == "__main__":
    main()
