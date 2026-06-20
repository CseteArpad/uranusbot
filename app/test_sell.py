import json
from tick_context import TickContextBuilder
from rule_engine import RuleEngine

STATE_PATH="/opt/bots/uranus/state.json"
PAIR="XRP/USDC"

def decision_dict(d):
    try:
        return d.__dict__
    except Exception:
        return {"repr": repr(d)}

s=json.load(open(STATE_PATH,"r",encoding="utf-8"))
ps=((s.get("pairs") or {}).get(PAIR) or {})
last_price=float(ps.get("last"))
# SELL teszthez pozícióban kell lennünk
in_position=True

ctx=TickContextBuilder.build(pair=PAIR, state=s, last_price=last_price, in_position=in_position)
lv=ctx.get("levels") or {}
std_sell=float(lv["std_sell"])
profitless_sell=float(lv["profitless_sell"])
panic_sell=float(lv["panic_sell"])

print("LEVELS std_sell:", std_sell, "profitless_sell:", profitless_sell, "panic_sell:", panic_sell)
print("NOTE: SELL prioritás: PANIC -> PROFITLESS -> STANDARD (rule_engine sorrend szerint)")

# --- 1) STANDARD_SELL: std_sell és profitless_sell közé (így profitless/panic nem aktiválódik)
ctx1=dict(ctx)
ctx1["prev_last"]=std_sell + 0.02
ctx1["last"]= (std_sell + profitless_sell) / 2.0
d1=RuleEngine.evaluate_sell(ctx1)
print("\n[TEST1 STANDARD_SELL]")
print("FORCED last:", ctx1["last"], "prev_last:", ctx1["prev_last"])
print("DECISION:", decision_dict(d1))

# --- 2) PROFITLESS_SELL: profitless_sell alá, de panic_sell fölé + not_enough_rise=True
ctx2=dict(ctx)
ctx2["flags"]=dict(ctx.get("flags") or {})
ctx2["flags"]["not_enough_rise"]=True
ctx2["prev_last"]=profitless_sell + 0.02
ctx2["last"]=profitless_sell - 0.02
# biztosan ne essen pánik alá
if ctx2["last"] <= panic_sell:
    ctx2["last"]=panic_sell + 0.02
d2=RuleEngine.evaluate_sell(ctx2)
print("\n[TEST2 PROFITLESS_SELL]")
print("FORCED last:", ctx2["last"], "prev_last:", ctx2["prev_last"], "not_enough_rise:", ctx2["flags"].get("not_enough_rise"))
print("DECISION:", decision_dict(d2))

# --- 3) PANIC_SELL: panic_sell alá
ctx3=dict(ctx)
ctx3["prev_last"]=panic_sell + 0.02
ctx3["last"]=panic_sell - 0.02
d3=RuleEngine.evaluate_sell(ctx3)
print("\n[TEST3 PANIC_SELL]")
print("FORCED last:", ctx3["last"], "prev_last:", ctx3["prev_last"])
print("DECISION:", decision_dict(d3))
