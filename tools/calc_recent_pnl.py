import csv
from datetime import datetime

FILE = "/opt/bots/uranus_monitor/data/trade_events.csv"
START = datetime.fromisoformat("2026-04-25 00:00:00")
FEE_SIDE = 0.00075

rows = []

with open(FILE, newline="") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            ts = datetime.fromisoformat(r["timestamp"])
            if ts < START:
                continue

            side = r["side"].upper().strip()
            if side not in ("BUY", "SELL"):
                continue

            rows.append({
                "ts": ts,
                "side": side,
                "price": float(str(r["price"]).replace(",", ".")),
                "amount": float(str(r["amount"]).replace(",", ".")),
                "trade_id": r.get("trade_id", ""),
                "profit_csv": float(str(r.get("profit") or 0).replace(",", ".")),
            })
        except Exception:
            continue

rows.sort(key=lambda x: x["ts"])

position_amount = 0.0
avg_buy_price = 0.0
closed = []

for r in rows:
    if r["side"] == "BUY":
        old_cost = position_amount * avg_buy_price
        new_cost = r["amount"] * r["price"]
        position_amount += r["amount"]
        avg_buy_price = (old_cost + new_cost) / position_amount

    elif r["side"] == "SELL" and position_amount > 0:
        sell_amount = min(r["amount"], position_amount)

        gross = (r["price"] - avg_buy_price) * sell_amount
        fee = (avg_buy_price * sell_amount * FEE_SIDE) + (r["price"] * sell_amount * FEE_SIDE)
        net = gross - fee

        closed.append({
            "ts": r["ts"],
            "buy": avg_buy_price,
            "sell": r["price"],
            "amount": sell_amount,
            "gross": gross,
            "fee": fee,
            "net": net,
        })

        position_amount -= sell_amount
        if position_amount <= 0.00000001:
            position_amount = 0.0
            avg_buy_price = 0.0

total = sum(x["net"] for x in closed)
wins = sum(1 for x in closed if x["net"] > 0)

print("Időszak kezdete:", START)
print("Beolvasott trade sor:", len(rows))
print("Lezárt körök:", len(closed))
print("Nettó PNL fee után:", round(total, 6))
print("Valódi win rate:", round(wins / len(closed) * 100, 2) if closed else 0, "%")
print("Nyitott mennyiség:", round(position_amount, 8))
print()

print("Lezárt körök részletesen:")
for x in closed:
    print(
        x["ts"],
        "BUY", round(x["buy"], 6),
        "SELL", round(x["sell"], 6),
        "AMOUNT", round(x["amount"], 6),
        "GROSS", round(x["gross"], 6),
        "FEE", round(x["fee"], 6),
        "NET", round(x["net"], 6)
    )
