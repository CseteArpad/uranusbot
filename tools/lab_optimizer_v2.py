import csv
import os
import sys
from itertools import product
from types import SimpleNamespace

MONITOR_PATH = "/opt/bots/uranus_monitor/app"
PRICES_FILE = "/opt/bots/uranus_monitor/data/prices.csv"
REPORT_FILE = "/opt/bots/uranus/reports/lab_optimizer_v2_report.txt"

sys.path.append(MONITOR_PATH)

from monitor_app import simulate_strategy


PARAM_GRID = {
    "std_sell_pct": [1.2, 1.5, 2.0],
    "std_buy_pct": [0.8, 1.2, 1.5],
    "panic_sell_pct": [3.0, 5.0],
    "panic_buy_pct": [3.0, 5.0],
    "recovery_sell_retrace_pct": [1.0, 1.5],
    "recovery_buy_rebound_pct": [1.0, 1.5],
    "ma_sideways_band_pct": [0.03, 0.05],
}


def load_prices():
    rows = []

    with open(PRICES_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if "last" not in row:
                continue

            try:
                price = float(str(row["last"]).replace(",", "."))
            except Exception:
                continue

            rows.append({
                "timestamp": row.get("timestamp_utc", ""),
                "price": price,
            })

    if len(rows) < 10:
        raise RuntimeError("Túl kevés árfolyamadat van a Strategy Lab szimulációhoz.")

    return rows


def make_params(values):
    return SimpleNamespace(
        initial_capital=1000.0,
        fee_pct=0.075,
        slippage_pct=0.0,

        std_sell_enabled=True,
        std_buy_enabled=True,
        recovery_sell_enabled=True,
        recovery_buy_enabled=True,
        panic_sell_enabled=True,
        panic_buy_enabled=True,
        catastrophe_sell_enabled=False,
        catastrophe_buy_enabled=False,

        std_sell_pct=values["std_sell_pct"],
        std_buy_pct=values["std_buy_pct"],
        panic_sell_pct=values["panic_sell_pct"],
        panic_buy_pct=values["panic_buy_pct"],
        recovery_sell_retrace_pct=values["recovery_sell_retrace_pct"],
        recovery_buy_rebound_pct=values["recovery_buy_rebound_pct"],
        ma_sideways_band_pct=values["ma_sideways_band_pct"],

        recovery_profit_target_pct=0.0,
        recovery_max_loss_pct=0.0,
        catastrophe_sell_pct=10.0,
        catastrophe_buy_pct=10.0,

        ma_filter_enabled=True,
        ma_short_period=7,
        ma_long_period=25,

        rules_source="lab",
        threshold_mode="lab",
    )


def generate_param_sets():
    keys = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())

    for combo in product(*values):
        yield dict(zip(keys, combo))


def run_one(prices, values):
    params = make_params(values)
    result = simulate_strategy(prices, params)

    summary = result.get("summary", {}) if isinstance(result, dict) else {}

    return {
        "params": values,
        "pnl": float(summary.get("pnl", 0.0) or 0.0),
        "pnl_pct": float(summary.get("pnl_pct", 0.0) or 0.0),
        "trade_count": int(summary.get("trade_count", 0) or 0),
        "max_drawdown_pct": float(summary.get("max_drawdown_pct", 0.0) or 0.0),
        "win_rate": float(summary.get("win_rate", 0.0) or 0.0),
        "closed_trade_count": int(summary.get("closed_trade_count", 0) or 0),
        "signal_count": int(summary.get("signal_count", 0) or 0),
    }


def is_valid(r):
    if r["trade_count"] < 1:
        return False

    if r["trade_count"] > 20:
        return False

    if r["max_drawdown_pct"] > 5.0:
        return False

    if r["pnl"] <= 0:
        return False

    return True


def run_optimizer():
    prices = load_prices()
    results = []
    errors = 0

    for values in generate_param_sets():
        try:
            r = run_one(prices, values)
            results.append(r)
        except Exception:
            errors += 1

    if not results:
        raise RuntimeError("Egyetlen Strategy Lab futtatás sem sikerült. Ellenőrizni kell a params mezőket.")

    valid = [r for r in results if is_valid(r)]

    if valid:
        best = max(valid, key=lambda x: x["pnl"])
        status = "RENDBEN"
    else:
        best = max(results, key=lambda x: x["pnl"])
        status = "FIGYELENDŐ"

    return best, status, len(prices), len(results), errors, len(valid)


def write_report(best, status, price_count, total_runs, errors, valid_count):
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

    with open(REPORT_FILE, "w") as f:
        f.write("URANUS LAB OPTIMIZER V2 REPORT\n")
        f.write("=" * 50 + "\n\n")

        f.write(f"Beolvasott árfolyam pontok száma: {price_count}\n")
        f.write(f"Sikeres futtatások: {total_runs}\n")
        f.write(f"Hibás futtatások: {errors}\n")
        f.write(f"Érvényes jelöltek: {valid_count}\n\n")

        f.write(f"Minősítés: {status}\n\n")

        f.write("LEGJOBB LAB PARAMÉTEREK:\n")
        for k, v in best["params"].items():
            f.write(f"{k} = {v}\n")

        f.write("\nEREDMÉNY:\n")
        f.write(f"PNL: {round(best['pnl'], 4)}\n")
        f.write(f"PNL %: {round(best['pnl_pct'], 4)}\n")
        f.write(f"Trade count: {best['trade_count']}\n")
        f.write(f"Closed trade count: {best['closed_trade_count']}\n")
        f.write(f"Signal count: {best['signal_count']}\n")
        f.write(f"Win rate: {round(best['win_rate'], 4)}\n")
        f.write(f"Max drawdown %: {round(best['max_drawdown_pct'], 4)}\n\n")

        f.write("FONTOS:\n")
        f.write("Ez Strategy Lab alapú javaslat.\n")
        f.write("Nem módosított éles beállítást.\n")
        f.write("Nem indított újra service-t.\n")


def main():
    best, status, price_count, total_runs, errors, valid_count = run_optimizer()
    write_report(best, status, price_count, total_runs, errors, valid_count)
    print("Kész. Jelentés:")
    print(REPORT_FILE)


if __name__ == "__main__":
    main()
