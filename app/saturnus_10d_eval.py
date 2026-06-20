#!/usr/bin/env python3
import csv
import json
import math
import re
from pathlib import Path
from datetime import datetime, timezone

STATE_PATH = Path("/opt/bots/uranus/state.json")
PRICES_CSV = Path("/opt/bots/uranus_monitor/data/prices.csv")
EVENTS_CSV = Path("/opt/bots/uranus_monitor/data/events.csv")
TRADE_EVENTS_CSV = Path("/opt/bots/uranus_monitor/data/trade_events.csv")
RUNNER_LOG = Path("/opt/bots/uranus/logs/runner.log")

INITIAL_EQUITY = 100.0


def read_csv(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def max_drawdown(values):
    peak = None
    max_dd = 0.0
    for v in values:
        if v is None:
            continue
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


def tail_lines(path, n=5000):
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def main():
    print("=== URANUS 10 NAPOS KIÉRTÉKELÉS ===")
    print("Futtatás ideje:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print()

    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print("STATE HIBA:", e)

    prices = read_csv(PRICES_CSV)
    events = read_csv(EVENTS_CSV)
    trade_events = read_csv(TRADE_EVENTS_CSV)

    print("=== 1. AKTUÁLIS ÁLLAPOT ===")
    print("in_position =", state.get("in_position"))
    print("last =", state.get("last"))
    print("base =", state.get("base"))
    print("peak =", state.get("peak"))
    print("trough =", state.get("trough"))

    decision = state.get("last_decision") or state.get("decision") or {}
    print("decision =", decision.get("decision_action") or decision.get("action"))
    print("reason =", decision.get("decision_reason") or decision.get("reason"))
    print("panic_context =", decision.get("panic_context") or state.get("panic_context"))
    print("recovery_context =", decision.get("recovery_context") or state.get("recovery_context"))
    print()

    print("=== 2. ADATMENNYISÉG ===")
    print("prices.csv sorok =", len(prices))
    print("events.csv sorok =", len(events))
    print("trade_events.csv sorok =", len(trade_events))
    print()

    print("=== 3. TRADE-EK ===")
    all_trade_rows = []
    for r in trade_events:
        text = " ".join(str(x) for x in r.values()).upper()
        if "BUY" in text or "SELL" in text:
            all_trade_rows.append(r)

    if not all_trade_rows:
        for r in events:
            text = " ".join(str(x) for x in r.values()).upper()
            if "BUY" in text or "SELL" in text:
                all_trade_rows.append(r)

    print("trade sorok =", len(all_trade_rows))

    panic_rows = []
    for r in all_trade_rows:
        text = " ".join(str(x) for x in r.values()).upper()
        if "PANIC" in text:
            panic_rows.append(r)

    print("panic trade sorok =", len(panic_rows))
    if panic_rows:
        print("PANIC TALÁLATOK:")
        for r in panic_rows[-10:]:
            print(r)
    print()

    print("=== 4. PROFIT / DRAWDOWN BECSLÉS ===")
    equity_values = []
    for r in events + trade_events:
        for key in ("equity", "equity_after", "balance", "total_equity"):
            if key in r:
                val = to_float(r.get(key))
                if val is not None:
                    equity_values.append(val)

    if equity_values:
        final_equity = equity_values[-1]
        profit_pct = (final_equity - INITIAL_EQUITY) / INITIAL_EQUITY * 100.0
        dd = max_drawdown(equity_values)
        print("final_equity =", round(final_equity, 6))
        print("profit_pct =", round(profit_pct, 4), "%")
        print("max_drawdown_pct =", round(dd, 4), "%")
    else:
        print("Nincs elég equity adat events/trade_events fájlban.")
        print("Aktuális állapot alapján profitot nem számolok automatikusan.")
    print()

    print("=== 5. RUNNER LOG HIBÁK ===")
    lines = tail_lines(RUNNER_LOG, 10000)
    error_patterns = [
        "ERROR", "Traceback", "Exception", "API_UNAVAILABLE",
        "EXEC_ERROR", "ORDER_FAILED", "FAILED", "insufficient",
        "Invalid", "timeout", "ConnectionError"
    ]

    hits = []
    for line in lines:
        up = line.upper()
        if any(p.upper() in up for p in error_patterns):
            hits.append(line)

    print("vizsgált log sorok =", len(lines))
    print("hiba/figyelmeztetés találatok =", len(hits))
    for line in hits[-30:]:
        print(line)
    print()

    print("=== 6. PÁNIKSZABÁLY ELLENŐRZÉS ===")
    raw = decision.get("raw") or decision.get("engine_raw") or {}
    debug = raw.get("debug") or decision.get("data", {}).get("debug") or []
    if debug:
        for d in debug:
            if "PANIC" in str(d).upper() or "ma_trend_state" in str(d):
                print(d)
    else:
        print("Nincs debug adat a state-ben.")
    print()

    print("=== 7. MINŐSÍTÉS ===")
    fail = False
    warn = False

    if hits:
        warn = True
    if panic_rows:
        warn = True

    if equity_values:
        if profit_pct < 0:
            fail = True
        if dd > 5:
            warn = True

    if fail:
        print("EREDMÉNY: NEM ÉLESÍTHETŐ nagyobb tőkével.")
    elif warn:
        print("EREDMÉNY: ÓVATOSAN FIGYELENDŐ, kézi ellenőrzés szükséges.")
    else:
        print("EREDMÉNY: RENDBEN, a rendszer stabilnak tűnik.")

    print()
    print("Ajánlás:")
    print("- 10 nap alatt ne módosíts paramétert.")
    print("- Ha PANIC trade történik, ellenőrizni kell: trend_state, price gate, reason.")
    print("- Ha hiba jelenik meg a logban, előbb javítás, csak utána további élesítés.")


if __name__ == "__main__":
    main()
