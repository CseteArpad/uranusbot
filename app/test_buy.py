import json
from tick_context import TickContextBuilder
from rule_engine import RuleEngine

STATE_PATH="/opt/bots/uranus/state.json"
PAIR="XRP/USDC"

s=json.load(open(STATE_PATH,"r",encoding="utf-8"))
ps=((s.get("pairs") or {}).get(PAIR) or {})
last_price=float(ps.get("last"))
in_position=bool(ps.get("in_position", False))

ctx=TickContextBuilder.build(pair=PAIR, state=s, last_price=last_price, in_position=in_position)
lv=ctx.get("levels") or {}
std_buy=float(lv["std_buy"])
profitless_buy=float(lv["profitless_buy"])
panic_buy=float(lv["panic_buy"])

# STANDARD_BUY kényszer: std_buy és profitless_buy közé
last_forced=(std_buy + profitless_buy) / 2.0
ctx["last"]=last_forced
ctx["prev_last"]=last_forced - 0.02

d=RuleEngine.evaluate_buy(ctx)

print("std_buy:", std_buy)
print("profitless_buy:", profitless_buy)
print("panic_buy:", panic_buy)
print("FORCED last:", ctx["last"], "prev_last:", ctx["prev_last"])
print("DECISION:", d.__dict__)
