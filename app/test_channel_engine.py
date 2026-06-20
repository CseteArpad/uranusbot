# app/test_channel_engine.py
# Unit tests for channel_engine.py — no live bot imports, no real orders.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import channel_engine as ce


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_lows(n: int, val: float = 1.0):
    return [val] * n


def _flat_highs(n: int, val: float = 1.5):
    return [val] * n


def _make_candles(n=40, base_low=0.80, base_high=1.50,
                  pl_idxs=None, pl_vals=None,
                  ph_idxs=None, ph_vals=None):
    """Return (highs, lows) with planted pivot values."""
    lows  = [base_low]  * n
    highs = [base_high] * n
    if pl_idxs and pl_vals:
        for i, v in zip(pl_idxs, pl_vals):
            lows[i] = v
    if ph_idxs and ph_vals:
        for i, v in zip(ph_idxs, ph_vals):
            highs[i] = v
    return highs, lows


# ---------------------------------------------------------------------------
# Pivot low detection
# ---------------------------------------------------------------------------

class TestDetectPivotLows(unittest.TestCase):

    def test_single_clear_pivot_low(self):
        lows = _flat_lows(20)
        lows[10] = 0.5
        self.assertEqual(ce.detect_pivot_lows(lows, left=3, right=3), [10])

    def test_no_pivot_in_flat_sequence(self):
        lows = _flat_lows(20)
        self.assertEqual(ce.detect_pivot_lows(lows, left=3, right=3), [])

    def test_multiple_pivot_lows(self):
        lows = _flat_lows(30)
        lows[5]  = 0.6
        lows[20] = 0.7
        result = ce.detect_pivot_lows(lows, left=3, right=3)
        self.assertIn(5,  result)
        self.assertIn(20, result)

    def test_pivot_too_close_to_edge_excluded(self):
        # index 2 cannot satisfy left=3 (would need indices -1, 0, 1)
        lows = _flat_lows(20)
        lows[2] = 0.1
        self.assertNotIn(2, ce.detect_pivot_lows(lows, left=3, right=3))

    def test_tie_on_right_is_not_a_pivot(self):
        # lows[9] == lows[8] — strict inequality fails on the right side
        lows = _flat_lows(20)
        lows[8] = 0.5
        lows[9] = 0.5
        result = ce.detect_pivot_lows(lows, left=3, right=3)
        self.assertNotIn(8, result)
        self.assertNotIn(9, result)


# ---------------------------------------------------------------------------
# Pivot high detection
# ---------------------------------------------------------------------------

class TestDetectPivotHighs(unittest.TestCase):

    def test_single_clear_pivot_high(self):
        highs = _flat_highs(20)
        highs[10] = 2.5
        self.assertEqual(ce.detect_pivot_highs(highs, left=3, right=3), [10])

    def test_no_pivot_in_flat_sequence(self):
        highs = _flat_highs(20)
        self.assertEqual(ce.detect_pivot_highs(highs, left=3, right=3), [])

    def test_multiple_pivot_highs(self):
        highs = _flat_highs(30)
        highs[7]  = 2.0
        highs[22] = 1.8
        result = ce.detect_pivot_highs(highs, left=3, right=3)
        self.assertIn(7,  result)
        self.assertIn(22, result)

    def test_pivot_too_close_to_edge_excluded(self):
        highs = _flat_highs(20)
        highs[2] = 9.9
        self.assertNotIn(2, ce.detect_pivot_highs(highs, left=3, right=3))


# ---------------------------------------------------------------------------
# Parallel channel building
# ---------------------------------------------------------------------------

