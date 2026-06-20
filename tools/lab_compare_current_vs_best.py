import csv
import os
import sys
from types import SimpleNamespace

MONITOR_PATH = "/opt/bots/uranus_monitor/app"
PRICES_FILE = "/opt/bots/uranus_monitor/data/prices.csv"
REPORT_FILE = "/opt/bots/uranus/reports/lab_compare_current_vs_best.txt"

sys.path.append(MONITOR_PATH)

from monitor_app import simulate_strategy


def load_prices():
    rows = []
    with open(PRICES_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                price = float(str(row["last"]).replace(",", "."))
            except Exception:
                continue
            rows.append({
                "timestamp": row.get("timestamp_utc", ""),
                "price": price,
            })

    if len(rows) < 10:
        raise RuntimeError("Túl kevés árfolyamadat.")

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


def simulate_named(name, prices, values):
    result = simulate_strategy(prices, make_params(values))
    summary = result.get("summary", {})

    return {
        "name": name,
        "params": values,
        "pnl": float(summary.get("pnl", 0.0) or 0.0),
        "pnl_pct": float(summary.get("pnl_pct", 0.0) or 0.0),
        "trade_count": int(summary.get("trade_count", 0) or 0),
        "closed_trade_count": int(summary.get("closed_trade_count", 0) or 0),
        "signal_count": int(summary.get("signal_count", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0.0) or 0.0),
        "max_drawdown_pct": float(summary.get("max_drawdown_pct", 0.0) or 0.0),
    }


def decision(current, best):
    pnl_gain = best["pnl_pct"] - current["pnl_pct"]
    dd_extra = best["max_drawdown_pct"] - current["max_drawdown_pct"]

    if best["pnl"] <= current["pnl"]:
        return "NEM JAVASOLT", "A labor beállítás nem ad jobb PNL-t."

    if best["max_drawdown_pct"] > 5.0:
        return "NEM JAVASOLT", "A labor beállítás drawdownja 5% fölé menne."

    if best["trade_count"] > 20:
        return "NEM JAVASOLT", "A labor beállítás túl sok trade-et generálna."

    if pnl_gain < 1.0:
        return "FIGYELENDŐ", "A labor beállítás jobb, de az előny kisebb mint 1 százalékpont."

    if dd_extra > 2.0:
        return "FIGYELENDŐ", "A profit jobb, de a drawdown több mint 2 százalékponttal nő."

    return "JAVASOLT", "A labor beállítás jobb PNL-t ad elfogadható kockázat mellett."


def write_report(current, best, verdict, reason, price_count):
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

    with open(REPORT_FILE, "w") as f:
        f.write("URANUS LAB CURRENT VS BEST REPORT\n")
        f.write("=" * 55 + "\n\n")

        f.write(f"Beolvasott árfolyam pontok száma: {price_count}\n\n")

        for item in [current, best]:
            f.write(f"{item['name']}\n")
            f.write("-" * 30 + "\n")
            f.write("Paraméterek:\n")
            for k, v in item["params"].items():
                f.write(f"  {k} = {v}\n")

            f.write("Eredmény:\n")
            f.write(f"  PNL: {round(item['pnl'], 4)}\n")
            f.write(f"  PNL %: {round(item['pnl_pct'], 4)}\n")
            f.write(f"  Trade count: {item['trade_count']}\n")
            f.write(f"  Closed trade count: {item['closed_trade_count']}\n")
            f.write(f"  Signal count: {item['signal_count']}\n")
            f.write(f"  Win rate: {round(item['win_rate'], 4)}\n")
            f.write(f"  Max drawdown %: {round(item['max_drawdown_pct'], 4)}\n\n")

        f.write("DÖNTÉS\n")
        f.write("-" * 30 + "\n")
        f.write(f"Minősítés: {verdict}\n")
        f.write(f"Indoklás: {reason}\n\n")

        f.write("FONTOS:\n")
        f.write("Ez csak összehasonlító laborriport.\n")
        f.write("Nem módosított éles beállítást.\n")
        f.write("Nem indított újra service-t.\n")


def main():
    prices = load_prices()

    current_values = {
        "std_sell_pct": 1.5,
        "std_buy_pct": 1.2,
        "panic_sell_pct": 5.0,
        "panic_buy_pct": 5.0,
        "recovery_sell_retrace_pct": 1.5,
        "recovery_buy_rebound_pct": 1.5,
        "ma_sideways_band_pct": 0.05,
    }

    best_values = {
        "std_sell_pct": 1.5,
        "std_buy_pct": 1.2,
        "panic_sell_pct": 3.0,
        "panic_buy_pct": 3.0,
        "recovery_sell_retrace_pct": 1.0,
        "recovery_buy_rebound_pct": 1.0,
        "ma_sideways_band_pct": 0.03,
    }

    current = simulate_named("CURRENT ÉLES BEÁLLÍTÁS", prices, current_values)
    best = simulate_named("LAB BEST BEÁLLÍTÁS", prices, best_values)

    verdict, reason = decision(current, best)

    write_report(current, best, verdict, reason, len(prices))

    print("Kész. Jelentés:")
    print(REPORT_FILE)


if __name__ == "__main__":
    main()
