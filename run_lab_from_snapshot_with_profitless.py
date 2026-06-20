#!/usr/bin/env python3
import csv
import sys
from collections import Counter

CSV_PATH = "/opt/bots/uranus/lab_snapshots/prices_2026-03-28_1230.csv"

DEFAULTS = {
    "initial_capital": 100.0,
    "fee_pct": 0.1,
    "slippage_pct": 0.0,

    "std_sell_enabled": 1,
    "profitless_sell_enabled": 1,
    "panic_sell_enabled": 1,

    "std_buy_enabled": 1,
    "profitless_buy_enabled": 1,
    "panic_buy_enabled": 1,

    "std_sell_pct": 0.95,
    "std_buy_pct": 0.795,
    "panic_sell_pct": 0.03,
    "panic_buy_pct": 1.495,

    # ÚJ: profitless paraméterek
    "profitless_sell_pct": 1.0025,
    "profitless_buy_pct": 0.9975,

    "panic_reentry_guard_enabled": 1,
    "panic_reentry_fee_buffer_pct": 0.19,
}

def parse_args(argv):
    params = DEFAULTS.copy()
    for arg in argv[1:]:
        if "=" not in arg:
            continue
        k, v = arg.split("=", 1)
        if k not in params:
            continue
        if isinstance(params[k], float):
            params[k] = float(v)
        elif isinstance(params[k], int):
            params[k] = int(float(v))
    return params

def load_prices(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for r in reader:
            try:
                rows.append(float(r[1]))
            except:
                continue
    return rows

def run(params):
    prices = load_prices(CSV_PATH)

    capital = params["initial_capital"]
    position = 0.0
    in_position = False

    base = None
    peak = None
    trough = None

    actions = Counter()
    rules = Counter()

    for i in range(1, len(prices)):
        prev = prices[i-1]
        last = prices[i]

        rising = last > prev
        falling = last < prev

        if not in_position:
            if trough is None or last < trough:
                trough = last

            std_buy = trough * params["std_buy_pct"]
            panic_buy = base * params["panic_buy_pct"] if base else None
            profitless_buy = base * params["profitless_buy_pct"] if base else None

            # PANIC BUY
            if panic_buy and rising and last >= panic_buy:
                qty = capital / last
                position = qty
                capital = 0
                in_position = True
                base = last
                peak = last
                actions["BUY"] += 1
                rules["PANIC_BUY"] += 1
                continue

            # PROFITLESS BUY
            if profitless_buy and rising and last >= profitless_buy:
                qty = capital / last
                position = qty
                capital = 0
                in_position = True
                base = last
                peak = last
                actions["BUY"] += 1
                rules["PROFITLESS_BUY"] += 1
                continue

            # STANDARD BUY
            if rising and last >= std_buy:
                qty = capital / last
                position = qty
                capital = 0
                in_position = True
                base = last
                peak = last
                actions["BUY"] += 1
                rules["STANDARD_BUY"] += 1
                continue

        else:
            if peak is None or last > peak:
                peak = last

            std_sell = peak * params["std_sell_pct"]
            panic_sell = base * (1 - params["panic_sell_pct"])
            profitless_sell = base * params["profitless_sell_pct"]

            # PANIC SELL
            if falling and last <= panic_sell:
                capital = position * last
                position = 0
                in_position = False
                base = last
                trough = last
                actions["SELL"] += 1
                rules["PANIC_SELL"] += 1
                continue

            # PROFITLESS SELL
            if falling and last <= profitless_sell:
                capital = position * last
                position = 0
                in_position = False
                base = last
                trough = last
                actions["SELL"] += 1
                rules["PROFITLESS_SELL"] += 1
                continue

            # STANDARD SELL
            if falling and last <= std_sell:
                capital = position * last
                position = 0
                in_position = False
                base = last
                trough = last
                actions["SELL"] += 1
                rules["STANDARD_SELL"] += 1
                continue

    final_equity = capital if not in_position else position * prices[-1]
    pnl_pct = (final_equity - params["initial_capital"]) / params["initial_capital"] * 100

    print("FINAL_EQUITY=", round(final_equity, 6))
    print("PNL_PCT=", round(pnl_pct, 6))
    print("ACTIONS=", dict(actions))
    print("RULES=", dict(rules))

if __name__ == "__main__":
    run(parse_args(sys.argv))
