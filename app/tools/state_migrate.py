#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path

# CANONICAL default (FÁZIS 1): /opt/bots/uranus/state.json
# Env override: URANUS_STATE_JSON (kompatibilis a többi komponenssel)
STATE_PATH = Path(os.getenv("URANUS_STATE_JSON", "/opt/bots/uranus/state.json"))

def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def migrate():
    state = load_state()
    legacy = {}

    def move(key, target, target_key=None):
        if key in state:
            legacy[key] = state[key]
            target[target_key or key] = state.pop(key)

    # --- prices ---
    prices = state.setdefault("prices", {})
    move("last_price", prices)
    move("current_rate", prices)
    move("open_rate", prices)

    # --- position ---
    position = state.setdefault("position", {})
    move("in_position", position)

    # --- base ---
    base = state.setdefault("base", {})
    move("base_price_current", base, "price")

    # --- meta ---
    meta = state.setdefault("meta", {})
    meta["migrated_utc"] = now_utc()

    if legacy:
        state["legacy"] = legacy

    state["updated_utc"] = now_utc()
    save_state(state)

    print("OK:: state.json migrated to canonical structure (legacy preserved)")
    print(f"OK:: path={STATE_PATH}")

if __name__ == "__main__":
    migrate()
