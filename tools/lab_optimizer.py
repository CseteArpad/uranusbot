import csv
import os

PRICES_FILE = "/opt/bots/uranus_monitor/data/prices.csv"
REPORT_FILE = "/opt/bots/uranus/reports/lab_optimizer_report.txt"

STD_SELL_RANGE = [0.01, 0.015, 0.02]
STD_BUY_RANGE = [0.008, 0.012, 0.015]
MA_BAND_RANGE = [0.0003, 0.0005, 0.001]

START_BALANCE = 1000.0


def detect_price_column(fieldnames):
    candidates = [
        "price",
        "last",
        "last_price",
        "close",
        "rate",
        "value",
        "current_price",
    ]

    for c in candidates:
        if c in fieldnames:
            return c

    raise RuntimeError(
        "Nem található árfolyam oszlop. Elérhető oszlopok: "
        + ", ".join(fieldnames)
    )


def load_prices():
    if not os.path.exists(PRICES_FILE):
        raise FileNotFoundError(f"Nem található fájl: {PRICES_FILE}")

    prices = []

    with open(PRICES_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise RuntimeError("A prices.csv üres vagy nincs fejlécsora.")

        price_col = detect_price_column(reader.fieldnames)

        for row in reader:
            raw = row.get(price_col)

            if raw is None or raw == "":
                continue

            try:
                prices.append(float(str(raw).replace(",", ".")))
            except ValueError:
                continue

    if len(prices) < 5:
        raise RuntimeError("Túl kevés árfolyamadat van a szimulációhoz.")

    return prices


def simulate(prices, std_sell_pct, std_buy_pct):
    balance = START_BALANCE
    position = 0.0
    entry_price = 0.0
    trade_count = 0

    for i in range(1, len(prices)):
        prev = prices[i - 1]
        curr = prices[i]

        if position == 0:
            if curr > prev * (1 + std_buy_pct):
                position = balance / curr
                entry_price = curr
                balance = 0.0
                trade_count += 1
        else:
            if curr < prev * (1 - std_sell_pct):
                balance = position * curr
                position = 0.0
                trade_count += 1

    if position > 0:
        balance = position * prices[-1]

    profit = balance - START_BALANCE
    return profit, trade_count


def run_optimizer():
    prices = load_prices()
    results = []

    for sell in STD_SELL_RANGE:
        for buy in STD_BUY_RANGE:
            for ma in MA_BAND_RANGE:
                profit, trades = simulate(prices, sell, buy)

                results.append({
                    "profit": profit,
                    "trades": trades,
                    "std_sell": sell,
                    "std_buy": buy,
                    "ma_band": ma,
                })

    filtered = [r for r in results if r["trades"] <= 20]

    if filtered:
        best = max(filtered, key=lambda x: x["profit"])
        status = "RENDBEN"
    else:
        best = max(results, key=lambda x: x["profit"])
        status = "FIGYELENDŐ"

    return best, status, len(prices)


def write_report(best, status, price_count):
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

    with open(REPORT_FILE, "w") as f:
        f.write("URANUS LAB OPTIMIZER REPORT\n")
        f.write("=" * 40 + "\n\n")
        f.write(f"Beolvasott árfolyam pontok száma: {price_count}\n\n")
        f.write(f"Minősítés: {status}\n\n")

        f.write("Javasolt paraméterek:\n")
        f.write(f"STD_SELL_PCT = {best['std_sell']}\n")
        f.write(f"STD_BUY_PCT = {best['std_buy']}\n")
        f.write(f"MA_SIDEWAYS_BAND_PCT = {best['ma_band']}\n\n")

        f.write(f"Szimulált profit: {round(best['profit'], 2)}\n")
        f.write(f"Trade-ek száma: {best['trades']}\n\n")

        f.write("FONTOS:\n")
        f.write("Ez csak labor-javaslat.\n")
        f.write("Nem módosított éles beállítást.\n")
        f.write("Nem indított újra service-t.\n")


def main():
    best, status, price_count = run_optimizer()
    write_report(best, status, price_count)
    print("Kész. Jelentés mentve:")
    print(REPORT_FILE)


if __name__ == "__main__":
    main()
