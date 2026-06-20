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

    def test_threshold_constants_exist(self):
        self.assertAlmostEqual(cte.LONG_THRESHOLD,  0.65, places=9)
        self.assertAlmostEqual(cte.SHORT_THRESHOLD, 0.35, places=9)


# ---------------------------------------------------------------------------
# compute_trend_state
# ---------------------------------------------------------------------------

class TestComputeTrendState(unittest.TestCase):
    # TrendState is now derived from TrendScore = 0.4*CP_4H + 0.6*CP_12H.
    # LONG >= 0.65 | SHORT <= 0.35 | SIDEWAYS otherwise | UNKNOWN if score is None

    def test_long_score_above_threshold(self):
        # score = 0.4*0.8 + 0.6*0.9 = 0.32 + 0.54 = 0.86 → LONG
        self.assertEqual(cte.compute_trend_state(cp_4h=0.8, cp_12h=0.9), "LONG")

    def test_long_at_exact_threshold(self):
        # Need score == 0.65 exactly: 0.4*a + 0.6*b = 0.65
        # Use a=0.65, b=0.65 → score=0.65 → LONG (>= threshold)
        self.assertEqual(cte.compute_trend_state(cp_4h=0.65, cp_12h=0.65), "LONG")

    def test_short_score_below_threshold(self):
        # score = 0.4*0.2 + 0.6*0.1 = 0.08 + 0.06 = 0.14 → SHORT
        self.assertEqual(cte.compute_trend_state(cp_4h=0.2, cp_12h=0.1), "SHORT")

    def test_short_at_exact_threshold(self):
        # a=0.35, b=0.35 → score=0.35 → SHORT (<= threshold)
        self.assertEqual(cte.compute_trend_state(cp_4h=0.35, cp_12h=0.35), "SHORT")

    def test_sideways_mid_score(self):
        # a=0.50, b=0.50 → score=0.50 → SIDEWAYS (0.35 < 0.50 < 0.65)
        self.assertEqual(cte.compute_trend_state(cp_4h=0.50, cp_12h=0.50), "SIDEWAYS")

    def test_sideways_just_above_short_threshold(self):
        # score just above 0.35 → SIDEWAYS
        # a=0.36, b=0.36 → score=0.36
        self.assertEqual(cte.compute_trend_state(cp_4h=0.36, cp_12h=0.36), "SIDEWAYS")

    def test_sideways_just_below_long_threshold(self):
        # a=0.64, b=0.64 → score=0.64 → SIDEWAYS
        self.assertEqual(cte.compute_trend_state(cp_4h=0.64, cp_12h=0.64), "SIDEWAYS")

    def test_sideways_diverging_cps(self):
        # 4H high, 12H low → score = 0.4*0.9 + 0.6*0.1 = 0.36 + 0.06 = 0.42 → SIDEWAYS
        self.assertEqual(cte.compute_trend_state(cp_4h=0.9, cp_12h=0.1), "SIDEWAYS")

    def test_both_cp_above_half_but_score_below_long(self):
        # Both > 0.50 does NOT guarantee LONG under new rule
        # score = 0.4*0.55 + 0.6*0.55 = 0.55 → SIDEWAYS (< 0.65)
        self.assertEqual(cte.compute_trend_state(cp_4h=0.55, cp_12h=0.55), "SIDEWAYS")

    def test_both_cp_below_half_but_score_above_short(self):
        # Both < 0.50 does NOT guarantee SHORT under new rule
        # score = 0.4*0.45 + 0.6*0.45 = 0.45 → SIDEWAYS (> 0.35)
        self.assertEqual(cte.compute_trend_state(cp_4h=0.45, cp_12h=0.45), "SIDEWAYS")

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
