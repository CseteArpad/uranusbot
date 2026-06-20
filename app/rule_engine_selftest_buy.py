from tick_context import TickContextBuilder
from rule_engine import RuleEngine

def _mk_ctx_out_of_position(base_price: float, trough_price: float, last_price: float, prev_last: float):
    state = {}
    pair = "TEST/USDC"

    # felveszünk egy base-t még poziban (hogy legyen értelmezhető referencia)
    TickContextBuilder.build(pair=pair, state=state, last_price=float(base_price), in_position=True)

    # lemegyünk trough-ig még poziban
    TickContextBuilder.build(pair=pair, state=state, last_price=float(trough_price), in_position=True)

    # kilépünk (out_of_position) – trough maradjon a state-ben out-hoz
    TickContextBuilder.build(pair=pair, state=state, last_price=float(trough_price), in_position=False)

    # teszt tick out_of_position
    ctx = TickContextBuilder.build(pair=pair, state=state, last_price=float(last_price), in_position=False)
    ctx["prev_last"] = float(prev_last)
    return ctx

def test_standard_buy():
    # Standard BUY: last > prev_last és last >= std_buy
    ctx = _mk_ctx_out_of_position(base_price=105.0, trough_price=103.0, last_price=103.0, prev_last=102.0)
    levels = ctx["levels"]
    std_buy = float(levels["std_buy"])

    last = std_buy + 0.01
    prev_last = last - 0.50
    ctx["last"] = last
    ctx["prev_last"] = prev_last

    d = RuleEngine.evaluate_buy(ctx)
    assert d is not None, "Nincs döntés"
    assert getattr(d, "action", None) == "BUY", f"Nem BUY: {d.__dict__ if hasattr(d,'__dict__') else d}"
    assert "STANDARD_BUY" in getattr(d, "reason", ""), f"Nem STANDARD_BUY: {getattr(d,'reason','')}"
    print("OK: STANDARD BUY")

def main():
    print("SELFTEST - Uranus BUY")
    test_standard_buy()
    print("ALL OK (BUY)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
