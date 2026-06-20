#!/usr/bin/env python3
import csv
import sys
from collections import Counter

CSV_PATH = "/opt/bots/uranus/latest_prices.csv"

DEFAULTS = {
    "initial_capital": 100.0,
    "fee_pct": 0.1,
    "slippage_pct": 0.0,

    "std_sell_enabled": 1,
    "profitless_sell_enabled": 0,
    "panic_sell_enabled": 1,

    "std_buy_enabled": 1,
    "profitless_buy_enabled": 0,
    "panic_buy_enabled": 1,

    "std_sell_pct": 0.5,
    "std_buy_pct": 0.8,
    "panic_sell_pct": 0.08,
    "panic_buy_pct": 0.15,

    "panic_buy_confirm_ticks": 1,
    "ma_filter_enabled": 1,
    "ma_period": 20,
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
        else:
            params[k] = v
    return params

def load_prices(path):
    rows = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts = r.get("timestamp_utc") or r.get("timestamp") or r.get("ts")
            val = r.get("last") or r.get("price")

            if not ts or val in (None, ""):
                continue

            try:
                dt = ts
                price = float(val)
                rows.append((dt, price))
            except:
                continue

    if not rows:
        raise RuntimeError(f"Nincs adat: {path}")

    # 🔥 IDŐREND JAVÍTÁS
    rows.sort(key=lambda x: x[0])

    # 🔥 DUPLIKÁTUM KISZŰRÉS
    dedup = {}
    for ts, price in rows:
        dedup[ts] = price

    rows = [(ts, dedup[ts]) for ts in sorted(dedup.keys())]

    return rows

def ma_value(prices, idx, period):
    if idx + 1 < period:
        return None
    window = prices[idx - period + 1: idx + 1]
    return sum(window) / len(window)

def run_sim(rows, p):
    prices = [x[1] for x in rows]

    cash = p["initial_capital"]
    qty = 0.0
    in_position = False

    base = None
    peak = None
    trough = None

    actions = Counter()
    rules = Counter()

    max_eq = None
    max_dd = 0.0
    consecutive_rising = 0
    consecutive_falling = 0

    for idx, (ts, price) in enumerate(rows):
        prev_last = rows[idx - 1][1] if idx > 0 else price
        rising = price > prev_last
        falling = price < prev_last

        if rising:
            consecutive_rising += 1
            consecutive_falling = 0
        elif falling:
            consecutive_falling += 1
            consecutive_rising = 0

        mav = ma_value(prices, idx, p["ma_period"])
        ma_ok = True
        if p["ma_filter_enabled"]:
            ma_ok = (mav is not None and price >= mav)

        if not in_position:
            if trough is None:
                trough = price
            trough = min(trough, price)

            std_buy_price = trough * (1 + p["std_buy_pct"] / 100.0)
            panic_buy_price = trough * (1 + p["panic_buy_pct"] / 100.0)

            do_buy = False
            buy_rule = None

            if p["std_buy_enabled"]:
                if rising and ma_ok and price >= std_buy_price and (
                    (not p["panic_buy_enabled"]) or (price < panic_buy_price)
                ):
                    do_buy = True
                    buy_rule = "STANDARD_BUY"

            if (not do_buy) and p["panic_buy_enabled"]:
                if rising and ma_ok and price >= panic_buy_price and consecutive_rising >= p["panic_buy_confirm_ticks"]:
                    do_buy = True
                    buy_rule = "PANIC_BUY"

            if do_buy:
                exec_price = price * (1 + p["slippage_pct"] / 100.0)
                fee_mult = 1 - (p["fee_pct"] / 100.0)
                qty = (cash / exec_price) * fee_mult
                cash = 0.0
                in_position = True
                base = exec_price
                peak = exec_price
                trough = exec_price
                actions["BUY"] += 1
                rules[buy_rule] += 1

        else:
            peak = max(peak, price)

            std_sell_price = peak * (1 - p["std_sell_pct"] / 100.0)
            panic_sell_price = base * (1 - p["panic_sell_pct"] / 100.0)

            do_sell = False
            sell_rule = None

            if p["std_sell_enabled"]:
                if falling and std_sell_price > base and price <= std_sell_price:
                    do_sell = True
                    sell_rule = "STANDARD_SELL"

            if (not do_sell) and p["panic_sell_enabled"]:
                if falling and panic_sell_price < base and price <= panic_sell_price:
                    do_sell = True
                    sell_rule = "PANIC_SELL"

            if do_sell:
                exec_price = price * (1 - p["slippage_pct"] / 100.0)
                gross_cash = qty * exec_price
                cash = gross_cash * (1 - p["fee_pct"] / 100.0)
                qty = 0.0
                in_position = False
                trough = exec_price
                base = exec_price
                peak = exec_price
                actions["SELL"] += 1
                rules[sell_rule] += 1

        equity = cash if not in_position else qty * price
        if max_eq is None or equity > max_eq:
            max_eq = equity
        dd = ((max_eq - equity) / max_eq) * 100.0 if max_eq else 0.0
        if dd > max_dd:
            max_dd = dd

    final_equity = cash if not in_position else qty * rows[-1][1]
    pnl_pct = ((final_equity / p["initial_capital"]) - 1.0) * 100.0

    return {
        "rows": len(rows),
        "start": rows[0],
        "end": rows[-1],
        "final_equity": final_equity,
        "pnl_pct": pnl_pct,
        "max_dd_pct": max_dd,
        "in_position_at_end": in_position,
        "actions": dict(actions),
        "rules": dict(rules),
        "last_base": base,
        "last_peak": peak,
        "last_trough": trough,
    }

def main(argv):
    params = parse_args(argv)
    rows = load_prices(CSV_PATH)
    result = run_sim(rows, params)

    print("USING_FILE=", CSV_PATH)
    print("ROWS=", result["rows"])
    print("START=", result["start"])
    print("END=", result["end"])
    print("FINAL_EQUITY=", round(result["final_equity"], 6))
    print("PNL_PCT=", round(result["pnl_pct"], 6))
    print("MAX_DD_PCT=", round(result["max_dd_pct"], 6))
    print("IN_POSITION_AT_END=", result["in_position_at_end"])
    print("ACTIONS=", result["actions"])
    print("RULES=", result["rules"])
    print("LAST_BASE=", round(result["last_base"], 6) if result["last_base"] is not None else None)
    print("LAST_PEAK=", round(result["last_peak"], 6) if result["last_peak"] is not None else None)
    print("LAST_TROUGH=", round(result["last_trough"], 6) if result["last_trough"] is not None else None)

if __name__ == "__main__":
    main(sys.argv)
