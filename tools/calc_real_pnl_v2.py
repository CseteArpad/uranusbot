import csv
from datetime import datetime

FILE = "/opt/bots/uranus_monitor/data/trade_events.csv"

FEE_SIDE = 0.00075  # 0.075% oldalanként

def parse_time(s):
    s = (s or "").replace("Z", "+00:00")
    return datetime.fromisoformat(s)

rows = []

with open(FILE, newline="") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            side = r.get("side", "").upper().strip()
            if side not in ("BUY", "SELL"):
                continue

            ts = r.get("timestamp_utc") or r.get("timestamp") or r.get("time") or r.get("Időpont")
            price = float(str(r.get("price") or r.get("Price")).replace(",", "."))
            amount = float(str(r.get("amount") or r.get("Amount") or 1).replace(",", "."))

            rows.append({
                "ts": parse_time(ts),
                "side": side,
                "price": price,
                "amount": amount,
            })
        except Exception:
            continue

rows.sort(key=lambda x: x["ts"])

position_amount = 0.0
avg_buy_price = 0.0

closed = []
buys = 0
sells = 0

for r in rows:
    side = r["side"]
    price = r["price"]
    amount = r["amount"]

    if side == "BUY":
        buys += 1
        total_cost_old = position_amount * avg_buy_price
        total_cost_new = amount * price
        position_amount += amount
        if position_amount > 0:
            avg_buy_price = (total_cost_old + total_cost_new) / position_amount

    elif side == "SELL" and position_amount > 0:
        sells += 1
        sell_amount = min(amount, position_amount)

        gross = (price - avg_buy_price) * sell_amount
        fee = (avg_buy_price * sell_amount * FEE_SIDE) + (price * sell_amount * FEE_SIDE)
        net = gross - fee

        closed.append({
            "buy": avg_buy_price,
            "sell": price,
            "amount": sell_amount,
            "gross": gross,
            "fee": fee,
            "net": net,
        })

        position_amount -= sell_amount
        if position_amount <= 0.00000001:
            position_amount = 0.0
            avg_buy_price = 0.0

total_net = sum(x["net"] for x in closed)
wins = sum(1 for x in closed if x["net"] > 0)

print("Beolvasott trade sor:", len(rows))
print("BUY sor:", buys)
print("SELL sor:", sells)
print("Lezárt pozíciók:", len(closed))
print("Nettó PNL fee után:", round(total_net, 6))
print("Valódi win rate:", round((wins / len(closed) * 100), 2) if closed else 0, "%")
print("Nyitott mennyiség:", round(position_amount, 8))
print()

print("Utolsó 10 lezárt trade:")
for x in closed[-10:]:
    print(
        "BUY", round(x["buy"], 6),
        "SELL", round(x["sell"], 6),
        "AMOUNT", round(x["amount"], 6),
        "NET", round(x["net"], 6)
    )