class TestBuildParallelChannel(unittest.TestCase):

    def test_valid_horizontal_channel(self):
        # Two pivot lows at the same price => horizontal lower line
        highs, lows = _make_candles(
            n=40,
            pl_idxs=[8, 22], pl_vals=[0.70, 0.70],
            ph_idxs=[15],    ph_vals=[1.90],   # above base_high=1.50
        )
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertTrue(ch["valid"], msg=ch.get("reason"))
        self.assertAlmostEqual(ch["lower_slope"], 0.0, places=8)
        self.assertGreater(ch["width_at_end"], 0.0)

    def test_valid_rising_channel(self):
        # Both pivot lows must be below base_low=0.80; second higher than first => rising slope
        highs, lows = _make_candles(
            n=40,
            pl_idxs=[8, 24], pl_vals=[0.60, 0.72],  # rising lows, both below 0.80
            ph_idxs=[16],    ph_vals=[1.90],
        )
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertTrue(ch["valid"], msg=ch.get("reason"))
        self.assertGreater(ch["lower_slope"], 0.0)

    def test_invalid_insufficient_pivot_lows(self):
        # Flat lows — no pivots detected
        highs = _flat_highs(30)
        lows  = _flat_lows(30)
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertFalse(ch["valid"])
        self.assertIn("pivot_low", ch["reason"])

    def test_invalid_no_pivot_highs(self):
        # Two pivot lows present but highs are flat (no pivot highs)
        lows = _flat_lows(30)
        lows[8]  = 0.70
        lows[20] = 0.70
        highs = _flat_highs(30)   # flat => no pivot highs
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertFalse(ch["valid"])

    def test_invalid_insufficient_bars(self):
        highs = _flat_highs(4)
        lows  = _flat_lows(4)
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertFalse(ch["valid"])
        self.assertEqual(ch["reason"], ce.REASON_INSUFFICIENT_PIVOT_LOWS)

    def test_invalid_returns_pivot_counts(self):
        # Even on failure, pivot_low_count and pivot_high_count are present
        highs = _flat_highs(30)
        lows  = _flat_lows(30)
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertFalse(ch["valid"])
        self.assertIn("pivot_low_count",  ch)
        self.assertIn("pivot_high_count", ch)

    def test_valid_returns_pivot_counts(self):
        highs, lows = _make_candles(
            n=40,
            pl_idxs=[8, 22], pl_vals=[0.70, 0.70],
            ph_idxs=[15],    ph_vals=[1.90],
        )
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertTrue(ch["valid"])
        self.assertGreaterEqual(ch["pivot_low_count"],  2)
        self.assertGreaterEqual(ch["pivot_high_count"], 1)

    def test_upper_above_lower_at_all_indices(self):
        highs, lows = _make_candles(
            n=40,
            pl_idxs=[8, 22], pl_vals=[0.70, 0.70],
            ph_idxs=[15],    ph_vals=[1.90],
        )
        ch = ce.build_parallel_channel(highs, lows, left=3, right=3)
        self.assertTrue(ch["valid"])
        for i in range(40):
            lo, up = ce.evaluate_channel_at(ch, i)
            self.assertGreater(up, lo, msg=f"upper <= lower at index {i}")


# ---------------------------------------------------------------------------
# channel_position: CP calculation
# ---------------------------------------------------------------------------

class TestChannelPosition(unittest.TestCase):

    def test_cp_zero_at_lower_band(self):
        cp = ce.channel_position(price=1.0, lower=1.0, upper=1.5)
        self.assertAlmostEqual(cp, 0.0)

    def test_cp_one_at_upper_band(self):
        cp = ce.channel_position(price=1.5, lower=1.0, upper=1.5)
        self.assertAlmostEqual(cp, 1.0)

    def test_cp_half_at_midline(self):
        cp = ce.channel_position(price=1.25, lower=1.0, upper=1.5)
        self.assertAlmostEqual(cp, 0.5)

    def test_cp_negative_below_lower_band(self):
        # (0.5 - 1.0) / (1.5 - 1.0) = -1.0
        cp = ce.channel_position(price=0.5, lower=1.0, upper=1.5)
        self.assertAlmostEqual(cp, -1.0)

    def test_cp_above_one_above_upper_band(self):
        # (2.0 - 1.0) / (1.5 - 1.0) = 2.0
        cp = ce.channel_position(price=2.0, lower=1.0, upper=1.5)
        self.assertAlmostEqual(cp, 2.0)

    def test_cp_raises_on_zero_width(self):
        with self.assertRaises(ValueError):
            ce.channel_position(price=1.2, lower=1.5, upper=1.5)

    def test_cp_raises_on_inverted_channel(self):
        with self.assertRaises(ValueError):
            ce.channel_position(price=1.2, lower=1.5, upper=1.0)


# ---------------------------------------------------------------------------
# evaluate_channel_at
# ---------------------------------------------------------------------------

class TestEvaluateChannelAt(unittest.TestCase):

    def _build_test_channel(self):
        highs, lows = _make_candles(
            n=40,
            pl_idxs=[8, 22], pl_vals=[0.70, 0.70],
            ph_idxs=[15],    ph_vals=[1.90],
        )
        return ce.build_parallel_channel(highs, lows, left=3, right=3)

    def test_returns_two_floats(self):
        ch = self._build_test_channel()
        lo, up = ce.evaluate_channel_at(ch, 0)
        self.assertIsInstance(lo, float)
        self.assertIsInstance(up, float)

    def test_upper_always_above_lower_across_range(self):
        ch = self._build_test_channel()
        for i in range(-5, 45):
            lo, up = ce.evaluate_channel_at(ch, i)
            self.assertGreater(up, lo, msg=f"upper <= lower at index {i}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
