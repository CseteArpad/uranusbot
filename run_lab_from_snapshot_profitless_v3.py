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

    # jelenlegi legjobb referencia-környék
    "std_sell_pct": 0.95,
    "std_buy_pct": 0.795,
    "panic_sell_pct": 0.03,
    "panic_buy_pct": 1.495,

    # kanonikus profitless szintek
    "profitless_sell_mult": 1.001,
    "profitless_buy_mult": 0.999,

    # kanonikus not_enough_rise feltétel
    "not_enough_rise_peak_mult": 1.03,

    "panic_buy_confirm_ticks": 1,
    "ma_filter_enabled": 1,
    "ma_period": 20,

    # bizonyított guard
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
            rows.append((ts, float(val)))
    if not rows:
        raise RuntimeError(f"Nincs feldolgozható adat a fájlban: {path}")
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

    panic_reentry_guard_active = False
    last_panic_sell_base = None
    last_panic_sell_price = None

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
            profitless_buy_price = base * p["profitless_buy_mult"] if base is not None else None
            panic_buy_price = trough * (1 + p["panic_buy_pct"] / 100.0)

            allow_profitless_buy = True

            std_buy_allowed_by_panic_guard = True
            if p["panic_reentry_guard_enabled"] and panic_reentry_guard_active:
                panic_loss = 0.0
                if last_panic_sell_base is not None and last_panic_sell_price is not None:
                    panic_loss = max(0.0, last_panic_sell_base - last_panic_sell_price)

                fee_buffer = (
                    last_panic_sell_price * (p["panic_reentry_fee_buffer_pct"] / 100.0)
                    if last_panic_sell_price is not None else 0.0
                )
                required_extra_drop = panic_loss + fee_buffer

                if last_panic_sell_price is not None:
                    required_trough = last_panic_sell_price - required_extra_drop
                    std_buy_allowed_by_panic_guard = trough <= required_trough

            do_buy = False
            buy_rule = None

            # 6) PANIC BUY
            if p["panic_buy_enabled"]:
                if rising and ma_ok and price >= panic_buy_price and consecutive_rising >= p["panic_buy_confirm_ticks"]:
                    do_buy = True
                    buy_rule = "PANIC_BUY"

            # 5) PROFITLESS BUY
            if (not do_buy) and p["profitless_buy_enabled"] and profitless_buy_price is not None:
                if (
                    allow_profitless_buy
                    and prev_last < profitless_buy_price
                    and price >= profitless_buy_price
                ):
                    do_buy = True
                    buy_rule = "PROFITLESS_BUY"

            # 4) STANDARD BUY
            if (not do_buy) and p["std_buy_enabled"]:
                if (
                    rising
                    and ma_ok
                    and std_buy_allowed_by_panic_guard
                    and price >= std_buy_price
                    and (profitless_buy_price is None or std_buy_price < profitless_buy_price)
                    and std_buy_price < panic_buy_price
                ):
                    do_buy = True
                    buy_rule = "STANDARD_BUY"

            if do_buy:
                exec_price = price * (1 + p["slippage_pct"] / 100.0)
                fee_mult = 1 - (p["fee_pct"] / 100.0)
                qty = (cash / exec_price) * fee_mult
                cash = 0.0
                in_position = True
                base = exec_price
                peak = exec_price
                trough = exec_price

                panic_reentry_guard_active = False
                last_panic_sell_base = None
                last_panic_sell_price = None

                actions["BUY"] += 1
                rules[buy_rule] += 1

        else:
            peak = max(peak, price)

            std_sell_price = peak * (1 - p["std_sell_pct"] / 100.0)
            profitless_sell_price = base * p["profitless_sell_mult"] if base is not None else None
            panic_sell_price = base * (1 - p["panic_sell_pct"] / 100.0)

            not_enough_rise = peak < (base * p["not_enough_rise_peak_mult"])

            do_sell = False
            sell_rule = None
            sell_base_before = base

            # 3) PANIC SELL
            if p["panic_sell_enabled"]:
                if falling and panic_sell_price < base and price <= panic_sell_price:
                    do_sell = True
                    sell_rule = "PANIC_SELL"

            # 2) PROFITLESS SELL
            if (not do_sell) and p["profitless_sell_enabled"] and profitless_sell_price is not None:
                if (
                    not_enough_rise
                    and prev_last > profitless_sell_price
                    and price <= profitless_sell_price
                    and profitless_sell_price > base
                    and profitless_sell_price < std_sell_price
                ):
                    do_sell = True
                    sell_rule = "PROFITLESS_SELL"

            # 1) STANDARD SELL
            if (not do_sell) and p["std_sell_enabled"]:
                if (
                    falling
                    and price <= std_sell_price
                    and std_sell_price > base
                    and (profitless_sell_price is None or std_sell_price > profitless_sell_price)
                ):
                    do_sell = True
                    sell_rule = "STANDARD_SELL"

            if do_sell:
                exec_price = price * (1 - p["slippage_pct"] / 100.0)
                gross_cash = qty * exec_price
                cash = gross_cash * (1 - p["fee_pct"] / 100.0)
                qty = 0.0
                in_position = False
                trough = exec_price
                base = exec_price
                peak = exec_price

                if sell_rule == "PANIC_SELL":
                    panic_reentry_guard_active = True
                    last_panic_sell_base = sell_base_before
                    last_panic_sell_price = exec_price
                else:
                    panic_reentry_guard_active = False
                    last_panic_sell_base = None
                    last_panic_sell_price = None

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
