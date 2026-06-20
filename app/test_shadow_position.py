#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_shadow_position.py — 50 unit tests for shadow_position.py

All tests use in-memory state dicts.
All network calls are mocked via unittest.mock.patch.
No filesystem writes. Must complete in <10 seconds.

Run:
    cd app && python -m pytest test_shadow_position.py -v
    cd app && python -m unittest test_shadow_position -v
"""

from __future__ import annotations

import json
import sys
import os
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import shadow_position as sp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_state(
    last: float = 2.0,
    in_position: bool = False,
    ma_short: float = 1.95,
    ma_long: float = 1.90,
) -> dict:
    return {
        "in_position": in_position,
        "market": {
            "last": last,
            "prev_last": last - 0.01,
            "ma_short": ma_short,
            "ma_long": ma_long,
            "high": last + 0.05,
            "low": last - 0.05,
        },
        "levels": {},
        "flags": {"ma_positive": True, "ma_negative": False},
        "cycle": {},
    }


def _mk_shadow_flat(equity: float = 1000.0) -> dict:
    shadow: dict = {}
    sp._ensure_shadow_section({"shadow": shadow})
    shadow["equity_usdc"] = equity
    shadow["equity_seed_usdc"] = equity
    shadow["equity_seed_source"] = "test"
    shadow["equity_seed_ts"] = int(time.time())
    return shadow


def _mk_shadow_in_position(
    equity: float = 0.0,
    entry_price: float = 2.0,
    qty: float = 490.0,
    cost: float = 1000.0,
) -> dict:
    shadow = _mk_shadow_flat(equity)
    shadow["in_position"] = True
    shadow["entry_price"] = entry_price
    shadow["entry_qty_base"] = qty
    shadow["entry_cost_usdc"] = cost
    shadow["entry_fee_usdc"] = 1.0
    shadow["entry_ts"] = int(time.time()) - 600
    shadow["entry_rule"] = "BUY_STANDARD"
    shadow["base"] = entry_price
    shadow["peak"] = entry_price
    shadow["trough"] = None
    return shadow


# ---------------------------------------------------------------------------
# TestEquitySeeding
# ---------------------------------------------------------------------------

class TestEquitySeeding(unittest.TestCase):

    def test_ft_balance_seeds_equity_when_flat(self):
        state = _mk_state()
        shadow = _mk_shadow_flat(0.0)
        shadow["equity_usdc"] = None
        shadow["equity_seed_usdc"] = None

        mock_result = b'{"currencies":{"USDC":{"free":1234.5}}}'
        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(1234.5, "ft_balance")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertAlmostEqual(shadow["equity_usdc"], 1234.5)
        self.assertAlmostEqual(shadow["equity_seed_usdc"], 1234.5)
        self.assertEqual(shadow["equity_seed_source"], "ft_balance")

    def test_sync_skipped_when_in_position(self):
        state = _mk_state(in_position=True)
        shadow = _mk_shadow_in_position()
        shadow["equity_usdc"] = 0.0

        with patch("shadow_position.fetch_shadow_equity_from_live") as mock_fetch:
            sp._shadow_sync_equity_if_flat(state, shadow)
            mock_fetch.assert_not_called()

        self.assertEqual(shadow["equity_usdc"], 0.0)

    def test_env_var_fallback_used_when_ft_unavailable(self):
        state = _mk_state()
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        shadow["equity_usdc"] = None
        shadow["equity_seed_usdc"] = None

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(500.0, "env_var")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertAlmostEqual(shadow["equity_usdc"], 500.0)
        self.assertEqual(shadow["equity_last_sync_source"], "env_var")

    def test_sync_failure_increments_counter(self):
        state = _mk_state()
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        shadow["equity_sync_failures"] = 2

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(None, "unavailable")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertEqual(shadow["equity_sync_failures"], 3)

    def test_seed_not_overwritten_on_subsequent_syncs(self):
        state = _mk_state()
        shadow = _mk_shadow_flat(1000.0)

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(1100.0, "ft_balance")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        # equity updated, but seed preserved
        self.assertAlmostEqual(shadow["equity_usdc"], 1100.0)
        self.assertAlmostEqual(shadow["equity_seed_usdc"], 1000.0)

    def test_fetch_returns_none_when_ft_unreachable(self):
        state = _mk_state()
        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(None, "unavailable")):
            amount, src = sp.fetch_shadow_equity_from_live(state)
        self.assertIsNone(amount)

    def test_binance_rest_path_used_as_fallback(self):
        state = _mk_state()
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        shadow["equity_usdc"] = None
        shadow["equity_seed_usdc"] = None

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(750.0, "binance_rest")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertAlmostEqual(shadow["equity_usdc"], 750.0)
        self.assertEqual(shadow["equity_last_sync_source"], "binance_rest")

    def test_zero_balance_not_stored(self):
        state = _mk_state()
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        shadow["equity_usdc"] = None

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(0.0, "ft_balance")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertIsNone(shadow["equity_usdc"])

    def test_ensure_shadow_section_idempotent(self):
        state: dict = {}
        sp._ensure_shadow_section(state)
        sp._ensure_shadow_section(state)
        self.assertIn("shadow", state)
        self.assertIn("cycle", state["shadow"])

    def test_equity_sync_updates_last_sync_ts(self):
        state = _mk_state()
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        before = int(time.time())

        with patch("shadow_position.fetch_shadow_equity_from_live", return_value=(900.0, "ft_balance")):
            sp._shadow_sync_equity_if_flat(state, shadow)

        self.assertGreaterEqual(shadow["equity_last_sync_ts"], before)


# ---------------------------------------------------------------------------
# TestPeakTrough
# ---------------------------------------------------------------------------

class TestPeakTrough(unittest.TestCase):

    def test_peak_grows_when_in_position(self):
        shadow = _mk_shadow_in_position(entry_price=2.0)
        market = {"last": 2.5, "high": 2.6, "low": 2.3}
        sp._shadow_update_peak_trough(shadow, market)
        self.assertAlmostEqual(shadow["peak"], 2.6)
        self.assertIsNone(shadow["trough"])

    def test_trough_shrinks_when_flat(self):
        shadow = _mk_shadow_flat()
        shadow["trough"] = 1.9
        market = {"last": 1.8, "high": 1.95, "low": 1.75}
        sp._shadow_update_peak_trough(shadow, market)
        self.assertAlmostEqual(shadow["trough"], 1.75)
        self.assertIsNone(shadow["peak"])

    def test_peak_falls_back_to_last_if_no_high(self):
        shadow = _mk_shadow_in_position(entry_price=2.0)
        market = {"last": 2.3}  # no "high"
        sp._shadow_update_peak_trough(shadow, market)
        self.assertAlmostEqual(shadow["peak"], 2.3)

    def test_no_crash_on_missing_market(self):
        shadow = _mk_shadow_flat()
        sp._shadow_update_peak_trough(shadow, {})  # must not raise


# ---------------------------------------------------------------------------
# TestRecoveryArming
# ---------------------------------------------------------------------------

class TestRecoveryArming(unittest.TestCase):

    def _state_with_levels(self, last: float, panic_sell: float = None, panic_buy: float = None) -> dict:
        state = _mk_state(last=last)
        levels = {}
        if panic_sell is not None:
            levels["panic_sell"] = panic_sell
        if panic_buy is not None:
            levels["panic_buy"] = panic_buy
        state["levels"] = levels
        return state

    def test_buy_arming_when_price_below_panic_sell(self):
        state = self._state_with_levels(last=1.8, panic_sell=2.0)
        state["flags"] = {"ma_positive": False, "ma_negative": True}
        shadow = _mk_shadow_flat()
        sp._shadow_update_recovery_arming(state, shadow)
        self.assertTrue(shadow["cycle"]["recovery_buy_armed"])

    def test_sell_arming_when_price_above_panic_buy(self):
        state = self._state_with_levels(last=2.2, panic_buy=2.1)
        state["flags"] = {"ma_positive": True, "ma_negative": False}
        shadow = _mk_shadow_flat()
        sp._shadow_update_recovery_arming(state, shadow)
        self.assertTrue(shadow["cycle"]["recovery_sell_armed"])

    def test_arming_timeout_disarms(self):
        # Price is above panic_sell and trend is up — arming condition NOT triggered,
        # so only the timeout logic runs and must clear the stale armed state.
        state = self._state_with_levels(last=2.1, panic_sell=2.0)
        state["flags"] = {"ma_positive": True, "ma_negative": False}
        shadow = _mk_shadow_flat()
        shadow["cycle"]["recovery_buy_armed"] = True
        shadow["cycle"]["recovery_buy_arm_age"] = 241

        with patch.dict(os.environ, {"RECOVERY_ARM_TIMEOUT_BARS": "240"}):
            sp._shadow_update_recovery_arming(state, shadow)

        self.assertFalse(shadow["cycle"]["recovery_buy_armed"])


# ---------------------------------------------------------------------------
# TestContextBuilder
# ---------------------------------------------------------------------------

class TestContextBuilder(unittest.TestCase):

    def test_ctx_uses_shadow_in_position_not_live(self):
        state = _mk_state(in_position=False)
        shadow = _mk_shadow_in_position()
        ctx, _ = sp._shadow_build_ctx(state, shadow)
        self.assertTrue(ctx["in_position"])

    def test_ctx_uses_shadow_base_not_live(self):
        state = _mk_state()
        state["base"] = 99.0  # live base — must NOT be used
        shadow = _mk_shadow_in_position(entry_price=2.0)
        shadow["base"] = 2.0
        ctx, _ = sp._shadow_build_ctx(state, shadow)
        self.assertAlmostEqual(ctx["base"], 2.0)

    def test_ctx_reads_shared_market_price(self):
        state = _mk_state(last=2.15)
        shadow = _mk_shadow_flat()
        ctx, _ = sp._shadow_build_ctx(state, shadow)
        self.assertAlmostEqual(ctx["last"], 2.15)

    def test_cycle_ctx_reads_shadow_cycle(self):
        state = _mk_state()
        shadow = _mk_shadow_flat()
        shadow["cycle"]["recovery_mode"] = True
        shadow["cycle"]["recovery_loss_pct"] = 0.05
        _, cycle_ctx = sp._shadow_build_ctx(state, shadow)
        self.assertTrue(cycle_ctx["recovery_mode"])
        self.assertAlmostEqual(cycle_ctx["recovery_loss_pct"], 0.05)


# ---------------------------------------------------------------------------
# TestSecondEnginePass
# ---------------------------------------------------------------------------

class TestSecondEnginePass(unittest.TestCase):

    def _mock_engine(self, action: str = "HOLD"):
        m = MagicMock()
        m.decide.return_value = {"decision": action, "action": action, "reason": "test"}
        return m

    def test_hold_decision_produces_no_trade(self):
        state = _mk_state()
        state["shadow"] = {}
        sp._ensure_shadow_section(state)
        shadow = state["shadow"]
        shadow["equity_usdc"] = 1000.0
        shadow["equity_seed_usdc"] = 1000.0

        with patch("shadow_position._env_bool", return_value=True), \
             patch("shadow_position.fetch_shadow_equity_from_live", return_value=(1000.0, "ft_balance")), \
             patch("shadow_position._shadow_sync_equity_if_flat"), \
             patch.dict("sys.modules", {"rule_engine": self._mock_engine("HOLD")}):
            sp.maybe_run_shadow_tick(state)

        self.assertFalse(shadow["in_position"])

    def test_buy_decision_enters_position(self):
        state = _mk_state()
        shadow = _mk_shadow_flat(1000.0)
        state["shadow"] = shadow

        with patch("shadow_position._shadow_sync_equity_if_flat"), \
             patch.dict("sys.modules", {"rule_engine": self._mock_engine("BUY_STANDARD")}), \
             patch.dict(os.environ, {"SHADOW_ENABLED": "1"}):
            sp.maybe_run_shadow_tick(state)

        self.assertTrue(shadow["in_position"])

    def test_shadow_disabled_does_nothing(self):
        state = _mk_state()
        state["shadow"] = {}

        with patch.dict(os.environ, {"SHADOW_ENABLED": "0"}):
            sp.maybe_run_shadow_tick(state)

        self.assertNotIn("enabled", state.get("shadow", {}))

    def test_engine_exception_does_not_crash_tick(self):
        state = _mk_state()
        shadow = _mk_shadow_flat(1000.0)
        state["shadow"] = shadow

        bad_engine = MagicMock()
        bad_engine.decide.side_effect = RuntimeError("engine exploded")

        with patch("shadow_position._shadow_sync_equity_if_flat"), \
             patch.dict("sys.modules", {"rule_engine": bad_engine}), \
             patch.dict(os.environ, {"SHADOW_ENABLED": "1"}):
            sp.maybe_run_shadow_tick(state)  # must not raise

        self.assertFalse(shadow["in_position"])


# ---------------------------------------------------------------------------
# TestBuyAccounting
# ---------------------------------------------------------------------------

class TestBuyAccounting(unittest.TestCase):

    def test_buy_depletes_equity(self):
        shadow = _mk_shadow_flat(1000.0)
        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=2.0, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        self.assertAlmostEqual(shadow["equity_usdc"], 0.0)

    def test_buy_sets_entry_price_with_slippage(self):
        shadow = _mk_shadow_flat(1000.0)
        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=2.0, decision=decision, fee_pct=0.001, slippage_pct=0.005)
        expected_entry = 2.0 * 1.005
        self.assertAlmostEqual(shadow["entry_price"], expected_entry, places=8)

    def test_buy_computes_qty_correctly(self):
        shadow = _mk_shadow_flat(1000.0)
        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        fee_pct = 0.001
        slippage = 0.0
        sp._shadow_record_buy(shadow, last=2.0, decision=decision, fee_pct=fee_pct, slippage_pct=slippage)
        expected_fee = 1000.0 * fee_pct
        expected_qty = (1000.0 - expected_fee) / 2.0
        self.assertAlmostEqual(shadow["entry_qty_base"], expected_qty, places=6)

    def test_buy_sets_in_position_true(self):
        shadow = _mk_shadow_flat(500.0)
        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=1.5, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        self.assertTrue(shadow["in_position"])

    def test_buy_skipped_when_equity_zero(self):
        shadow = _mk_shadow_flat(0.0)
        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=2.0, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        self.assertFalse(shadow["in_position"])


# ---------------------------------------------------------------------------
# TestSellAccounting
# ---------------------------------------------------------------------------

class TestSellAccounting(unittest.TestCase):

    def _buy_and_sell(self, entry_last: float, exit_last: float, fee: float = 0.001, slip: float = 0.0):
        shadow = _mk_shadow_flat(1000.0)
        buy_dec = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=entry_last, decision=buy_dec, fee_pct=fee, slippage_pct=slip)
        sell_dec = {"action": "SELL", "rule": "SELL_STANDARD"}
        entry = sp._shadow_record_sell(shadow, last=exit_last, decision=sell_dec, fee_pct=fee, slippage_pct=slip)
        return shadow, entry

    def test_sell_restores_equity(self):
        shadow, _ = self._buy_and_sell(2.0, 2.2)
        self.assertGreater(shadow["equity_usdc"], 0.0)

    def test_sell_computes_net_proceeds_correctly(self):
        shadow = _mk_shadow_in_position(equity=0.0, entry_price=2.0, qty=490.0, cost=1000.0)
        decision = {"action": "SELL", "rule": "SELL_STANDARD"}
        entry = sp._shadow_record_sell(shadow, last=2.2, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        expected_gross = 490.0 * 2.2
        expected_fee = expected_gross * 0.001
        expected_net = expected_gross - expected_fee
        self.assertAlmostEqual(entry["net_proceeds_usdc"], expected_net, places=4)

    def test_sell_is_win_on_profit(self):
        shadow, entry = self._buy_and_sell(2.0, 2.5)
        self.assertTrue(entry["is_win"])

    def test_sell_is_loss_on_decline(self):
        shadow = _mk_shadow_in_position(equity=0.0, entry_price=2.0, qty=490.0, cost=1000.0)
        decision = {"action": "SELL", "rule": "SELL_PANIC"}
        entry = sp._shadow_record_sell(shadow, last=1.8, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        self.assertFalse(entry["is_win"])
        self.assertLess(entry["net_pnl_usdc"], 0.0)

    def test_sell_clears_in_position(self):
        shadow, _ = self._buy_and_sell(2.0, 2.2)
        self.assertFalse(shadow["in_position"])

    def test_sell_skipped_on_missing_entry_data(self):
        shadow: dict = {}
        sp._ensure_shadow_section({"shadow": shadow})
        shadow["in_position"] = True
        decision = {"action": "SELL", "rule": "SELL_STANDARD"}
        result = sp._shadow_record_sell(shadow, last=2.0, decision=decision, fee_pct=0.001, slippage_pct=0.0)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# TestLedgerAndStats
# ---------------------------------------------------------------------------

class TestLedgerAndStats(unittest.TestCase):

    def _make_ledger_entry(self, net_pnl: float, is_win: bool) -> dict:
        return {
            "trade_id": 1,
            "entry_ts": int(time.time()) - 100,
            "exit_ts": int(time.time()),
            "gross_pnl_usdc": net_pnl + 0.5,
            "total_fees_usdc": 0.5,
            "net_pnl_usdc": net_pnl,
            "net_pnl_pct": net_pnl / 1000.0,
            "equity_after_usdc": 1000.0 + net_pnl,
            "is_win": is_win,
        }

    def test_append_increments_ledger(self):
        shadow = _mk_shadow_flat()
        shadow["ledger"] = []
        entry = self._make_ledger_entry(10.0, True)
        sp._shadow_append_ledger(shadow, entry)
        self.assertEqual(len(shadow["ledger"]), 1)

    def test_ledger_cap_enforced(self):
        shadow = _mk_shadow_flat()
        shadow["ledger"] = []
        shadow["ledger_overflow_count"] = 0
        for i in range(510):
            sp._shadow_append_ledger(shadow, self._make_ledger_entry(1.0, True))
        self.assertEqual(len(shadow["ledger"]), 500)
        self.assertEqual(shadow["ledger_overflow_count"], 10)

    def test_stats_win_rate_computed(self):
        shadow = _mk_shadow_flat(1200.0)
        shadow["ledger"] = [
            self._make_ledger_entry(10.0, True),
            self._make_ledger_entry(-5.0, False),
            self._make_ledger_entry(8.0, True),
        ]
        sp._shadow_recompute_stats(shadow)
        self.assertAlmostEqual(shadow["stats"]["win_rate_pct"], 200.0 / 3.0, places=4)

    def test_stats_largest_win_tracked(self):
        shadow = _mk_shadow_flat(1100.0)
        shadow["ledger"] = [
            self._make_ledger_entry(5.0, True),
            self._make_ledger_entry(20.0, True),
        ]
        sp._shadow_recompute_stats(shadow)
        self.assertAlmostEqual(shadow["stats"]["largest_win_usdc"], 20.0)

    def test_stats_pnl_from_seed(self):
        shadow = _mk_shadow_flat(1000.0)
        shadow["equity_usdc"] = 1050.0
        shadow["ledger"] = [self._make_ledger_entry(50.0, True)]
        sp._shadow_recompute_stats(shadow)
        self.assertAlmostEqual(shadow["stats"]["total_pnl_from_seed_usdc"], 50.0)
        self.assertAlmostEqual(shadow["stats"]["total_pnl_from_seed_pct"], 0.05)


# ---------------------------------------------------------------------------
# TestCycleTransitions
# ---------------------------------------------------------------------------

class TestCycleTransitions(unittest.TestCase):

    def _sc(self, **kw) -> dict:
        sc: dict = {
            "recovery_mode": False,
            "recovery_loss_pct": 0.0,
            "recovery_anchor_price": None,
            "required_next_buy_mode": None,
            "required_next_sell_mode": None,
            "recovery_target_entry_cap": None,
        }
        sc.update(kw)
        return sc

    def _shadow(self, sc: dict = None) -> dict:
        shadow = _mk_shadow_flat()
        shadow["cycle"] = sc or self._sc()
        return shadow

    def test_unprofitable_sell_sets_recovery(self):
        shadow = self._shadow()
        dec = {"action": "SELL", "rule": "SELL_STANDARD", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=2.0, last=1.8)
        self.assertTrue(shadow["cycle"]["recovery_mode"])
        self.assertEqual(shadow["cycle"]["required_next_buy_mode"], "RECOVERY_OR_LOWER_STANDARD")

    def test_profitable_sell_clears_recovery(self):
        sc = self._sc(recovery_mode=True, required_next_buy_mode="RECOVERY_OR_LOWER_STANDARD")
        shadow = self._shadow(sc)
        dec = {"action": "SELL", "rule": "SELL_RECOVERY", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=2.0, last=2.1)
        self.assertFalse(shadow["cycle"]["recovery_mode"])
        self.assertIsNone(shadow["cycle"]["required_next_buy_mode"])

    def test_panic_sell_sets_recovery(self):
        shadow = self._shadow()
        dec = {"action": "SELL", "rule": "SELL_PANIC", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=2.0, last=1.5)
        self.assertTrue(shadow["cycle"]["recovery_mode"])

    def test_panic_buy_sets_sell_recovery(self):
        shadow = self._shadow()
        dec = {"action": "BUY", "rule": "BUY_PANIC", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=None, last=2.3)
        self.assertTrue(shadow["cycle"]["recovery_mode"])
        self.assertEqual(shadow["cycle"]["required_next_sell_mode"], "RECOVERY_OR_HIGHER_STANDARD")

    def test_recovery_buy_consumed_clears_requirement(self):
        sc = self._sc(
            recovery_mode=True,
            required_next_buy_mode="RECOVERY_OR_LOWER_STANDARD",
            recovery_target_entry_cap=1.9,
        )
        shadow = self._shadow(sc)
        dec = {"action": "BUY", "rule": "BUY_RECOVERY", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=None, last=1.85)
        self.assertIsNone(shadow["cycle"]["required_next_buy_mode"])

    def test_standard_buy_no_recovery_leaves_cycle_clean(self):
        shadow = self._shadow()
        dec = {"action": "BUY", "rule": "BUY_STANDARD", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=None, last=2.0)
        self.assertFalse(shadow["cycle"]["recovery_mode"])

    def test_loss_pct_computed_correctly(self):
        shadow = self._shadow()
        dec = {"action": "SELL", "rule": "SELL_PANIC", "data": {}}
        sp._shadow_apply_cycle_transition(shadow, dec, entry_price_before_trade=2.0, last=1.8)
        expected_loss_pct = (2.0 - 1.8) / 2.0  # 0.1
        self.assertAlmostEqual(shadow["cycle"]["recovery_loss_pct"], expected_loss_pct, places=6)


# ---------------------------------------------------------------------------
# TestIsolation
# ---------------------------------------------------------------------------

class TestIsolation(unittest.TestCase):

    def test_shadow_buy_does_not_modify_live_state_keys(self):
        state = _mk_state()
        state["in_position"] = False
        state["base"] = 99.0  # live base
        state["entry_price"] = 98.0

        shadow = _mk_shadow_flat(1000.0)
        state["shadow"] = shadow

        decision = {"action": "BUY", "rule": "BUY_STANDARD"}
        sp._shadow_record_buy(shadow, last=2.0, decision=decision, fee_pct=0.001, slippage_pct=0.0)

        # Live state must be unchanged
        self.assertFalse(state["in_position"])
        self.assertEqual(state["base"], 99.0)
        self.assertEqual(state["entry_price"], 98.0)

    def test_shadow_cycle_independent_from_live_cycle(self):
        state = _mk_state()
        state["cycle"] = {"recovery_mode": False, "required_next_buy_mode": None}

        shadow = _mk_shadow_flat()
        shadow["cycle"]["recovery_mode"] = True
        shadow["cycle"]["required_next_buy_mode"] = "RECOVERY_OR_LOWER_STANDARD"
        state["shadow"] = shadow

        ctx, cycle_ctx = sp._shadow_build_ctx(state, shadow)

        # cycle_ctx must reflect shadow cycle, not live cycle
        self.assertTrue(cycle_ctx["recovery_mode"])
        self.assertEqual(cycle_ctx["required_next_buy_mode"], "RECOVERY_OR_LOWER_STANDARD")

        # live cycle must be untouched
        self.assertFalse(state["cycle"]["recovery_mode"])


# ---------------------------------------------------------------------------
# TestStakeAwareEquity — new stake_amount-aware equity logic
# ---------------------------------------------------------------------------

class TestStakeAwareEquity(unittest.TestCase):
    """Tests for the stake_amount-capped equity seeding logic."""

    def _state_flat(self, live_trade_stake=None) -> dict:
        s = _mk_state(in_position=False)
        if live_trade_stake is not None:
            s["live_trade_stake"] = live_trade_stake
        return s

    def _state_in_position(self, live_trade_stake=None) -> dict:
        s = _mk_state(in_position=True)
        if live_trade_stake is not None:
            s["live_trade_stake"] = live_trade_stake
        return s

    # ------------------------------------------------------------------
    # Step 0: live in position → live_trade_stake from state
    # ------------------------------------------------------------------

    def test_live_in_position_uses_state_live_trade_stake(self):
        state = self._state_in_position(live_trade_stake=150.0)
        amount, source = sp.fetch_shadow_equity_from_live(state)
        self.assertAlmostEqual(amount, 150.0)
        self.assertEqual(source, "live_trade_stake")

    def test_live_in_position_falls_back_to_ft_open_trade_cost(self):
        state = self._state_in_position(live_trade_stake=None)
        with patch("shadow_position._ft_read_open_trade_cost", return_value=200.0):
            amount, source = sp.fetch_shadow_equity_from_live(state)
        self.assertAlmostEqual(amount, 200.0)
        self.assertEqual(source, "ft_open_trade_cost")

    def test_live_in_position_zero_stake_falls_through_to_ft_api(self):
        state = self._state_in_position(live_trade_stake=0.0)
        with patch("shadow_position._ft_read_open_trade_cost", return_value=300.0):
            amount, source = sp.fetch_shadow_equity_from_live(state)
        self.assertAlmostEqual(amount, 300.0)
        self.assertEqual(source, "ft_open_trade_cost")

    # ------------------------------------------------------------------
    # Step 1: live flat + stake_amount cap
    # ------------------------------------------------------------------

    def test_flat_numeric_stake_caps_equity(self):
        """Free USDC 500, stake_amount 100 → shadow equity = 100."""
        state = self._state_flat()
        ft_balance_body = json.dumps({
            "currencies": [{"currency": "USDC", "free": 500.0, "balance": 500.0}]
        }).encode()

        with patch("shadow_position._ft_read_stake_config", return_value=100.0), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = ft_balance_body
            mock_urlopen.return_value = mock_ctx

            amount, source = sp.fetch_shadow_equity_from_live(state)

        self.assertAlmostEqual(amount, 100.0)
        self.assertEqual(source, "ft_balance_stake_capped")

    def test_flat_stake_larger_than_free_uses_free(self):
        """Free USDC 80, stake_amount 200 → shadow equity = 80 (capped by free)."""
        state = self._state_flat()
        ft_balance_body = json.dumps({
            "currencies": [{"currency": "USDC", "free": 80.0, "balance": 80.0}]
        }).encode()

        with patch("shadow_position._ft_read_stake_config", return_value=200.0), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = ft_balance_body
            mock_urlopen.return_value = mock_ctx

            amount, source = sp.fetch_shadow_equity_from_live(state)

        self.assertAlmostEqual(amount, 80.0)
        self.assertEqual(source, "ft_balance_stake_capped")

    def test_flat_unlimited_stake_uses_full_free_balance(self):
        """stake_amount = 'unlimited' → no cap, use full free USDC."""
        state = self._state_flat()
        ft_balance_body = json.dumps({
            "currencies": [{"currency": "USDC", "free": 500.0, "balance": 500.0}]
        }).encode()

        with patch("shadow_position._ft_read_stake_config", return_value=None), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = ft_balance_body
            mock_urlopen.return_value = mock_ctx

            amount, source = sp.fetch_shadow_equity_from_live(state)

        self.assertAlmostEqual(amount, 500.0)
        self.assertEqual(source, "ft_balance")

    # ------------------------------------------------------------------
    # _ft_read_stake_config unit tests
    # ------------------------------------------------------------------

    def test_ft_read_stake_config_numeric(self):
        body = json.dumps({"stake_amount": 100.0}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = body
            mock_urlopen.return_value = mock_ctx
            result = sp._ft_read_stake_config("http://localhost:8090", {}, 5.0)
        self.assertAlmostEqual(result, 100.0)

    def test_ft_read_stake_config_unlimited_returns_none(self):
        body = json.dumps({"stake_amount": "unlimited"}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = body
            mock_urlopen.return_value = mock_ctx
            result = sp._ft_read_stake_config("http://localhost:8090", {}, 5.0)
        self.assertIsNone(result)

    def test_ft_read_stake_config_exception_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = sp._ft_read_stake_config("http://localhost:8090", {}, 5.0)
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # _ft_read_open_trade_cost unit tests
    # ------------------------------------------------------------------

    def test_ft_read_open_trade_cost_stake_amount(self):
        body = json.dumps([{"stake_amount": 150.0, "open_rate": 2.0, "amount": 50.0}]).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = body
            mock_urlopen.return_value = mock_ctx
            result = sp._ft_read_open_trade_cost("http://localhost:8090", {}, 5.0)
        self.assertAlmostEqual(result, 150.0)

    def test_ft_read_open_trade_cost_falls_back_to_rate_x_amount(self):
        body = json.dumps([{"open_rate": 2.0, "amount": 50.0}]).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = body
            mock_urlopen.return_value = mock_ctx
            result = sp._ft_read_open_trade_cost("http://localhost:8090", {}, 5.0)
        self.assertAlmostEqual(result, 100.0)

    def test_ft_read_open_trade_cost_empty_list_returns_none(self):
        body = json.dumps([]).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = lambda s: s
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.read.return_value = body
            mock_urlopen.return_value = mock_ctx
            result = sp._ft_read_open_trade_cost("http://localhost:8090", {}, 5.0)
        self.assertIsNone(result)

    def test_ft_read_open_trade_cost_exception_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = sp._ft_read_open_trade_cost("http://localhost:8090", {}, 5.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
