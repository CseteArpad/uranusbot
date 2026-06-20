# app/test_trendline_channel_engine.py
# Unit tests for trendline_channel_engine.py — no live bot imports, no real orders.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import trendline_channel_engine as tce


# ---------------------------------------------------------------------------
# Shared test-data builder
# ---------------------------------------------------------------------------

def _make_bars(n=25, low_base=0.9, high_base=1.5,
               pivot_lows=None, pivot_highs=None):
    """
    Build (highs, lows) arrays of length *n*.
    pivot_lows / pivot_highs are dicts {index: value} that override the base.
    """
    lows  = [low_base]  * n
    highs = [high_base] * n
    if pivot_lows:
        for idx, val in pivot_lows.items():
            lows[idx] = val
    if pivot_highs:
        for idx, val in pivot_highs.items():
            highs[idx] = val
    return highs, lows


# ---------------------------------------------------------------------------
# TestBuildTrendlineChannel — validity and geometry
# ---------------------------------------------------------------------------

class TestBuildTrendlineChannel(unittest.TestCase):

    def test_rising_lower_and_upper(self):
        """Both trendlines slope upward when later pivots are higher."""
        highs, lows = _make_bars(
            n=25,
            pivot_lows  = {5: 0.50, 10: 0.60},   # rising lows  (0.02/bar)
            pivot_highs = {5: 2.00, 10: 2.10},    # rising highs (0.02/bar)
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertTrue(ch["valid"])
        self.assertGreater(ch["lower_slope"], 0)
        self.assertGreater(ch["upper_slope"], 0)

    def test_falling_lower_and_upper(self):
        """Both trendlines slope downward when later pivots are lower."""
        highs, lows = _make_bars(
            n=25,
            pivot_lows  = {5: 0.60, 10: 0.50},   # falling lows
            pivot_highs = {5: 2.10, 10: 2.00},    # falling highs
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertTrue(ch["valid"])
        self.assertLess(ch["lower_slope"], 0)
        self.assertLess(ch["upper_slope"], 0)

    def test_non_parallel_lines_accepted(self):
        """Upper and lower lines are allowed to have different slopes."""
        highs, lows = _make_bars(
            n=25,
            pivot_lows  = {5: 0.50, 10: 0.60},   # lower rising  (+0.02)
            pivot_highs = {5: 2.10, 10: 2.00},    # upper falling (-0.02)
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertTrue(ch["valid"])
        # Lines must have opposite-sign slopes → definitively non-parallel
        self.assertGreater(ch["lower_slope"], 0)
        self.assertLess(ch["upper_slope"], 0)
        # But at n-1=24 the channel must still be valid (upper > lower)
        self.assertGreater(ch["upper_now"], ch["lower_now"])

    def test_multiple_pivot_points_used(self):
        """With > 2 pivots the OLS fit is used; channel must still be valid."""
        highs, lows = _make_bars(
            n=30,
            pivot_lows  = {4: 0.50, 9: 0.55, 14: 0.60},
            pivot_highs = {4: 2.00, 9: 2.05, 14: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertTrue(ch["valid"])
        self.assertEqual(ch["pivot_low_count"],  3)
        self.assertEqual(ch["pivot_high_count"], 3)
        self.assertEqual(len(ch["used_low_indices"]),  3)
        self.assertEqual(len(ch["used_high_indices"]), 3)

    def test_max_pivots_limits_used_points(self):
        """max_pivots=2 keeps only the two most-recent pivots."""
        highs, lows = _make_bars(
            n=30,
            pivot_lows  = {4: 0.50, 9: 0.55, 14: 0.60},
            pivot_highs = {4: 2.00, 9: 2.05, 14: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows, max_pivots=2)
        self.assertTrue(ch["valid"])
        self.assertEqual(len(ch["used_low_indices"]),  2)
        self.assertEqual(len(ch["used_high_indices"]), 2)
        # Most-recent two lows are at 9 and 14
        self.assertEqual(ch["used_low_indices"],  [9, 14])
        self.assertEqual(ch["used_high_indices"], [9, 14])

    def test_result_fields_present(self):
        """All documented fields must exist in a valid result."""
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        required = [
            "valid", "reason", "lower_slope", "lower_intercept",
            "upper_slope", "upper_intercept", "lower_now", "upper_now",
            "width_now", "cp", "pivot_low_count", "pivot_high_count",
            "used_low_indices", "used_high_indices", "n_bars",
        ]
        for field in required:
            self.assertIn(field, ch, msg=f"missing field: {field}")

    def test_lower_slope_intercept_exact_two_points(self):
        """With exactly 2 pivot lows OLS gives the exact line through both."""
        # Pivots at (5, 0.50) and (10, 0.60)
        # slope = (0.60-0.50)/(10-5) = 0.02
        # intercept = 0.50 - 0.02*5 = 0.40
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertAlmostEqual(ch["lower_slope"],     0.02, places=9)
        self.assertAlmostEqual(ch["lower_intercept"], 0.40, places=9)

    def test_lower_now_upper_now_at_last_bar(self):
        """lower_now and upper_now are evaluated at index n-1."""
        n = 25
        highs, lows = _make_bars(
            n=n,
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        cur = float(n - 1)
        expected_lower = ch["lower_slope"] * cur + ch["lower_intercept"]
        expected_upper = ch["upper_slope"] * cur + ch["upper_intercept"]
        self.assertAlmostEqual(ch["lower_now"], expected_lower, places=9)
        self.assertAlmostEqual(ch["upper_now"], expected_upper, places=9)

    def test_width_now_equals_upper_minus_lower(self):
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertAlmostEqual(
            ch["width_now"], ch["upper_now"] - ch["lower_now"], places=9
        )


# ---------------------------------------------------------------------------
# TestInvalidChannels
# ---------------------------------------------------------------------------

class TestInvalidChannels(unittest.TestCase):

    def test_insufficient_pivot_lows(self):
        """Only one pivot low → insufficient_pivot_lows."""
        n = 25
        lows  = [0.9] * n
        highs = [1.5] * n
        lows[10]  = 0.5   # single pivot low (only one dip)
        highs[5]  = 2.0   # two pivot highs
        highs[15] = 2.1
        ch = tce.build_trendline_channel(highs, lows)
        self.assertFalse(ch["valid"])
        self.assertEqual(ch["reason"], tce.REASON_INSUFFICIENT_PIVOT_LOWS)

    def test_insufficient_pivot_highs(self):
        """Only one pivot high → insufficient_pivot_highs."""
        n = 25
        lows  = [0.9] * n
        highs = [1.5] * n
        lows[5]   = 0.5   # two pivot lows
        lows[12]  = 0.6
        highs[10] = 2.0   # single pivot high
        ch = tce.build_trendline_channel(highs, lows)
        self.assertFalse(ch["valid"])
        self.assertEqual(ch["reason"], tce.REASON_INSUFFICIENT_PIVOT_HIGHS)

    def test_insufficient_bars(self):
        """Fewer bars than left+right+1 → no pivots detected → invalid."""
        highs = [1.0] * 5
        lows  = [0.5] * 5
        ch = tce.build_trendline_channel(highs, lows)
        self.assertFalse(ch["valid"])

    def test_upper_below_or_equal_lower_at_current_index(self):
        """
        Crossing trendlines: lower rises sharply, upper falls sharply.
        At n-1 the extrapolated upper is below the lower.
        n=25, cur_idx=24.
        lower pivots: (5, 0.5), (10, 2.0) → slope=(2.0-0.5)/5=0.30, int=0.5-0.30*5=-1.0
          lower_now = 0.30*24 - 1.0 = 7.2 - 1.0 = 6.2
        upper pivots: (5, 2.0), (10, 0.5) → slope=(0.5-2.0)/5=-0.30, int=2.0+0.30*5=3.5
          upper_now = -0.30*24 + 3.5 = -7.2 + 3.5 = -3.7
        upper_now (-3.7) < lower_now (6.2) → UPPER_BELOW_OR_EQUAL_LOWER
        """
        n = 25
        lows  = [3.0] * n   # background > pivot values
        highs = [0.3] * n   # background < pivot values
        lows[5]   = 0.5
        lows[10]  = 2.0
        highs[5]  = 2.0
        highs[10] = 0.5
        ch = tce.build_trendline_channel(highs, lows)
        self.assertFalse(ch["valid"])
        self.assertEqual(ch["reason"], tce.REASON_UPPER_BELOW_OR_EQUAL_LOWER)


# ---------------------------------------------------------------------------
# TestChannelPosition
# ---------------------------------------------------------------------------

class TestChannelPosition(unittest.TestCase):

    def _ch_at_price(self, price):
        """Build a known channel and compute CP for a given price."""
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        self.assertTrue(ch["valid"])
        return tce.channel_position(price, ch["lower_now"], ch["upper_now"])

    def test_cp_at_lower_is_zero(self):
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        cp = tce.channel_position(ch["lower_now"], ch["lower_now"], ch["upper_now"])
        self.assertAlmostEqual(cp, 0.0, places=9)

    def test_cp_at_upper_is_one(self):
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        cp = tce.channel_position(ch["upper_now"], ch["lower_now"], ch["upper_now"])
        self.assertAlmostEqual(cp, 1.0, places=9)

    def test_cp_below_lower_is_negative(self):
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        cp = tce.channel_position(ch["lower_now"] - 0.10, ch["lower_now"], ch["upper_now"])
        self.assertLess(cp, 0.0)

    def test_cp_above_upper_exceeds_one(self):
        highs, lows = _make_bars(
            pivot_lows  = {5: 0.50, 10: 0.60},
            pivot_highs = {5: 2.00, 10: 2.10},
        )
        ch = tce.build_trendline_channel(highs, lows)
        cp = tce.channel_position(ch["upper_now"] + 0.10, ch["lower_now"], ch["upper_now"])
        self.assertGreater(cp, 1.0)

    def test_cp_midpoint_is_half(self):
        lo, up = 1.0, 3.0
        cp = tce.channel_position(2.0, lo, up)
        self.assertAlmostEqual(cp, 0.5, places=9)

    def test_cp_raises_on_degenerate(self):
        with self.assertRaises(ValueError):
            tce.channel_position(1.0, 2.0, 2.0)

    def test_cp_raises_when_upper_below_lower(self):
        with self.assertRaises(ValueError):
            tce.channel_position(1.5, 2.0, 1.0)


# ---------------------------------------------------------------------------
# TestPivotStrengthVariants
# ---------------------------------------------------------------------------

class TestPivotStrengthVariants(unittest.TestCase):

    def test_left2_finds_more_pivots_than_left3(self):
        """Tighter window (left=2) detects pivots that left=3 misses."""
        n = 25
        # Place a dip at index 8 with a tie at distance 3 (blocks left=3)
        lows = [1.0] * n
        lows[5]  = 0.7   # tie at distance 3 from index 8
        lows[8]  = 0.7   # same value → NOT strict with left=3; IS strict with left=2
        lows[15] = 0.7
        lows[18] = 0.7

        highs = [1.5] * n
        highs[12] = 2.0
        highs[20] = 2.1

        ch3 = tce.build_trendline_channel(highs, lows, left=3, right=3)
        ch2 = tce.build_trendline_channel(highs, lows, left=2, right=2)

        self.assertFalse(ch3["valid"])  # left=3 can't find 2 strict pivot lows
        self.assertTrue(ch2["valid"])   # left=2 finds them

    def test_custom_left_right(self):
        """Engine respects arbitrary left/right parameters."""
        n = 20
        lows  = [1.0] * n
        highs = [2.0] * n
        lows[3]  = 0.5   # pivot low with left=2 (distance 2 from start)
        lows[8]  = 0.6
        highs[3] = 2.5
        highs[8] = 2.6
        ch = tce.build_trendline_channel(highs, lows, left=2, right=2)
        self.assertTrue(ch["valid"])


# ---------------------------------------------------------------------------
# TestAllInvalidReasons
# ---------------------------------------------------------------------------

class TestAllInvalidReasons(unittest.TestCase):

    def test_all_reason_constants_listed(self):
        expected = {
            tce.REASON_INSUFFICIENT_PIVOT_LOWS,
            tce.REASON_INSUFFICIENT_PIVOT_HIGHS,
            tce.REASON_UPPER_BELOW_OR_EQUAL_LOWER,
            tce.REASON_DEGENERATE_WIDTH,
            tce.REASON_OTHER_EXCEPTION,
        }
        self.assertEqual(set(tce.ALL_INVALID_REASONS), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
