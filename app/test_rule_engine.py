import unittest

from rule_engine import (
    RuleLevels,
    TickContext,
    evaluate,
    Action,
    compute_insufficient_rise,
)


class TestRuleEngine(unittest.TestCase):

    def test_compute_insufficient_rise(self):
        self.assertTrue(compute_insufficient_rise(100.0, 103.0))
        self.assertFalse(compute_insufficient_rise(104.0, 103.0))
        self.assertFalse(compute_insufficient_rise(None, 103.0))
        self.assertFalse(compute_insufficient_rise(100.0, None))

    def test_rule6_panic_buy_triggers(self):
        lv = RuleLevels(
            std_sell=97.0,
            profitless_sell=95.0,
            panic_sell=90.0,
            std_buy=103.0,
            profitless_buy=110.0,
            panic_buy=120.0,
        )

        t = TickContext(
            in_position=False,
            prev_last=119.5,
            last=120.0,
        )

        d = evaluate(lv, t)
        self.assertEqual(d.action, Action.BUY_PANIC)

    def test_rule6_no_cross(self):
        lv = RuleLevels(
            std_sell=97.0,
            profitless_sell=95.0,
            panic_sell=90.0,
            std_buy=103.0,
            profitless_buy=110.0,
            panic_buy=120.0,
        )

        t = TickContext(
            in_position=False,
            prev_last=120.0,
            last=120.2,
        )

        d = evaluate(lv, t)
        self.assertNotEqual(d.action, Action.BUY_PANIC)

    def test_rule6_not_rising(self):
        lv = RuleLevels(
            std_sell=97.0,
            profitless_sell=95.0,
            panic_sell=90.0,
            std_buy=103.0,
            profitless_buy=110.0,
            panic_buy=120.0,
        )

        t = TickContext(
            in_position=False,
            prev_last=119.8,
            last=119.7,
        )

        d = evaluate(lv, t)
        self.assertEqual(d.action, Action.NONE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
