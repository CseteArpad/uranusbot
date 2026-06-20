from tick_context import TickContextBuilder
from rule_engine import RuleEngine

def _mk_ctx_in_position(base_price: float, peak_price: float, last_price: float, prev_last: float):
    state = {}
    pair = "TEST/USDC"

    # 1) belépünk poziba -> base init
    TickContextBuilder.build(pair=pair, state=state, last_price=float(base_price), in_position=True)
    # 2) peak-et felhúzzuk
    TickContextBuilder.build(pair=pair, state=state, last_price=float(peak_price), in_position=True)
    # 3) teszt tick
    ctx = TickContextBuilder.build(pair=pair, state=state, last_price=float(last_price), in_position=True)

    # prev_last kényszerítés (irány detektálás)
    ctx["prev_last"] = float(prev_last)
    return ctx

def test_panic_sell():
    # panic_sell mindig base alatt, itt csak triggerelünk
    ctx = _mk_ctx_in_position(base_price=105.0, peak_price=109.0, last_price=105.0, prev_last=106.0)
    levels = ctx["levels"]
    panic_sell = float(levels["panic_sell"])

    last = panic_sell - 0.01
    prev_last = last + 0.50
    ctx["last"] = last
    ctx["prev_last"] = prev_last

    d = RuleEngine.evaluate_sell(ctx)
    assert d is not None, "Nincs döntés"
    assert getattr(d, "action", None) == "SELL", f"Nem SELL: {d.__dict__ if hasattr(d,'__dict__') else d}"
    assert "PANIC_SELL" in getattr(d, "reason", ""), f"Nem PANIC_SELL: {getattr(d,'reason','')}"
    print("OK: PANIC SELL")

def test_standard_sell():
    # Standard SELL: last < prev_last és last <= std_sell
    ctx = _mk_ctx_in_position(base_price=105.0, peak_price=109.0, last_price=105.0, prev_last=106.0)
    levels = ctx["levels"]
    std_sell = float(levels["std_sell"])

    last = std_sell - 0.01
    prev_last = last + 0.50
    ctx["last"] = last
    ctx["prev_last"] = prev_last

    d = RuleEngine.evaluate_sell(ctx)
    assert d is not None, "Nincs döntés"
    assert getattr(d, "action", None) == "SELL", f"Nem SELL: {d.__dict__ if hasattr(d,'__dict__') else d}"
    assert "STANDARD_SELL" in getattr(d, "reason", ""), f"Nem STANDARD_SELL: {getattr(d,'reason','')}"
    print("OK: STANDARD SELL")
    print(f"prev_last={prev_last:.4f} last={last:.4f} std_sell={std_sell:.4f} profitless_sell={float(levels['profitless_sell']):.4f} panic_sell={float(levels['panic_sell']):.4f}")

def main():
    print("SELFTEST - Uranus SELL")
    test_panic_sell()
    test_standard_sell()
    print("ALL OK (SELL)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
