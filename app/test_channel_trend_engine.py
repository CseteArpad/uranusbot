# app/test_channel_trend_engine.py
# Unit tests for channel_trend_engine.py — no live bot imports, no real orders.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import channel_trend_engine as cte


# ---------------------------------------------------------------------------
# compute_trend_score
# ---------------------------------------------------------------------------

class TestComputeTrendScore(unittest.TestCase):

    def test_formula(self):
        # 0.4 * 0.6 + 0.6 * 0.7 = 0.24 + 0.42 = 0.66
        score = cte.compute_trend_score(cp_4h=0.6, cp_12h=0.7)
        self.assertAlmostEqual(score, 0.66, places=9)

    def test_formula_at_midpoint(self):
        # 0.4 * 0.5 + 0.6 * 0.5 = 0.5
        self.assertAlmostEqual(cte.compute_trend_score(0.5, 0.5), 0.5, places=9)

    def test_formula_zero_inputs(self):
        self.assertAlmostEqual(cte.compute_trend_score(0.0, 0.0), 0.0, places=9)

    def test_formula_negative_cp(self):
        # Negative CP (price below lower band) is valid
        score = cte.compute_trend_score(cp_4h=-0.2, cp_12h=-0.1)
        expected = 0.4 * (-0.2) + 0.6 * (-0.1)
        self.assertAlmostEqual(score, expected, places=9)

    def test_returns_none_when_cp_4h_missing(self):
        self.assertIsNone(cte.compute_trend_score(cp_4h=None, cp_12h=0.7))

    def test_returns_none_when_cp_12h_missing(self):
        self.assertIsNone(cte.compute_trend_score(cp_4h=0.6, cp_12h=None))

    def test_returns_none_when_both_missing(self):
        self.assertIsNone(cte.compute_trend_score(cp_4h=None, cp_12h=None))

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(cte.TREND_SCORE_W4H + cte.TREND_SCORE_W12H, 1.0, places=9)


# ---------------------------------------------------------------------------
# compute_trend_state
# ---------------------------------------------------------------------------

class TestComputeTrendState(unittest.TestCase):

    def test_long(self):
        # Both CPs clearly above 0.50
        self.assertEqual(cte.compute_trend_state(cp_4h=0.7, cp_12h=0.8), "LONG")

    def test_long_just_above_threshold(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=0.51, cp_12h=0.51), "LONG")

    def test_short(self):
        # Both CPs clearly below 0.50
        self.assertEqual(cte.compute_trend_state(cp_4h=0.3, cp_12h=0.4), "SHORT")

    def test_short_just_below_threshold(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=0.49, cp_12h=0.49), "SHORT")

    def test_sideways_diverging_high_low(self):
        # 4H above, 12H below → neither LONG nor SHORT
        self.assertEqual(cte.compute_trend_state(cp_4h=0.7, cp_12h=0.3), "SIDEWAYS")

    def test_sideways_diverging_low_high(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=0.3, cp_12h=0.7), "SIDEWAYS")

    def test_sideways_at_exact_threshold(self):
        # 0.50 is NOT > 0.50, so not LONG; NOT < 0.50, so not SHORT → SIDEWAYS
        self.assertEqual(cte.compute_trend_state(cp_4h=0.50, cp_12h=0.50), "SIDEWAYS")

    def test_unknown_when_cp_4h_missing(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=None, cp_12h=0.7), "UNKNOWN")

    def test_unknown_when_cp_12h_missing(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=0.7, cp_12h=None), "UNKNOWN")

    def test_unknown_when_both_missing(self):
        self.assertEqual(cte.compute_trend_state(cp_4h=None, cp_12h=None), "UNKNOWN")


# ---------------------------------------------------------------------------
# compute_candidate_action
# ---------------------------------------------------------------------------

class TestComputeCandidateAction(unittest.TestCase):

    def test_buy_zone_low_value(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=0.10), "BUY_ZONE")

    def test_buy_zone_at_exact_boundary(self):
        # 0.20 <= 0.20 → BUY_ZONE (inclusive)
        self.assertEqual(cte.compute_candidate_action(cpagg=0.20), "BUY_ZONE")

    def test_hold_zone_just_above_buy_boundary(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=0.21), "HOLD_ZONE")

    def test_hold_zone_mid(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=0.50), "HOLD_ZONE")

    def test_hold_zone_just_below_sell_boundary(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=0.79), "HOLD_ZONE")

    def test_sell_zone_at_exact_boundary(self):
        # 0.80 >= 0.80 → SELL_ZONE (inclusive)
        self.assertEqual(cte.compute_candidate_action(cpagg=0.80), "SELL_ZONE")

    def test_sell_zone_high_value(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=0.95), "SELL_ZONE")

    def test_unknown_when_cpagg_missing(self):
        self.assertEqual(cte.compute_candidate_action(cpagg=None), "UNKNOWN")

    def test_buy_zone_negative_cpagg(self):
        # CPagg can be negative (price below all lower bands)
        self.assertEqual(cte.compute_candidate_action(cpagg=-0.5), "BUY_ZONE")

    def test_sell_zone_above_one(self):
        # CPagg can exceed 1.0 (price above all upper bands)
        self.assertEqual(cte.compute_candidate_action(cpagg=1.2), "SELL_ZONE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
