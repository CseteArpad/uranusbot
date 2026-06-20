# app/test_channel_decision_engine.py
# Unit tests for channel_decision_engine.py — no live bot imports, no real orders.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import channel_decision_engine as cde

FLAT = cde.POSITION_FLAT
IN   = cde.POSITION_IN_POSITION


class TestFlatPanicBuy(unittest.TestCase):

    def test_panic_buy_at_threshold(self):
        # cpagg == 1.01 → PANIC_BUY (boundary inclusive)
        r = cde.evaluate(FLAT, cpagg=1.01, prev_cpagg=0.50)
        self.assertEqual(r.action, cde.ACTION_BUY)
        self.assertEqual(r.rule,   cde.RULE_PANIC_BUY)

    def test_panic_buy_above_threshold(self):
        r = cde.evaluate(FLAT, cpagg=1.20, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_BUY)
        self.assertEqual(r.rule,   cde.RULE_PANIC_BUY)

    def test_panic_buy_does_not_need_prev_cpagg(self):
        # prev_cpagg None must not block panic rule
        r = cde.evaluate(FLAT, cpagg=1.05, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_BUY)
        self.assertEqual(r.rule,   cde.RULE_PANIC_BUY)


class TestFlatStandardBuy(unittest.TestCase):

    def test_standard_buy_rising(self):
        # cpagg in zone and rising
        r = cde.evaluate(FLAT, cpagg=0.15, prev_cpagg=0.10)
        self.assertEqual(r.action, cde.ACTION_BUY)
        self.assertEqual(r.rule,   cde.RULE_STANDARD_BUY)

    def test_standard_buy_at_boundary(self):
        # cpagg == 0.20 (boundary inclusive) and rising
        r = cde.evaluate(FLAT, cpagg=0.20, prev_cpagg=0.18)
        self.assertEqual(r.action, cde.ACTION_BUY)
        self.assertEqual(r.rule,   cde.RULE_STANDARD_BUY)

    def test_standard_buy_not_rising_gives_hold(self):
        # cpagg in zone but NOT rising
        r = cde.evaluate(FLAT, cpagg=0.15, prev_cpagg=0.20)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_PRICE_NOT_RISING)

    def test_standard_buy_equal_prev_gives_hold(self):
        # cpagg == prev_cpagg → not strictly rising
        r = cde.evaluate(FLAT, cpagg=0.15, prev_cpagg=0.15)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_PRICE_NOT_RISING)

    def test_standard_buy_missing_prev_gives_hold(self):
        # Standard buy requires prev_cpagg; None → HOLD / MISSING_PREV_CPAGG
        r = cde.evaluate(FLAT, cpagg=0.10, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_MISSING_PREV_CPAGG)


class TestFlatNeverSells(unittest.TestCase):

    def test_flat_mid_zone_hold(self):
        r = cde.evaluate(FLAT, cpagg=0.50, prev_cpagg=0.40)
        self.assertEqual(r.action, cde.ACTION_HOLD)

    def test_flat_sell_zone_hold(self):
        # Even if cpagg is in sell territory, FLAT never issues SELL
        r = cde.evaluate(FLAT, cpagg=0.85, prev_cpagg=0.90)
        self.assertNotEqual(r.action, cde.ACTION_SELL)

    def test_flat_never_returns_sell(self):
        for cpagg in [0.0, 0.20, 0.50, 0.80, 1.00]:
            r = cde.evaluate(FLAT, cpagg=cpagg, prev_cpagg=cpagg - 0.01)
            self.assertNotEqual(r.action, cde.ACTION_SELL,
                                msg=f"FLAT returned SELL for cpagg={cpagg}")


class TestInPositionPanicSell(unittest.TestCase):

    def test_panic_sell_at_threshold(self):
        # cpagg == -0.01 → PANIC_SELL (boundary inclusive)
        r = cde.evaluate(IN, cpagg=-0.01, prev_cpagg=0.50)
        self.assertEqual(r.action, cde.ACTION_SELL)
        self.assertEqual(r.rule,   cde.RULE_PANIC_SELL)

    def test_panic_sell_below_threshold(self):
        r = cde.evaluate(IN, cpagg=-0.10, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_SELL)
        self.assertEqual(r.rule,   cde.RULE_PANIC_SELL)

    def test_panic_sell_does_not_need_prev_cpagg(self):
        r = cde.evaluate(IN, cpagg=-0.05, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_SELL)
        self.assertEqual(r.rule,   cde.RULE_PANIC_SELL)


