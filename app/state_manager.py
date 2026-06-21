"""
State Manager — loads and saves state.json for the Range Lab.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state.json")


def load_state(path: str = STATE_PATH) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return _default_state()


def save_state(state: dict, path: str = STATE_PATH) -> None:
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _default_state() -> dict:
    return {
        "schema_version": 1,
        "last_updated": None,
        "last_run": None,
        "results": {},
    }


def record_run_result(state: dict, symbol: str, timeframe: str, summary: dict) -> None:
    key = f"{symbol}_{timeframe}"
    state.setdefault("results", {})[key] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "run_at": datetime.now(timezone.utc).isoformat(),
        **summary,
    }
    state["last_run"] = datetime.now(timezone.utc).isoformat()
