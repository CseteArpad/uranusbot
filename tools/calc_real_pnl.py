import csv

FEE = 0.0015  # 0.15%

trades = []

with open("/opt/bots/uranus/trades.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        trades.append(row)

pnl = 0
pairs = []

for i in range(len(trades)-1):
    t1 = trades[i]
    t2 = trades[i+1]

    if t1["side"] == "BUY" and t2["side"] == "SELL":
        buy = float(t1["price"])
        sell = float(t2["price"])

        gross = sell - buy
        fee = (buy + sell) * FEE
        net = gross - fee

        pnl += net
        pairs.append(net)

print("Trade párok:", len(pairs))
print("Összesített PNL:", round(pnl, 4))
print("Átlag / trade:", round(pnl/len(pairs), 6) if pairs else 0)

wins = sum(1 for x in pairs if x > 0)
print("Valódi win rate:", round(wins/len(pairs)*100,2) if pairs else 0, "%")