class TestInPositionStandardSell(unittest.TestCase):

    def test_standard_sell_falling(self):
        r = cde.evaluate(IN, cpagg=0.85, prev_cpagg=0.90)
        self.assertEqual(r.action, cde.ACTION_SELL)
        self.assertEqual(r.rule,   cde.RULE_STANDARD_SELL)

    def test_standard_sell_at_boundary(self):
        # cpagg == 0.80 (boundary inclusive) and falling
        r = cde.evaluate(IN, cpagg=0.80, prev_cpagg=0.85)
        self.assertEqual(r.action, cde.ACTION_SELL)
        self.assertEqual(r.rule,   cde.RULE_STANDARD_SELL)

    def test_standard_sell_not_falling_gives_hold(self):
        r = cde.evaluate(IN, cpagg=0.85, prev_cpagg=0.80)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_PRICE_NOT_FALLING)

    def test_standard_sell_equal_prev_gives_hold(self):
        r = cde.evaluate(IN, cpagg=0.85, prev_cpagg=0.85)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_PRICE_NOT_FALLING)

    def test_standard_sell_missing_prev_gives_hold(self):
        r = cde.evaluate(IN, cpagg=0.90, prev_cpagg=None)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_MISSING_PREV_CPAGG)


class TestInPositionNeverBuys(unittest.TestCase):

    def test_in_position_mid_zone_hold(self):
        r = cde.evaluate(IN, cpagg=0.50, prev_cpagg=0.40)
        self.assertEqual(r.action, cde.ACTION_HOLD)

    def test_in_position_buy_zone_hold(self):
        # Even if cpagg is in buy zone, IN_POSITION never issues BUY
        r = cde.evaluate(IN, cpagg=0.10, prev_cpagg=0.05)
        self.assertNotEqual(r.action, cde.ACTION_BUY)

    def test_in_position_never_returns_buy(self):
        for cpagg in [0.0, 0.10, 0.20, 0.50, 0.80]:
            r = cde.evaluate(IN, cpagg=cpagg, prev_cpagg=cpagg + 0.01)
            self.assertNotEqual(r.action, cde.ACTION_BUY,
                                msg=f"IN_POSITION returned BUY for cpagg={cpagg}")


class TestMissingCpagg(unittest.TestCase):

    def test_flat_missing_cpagg(self):
        r = cde.evaluate(FLAT, cpagg=None, prev_cpagg=0.50)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_MISSING_CPAGG)

    def test_in_position_missing_cpagg(self):
        r = cde.evaluate(IN, cpagg=None, prev_cpagg=0.50)
        self.assertEqual(r.action, cde.ACTION_HOLD)
        self.assertEqual(r.reason, cde.REASON_MISSING_CPAGG)


class TestPanicPriority(unittest.TestCase):

    def test_panic_buy_overrides_standard_buy_zone(self):
        # cpagg >= panic threshold AND <= buy_zone_threshold simultaneously
        # (only possible with custom thresholds — verify panic wins)
        r = cde.evaluate(
            FLAT, cpagg=0.15, prev_cpagg=0.10,
            panic_buy_threshold=0.15,    # lower threshold for test
            buy_zone_threshold=0.20,
        )
        self.assertEqual(r.rule, cde.RULE_PANIC_BUY)

    def test_panic_sell_overrides_standard_sell_zone(self):
        r = cde.evaluate(
            IN, cpagg=0.85, prev_cpagg=0.90,
            panic_sell_threshold=0.85,   # raise threshold for test
            sell_zone_threshold=0.80,
        )
        self.assertEqual(r.rule, cde.RULE_PANIC_SELL)


class TestResultFields(unittest.TestCase):

    def test_result_carries_trend_state(self):
        r = cde.evaluate(FLAT, cpagg=0.50, prev_cpagg=0.40, trend_state="LONG")
        self.assertEqual(r.trend_state, "LONG")

    def test_result_carries_position_state(self):
        r = cde.evaluate(IN, cpagg=0.50, prev_cpagg=0.60)
        self.assertEqual(r.position_state, IN)

    def test_result_carries_cpagg_and_prev(self):
        r = cde.evaluate(FLAT, cpagg=0.15, prev_cpagg=0.12)
        self.assertAlmostEqual(r.cpagg,     0.15)
        self.assertAlmostEqual(r.prev_cpagg, 0.12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
