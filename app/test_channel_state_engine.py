# app/test_channel_state_engine.py
# Unit tests for channel_state_engine.py — no live bot imports, no real orders.

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import channel_state_engine as cse


TS = "2024-01-15T12:00:00Z"
TS2 = "2024-01-15T12:01:00Z"


class TestInitialState(unittest.TestCase):

    def test_all_cp_initially_none(self):
        st = cse.ChannelState()
        eff = cse.effective_cp_by_tf(st)
        for tf, cp in eff.items():
            self.assertIsNone(cp, msg=f"{tf} should start as None")

    def test_all_stale_ticks_initially_zero(self):
        st = cse.ChannelState()
        stale = cse.staleness_by_tf(st)
        for tf, v in stale.items():
            self.assertEqual(v, 0, msg=f"{tf} stale_ticks should start at 0")


class TestUpdateValidCp(unittest.TestCase):

    def test_valid_cp_stored(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS, {"4H": 0.65, "12H": 0.72})
        self.assertAlmostEqual(st.last_valid_cp_4h,  0.65)
        self.assertAlmostEqual(st.last_valid_cp_12h, 0.72)

    def test_valid_cp_stores_timestamp(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS, {"4H": 0.5})
        self.assertEqual(st.last_valid_ts_4h, TS)

    def test_valid_cp_resets_stale_ticks(self):
        st = cse.ChannelState()
        # Simulate two None ticks first
        cse.update_channel_state(st, TS, {"12H": None})
        cse.update_channel_state(st, TS, {"12H": None})
        self.assertEqual(st.stale_ticks_12h, 2)
        # Now a valid CP arrives
        cse.update_channel_state(st, TS2, {"12H": 0.55})
        self.assertEqual(st.stale_ticks_12h, 0)

    def test_valid_cp_overwrites_previous(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS,  {"1H": 0.30})
        cse.update_channel_state(st, TS2, {"1H": 0.75})
        self.assertAlmostEqual(st.last_valid_cp_1h, 0.75)


class TestUpdateNoneCp(unittest.TestCase):

    def test_none_cp_does_not_clear_last_valid(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS,  {"12H": 0.60})
        cse.update_channel_state(st, TS2, {"12H": None})
        # last_valid_cp_12h must still hold 0.60
        self.assertAlmostEqual(st.last_valid_cp_12h, 0.60)

    def test_none_cp_increments_stale_ticks(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS, {"1D": None})
        self.assertEqual(st.stale_ticks_1d, 1)

    def test_stale_ticks_accumulate(self):
        st = cse.ChannelState()
        for _ in range(5):
            cse.update_channel_state(st, TS, {"1D": None})
        self.assertEqual(st.stale_ticks_1d, 5)

    def test_none_does_not_touch_other_tfs(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS,  {"4H": 0.55, "12H": 0.60})
        cse.update_channel_state(st, TS2, {"4H": None})
        # 12H must be untouched
        self.assertAlmostEqual(st.last_valid_cp_12h, 0.60)
        self.assertEqual(st.stale_ticks_12h, 0)


class TestEffectiveCpByTf(unittest.TestCase):

    def test_returns_last_valid_after_none(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS,  {"4H": 0.70})
        cse.update_channel_state(st, TS2, {"4H": None})
        eff = cse.effective_cp_by_tf(st)
        self.assertAlmostEqual(eff["4H"], 0.70)

    def test_returns_none_before_any_valid(self):
        st = cse.ChannelState()
        eff = cse.effective_cp_by_tf(st)
        self.assertIsNone(eff["12H"])

    def test_all_four_tfs_returned(self):
        st = cse.ChannelState()
        eff = cse.effective_cp_by_tf(st)
        self.assertSetEqual(set(eff.keys()), {"1H", "4H", "12H", "1D"})

    def test_multiple_tfs_independent(self):
        st = cse.ChannelState()
        cse.update_channel_state(st, TS, {"1H": 0.10, "4H": 0.40, "12H": 0.60, "1D": 0.80})
        cse.update_channel_state(st, TS2, {"1H": None, "4H": None, "12H": None, "1D": None})
        eff = cse.effective_cp_by_tf(st)
        self.assertAlmostEqual(eff["1H"],  0.10)
        self.assertAlmostEqual(eff["4H"],  0.40)
        self.assertAlmostEqual(eff["12H"], 0.60)
        self.assertAlmostEqual(eff["1D"],  0.80)


class TestStalenessbyTf(unittest.TestCase):

    def test_all_four_tfs_returned(self):
        st = cse.ChannelState()
        stale = cse.staleness_by_tf(st)
        self.assertSetEqual(set(stale.keys()), {"1H", "4H", "12H", "1D"})

    def test_stale_counts_per_tf(self):
        st = cse.ChannelState()
        for _ in range(3):
            cse.update_channel_state(st, TS, {"12H": None})
        for _ in range(7):
            cse.update_channel_state(st, TS, {"1D": None})
        stale = cse.staleness_by_tf(st)
        self.assertEqual(stale["12H"], 3)
        self.assertEqual(stale["1D"],  7)
        self.assertEqual(stale["1H"],  0)
        self.assertEqual(stale["4H"],  0)


class TestUnknownTfIgnored(unittest.TestCase):

    def test_unknown_tf_does_not_raise(self):
        st = cse.ChannelState()
        # Should not raise
        cse.update_channel_state(st, TS, {"8H": 0.5, "1H": 0.3})
        # 1H should be stored, 8H silently ignored
        self.assertAlmostEqual(st.last_valid_cp_1h, 0.3)


class TestReplaySimulation(unittest.TestCase):
    """End-to-end simulation of a short replay to verify last-valid behaviour."""

    def test_effective_cp_survives_gap(self):
        """
        Simulates:
          tick 1 — 12H valid (0.55)
          tick 2 — 12H None  → effective still 0.55
          tick 3 — 12H None  → effective still 0.55
          tick 4 — 12H valid (0.65) → effective updates
        """
        st = cse.ChannelState()

        cse.update_channel_state(st, "T1", {"12H": 0.55})
        eff1 = cse.effective_cp_by_tf(st)["12H"]
        self.assertAlmostEqual(eff1, 0.55)

        cse.update_channel_state(st, "T2", {"12H": None})
        eff2 = cse.effective_cp_by_tf(st)["12H"]
        self.assertAlmostEqual(eff2, 0.55, msg="effective must not change on None tick")

        cse.update_channel_state(st, "T3", {"12H": None})
        eff3 = cse.effective_cp_by_tf(st)["12H"]
        self.assertAlmostEqual(eff3, 0.55)
        self.assertEqual(st.stale_ticks_12h, 2)

        cse.update_channel_state(st, "T4", {"12H": 0.65})
        eff4 = cse.effective_cp_by_tf(st)["12H"]
        self.assertAlmostEqual(eff4, 0.65)
        self.assertEqual(st.stale_ticks_12h, 0)

    def test_trend_not_unknown_after_gap(self):
        """Effective CP from last-valid prevents UNKNOWN trend state."""
        import channel_trend_engine as cte

        st = cse.ChannelState()
        # Seed both 4H and 12H
        cse.update_channel_state(st, "T1", {"4H": 0.70, "12H": 0.65})
        # Simulate both going None next tick
        cse.update_channel_state(st, "T2", {"4H": None, "12H": None})

        eff = cse.effective_cp_by_tf(st)
        trend = cte.compute_trend_state(eff.get("4H"), eff.get("12H"))
        self.assertEqual(trend, "LONG",
                         msg="effective CP should keep trend non-UNKNOWN after gap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
