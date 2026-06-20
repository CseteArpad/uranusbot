#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
executor_dryrun_validation.py (CANONICAL TEST TOOL)

Purpose:
  Validate that Uranus tick_runner executor wiring is SAFE in dry-run:
    A) EXECUTION_ENABLED=0 (disabled) => must NOT touch network, executed=False
    B) EXECUTION_ENABLED=1 + EXECUTION_LOG_ONLY=1 (log-only) => must NOT touch network
       (If any network attempt happens => FAIL)

This tool monkeypatches common HTTP clients (requests/httpx/urllib) to raise immediately.
If the executor path tries to call Freqtrade REST endpoints, the test will FAIL.
"""

import os
import sys
import importlib
from types import ModuleType
from typing import Any, Dict, Tuple


# --- Ensure /opt/bots/uranus/app is importable (so "import tick_runner" works) ---
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


class NetworkAttempt(Exception):
    pass


def _patch_requests() -> int:
    applied = 0
    try:
        import requests  # type: ignore

        def _blocked(*args, **kwargs):
            raise NetworkAttempt(f"NETWORK_BLOCKED: requests call attempted args={args} kwargs={kwargs}")

        if hasattr(requests, "request"):
            requests.request = _blocked  # type: ignore
            applied += 1

        try:
            import requests.sessions  # type: ignore

            if hasattr(requests.sessions, "Session") and hasattr(requests.sessions.Session, "request"):
                requests.sessions.Session.request = _blocked  # type: ignore
                applied += 1
        except Exception:
            pass
    except Exception:
        pass

    return applied


def _patch_httpx() -> int:
    applied = 0
    try:
        import httpx  # type: ignore

        def _blocked(*args, **kwargs):
            raise NetworkAttempt(f"NETWORK_BLOCKED: httpx call attempted args={args} kwargs={kwargs}")

        for attr in ("get", "post", "put", "delete", "request"):
            if hasattr(httpx, attr):
                setattr(httpx, attr, _blocked)
                applied += 1

        for clsname in ("Client", "AsyncClient"):
            cls = getattr(httpx, clsname, None)
            if cls and hasattr(cls, "request"):
                cls.request = _blocked  # type: ignore
                applied += 1
    except Exception:
        pass
    return applied


def _patch_urllib() -> int:
    applied = 0
    try:
        import urllib.request  # type: ignore

        def _blocked(*args, **kwargs):
            raise NetworkAttempt(f"NETWORK_BLOCKED: urllib.request call attempted args={args} kwargs={kwargs}")

        if hasattr(urllib.request, "urlopen"):
            urllib.request.urlopen = _blocked  # type: ignore
            applied += 1
    except Exception:
        pass
    return applied


def patch_network() -> int:
    return _patch_requests() + _patch_httpx() + _patch_urllib()


def _reload_module(modname: str) -> ModuleType:
    if modname in sys.modules:
        return importlib.reload(sys.modules[modname])
    return importlib.import_module(modname)


def _make_min_state_and_decision(action: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    state: Dict[str, Any] = {
        "schema_version": 2,
        "pair": "XRP/USDC",
        "time": None,
        "last": 100.0,
        "prev_last": 99.0,
        "execution": {
            "enabled": (os.getenv("EXECUTION_ENABLED", "0") == "1"),
            "log_only": (os.getenv("EXECUTION_LOG_ONLY", "1") == "1"),
        },
        "freqtrade": {
            "open_trades": 0,
        },
    }

    decision: Dict[str, Any] = {
        "level": "buy" if action == "BUY" else ("sell" if action == "SELL" else "none"),
        "action": action,
        "rule": "TEST_DECISION",
        "reason": "TEST_TRIGGER",
    }
    return state, decision


def run_one_scenario(enabled: str, log_only: str) -> int:
    os.environ["EXECUTION_ENABLED"] = enabled
    os.environ["EXECUTION_LOG_ONLY"] = log_only

    patched = patch_network()

    try:
        # reload after ENV changes
        _reload_module("freqtrade_executor")
        tick_runner = _reload_module("tick_runner")
    except Exception as e:
        print(f"ERR - import/reload failed: {e}")
        return 2

    if not hasattr(tick_runner, "maybe_execute_via_api"):
        print("ERR - tick_runner.maybe_execute_via_api not found")
        return 2

    fn = getattr(tick_runner, "maybe_execute_via_api")

    print(f"\n=== SCENARIO enabled={enabled} log_only={log_only} (network_patches={patched}) ===")

    # BUY test
    try:
        st, dec = _make_min_state_and_decision("BUY")
        dec2, executed = fn(st, dec)
        print(f"OK  - BUY call returned executed={executed} action={dec2.get('action')} rule={dec2.get('rule')}")
        if enabled != "1" and executed:
            print("ERR - disabled mode executed=True (must be False)")
            return 2
    except NetworkAttempt as ne:
        print(f"ERR - network attempt detected in BUY path: {ne}")
        return 2
    except KeyError as ke:
        print(f"ERR - missing key in BUY path: {ke}")
        return 2
    except Exception as e:
        print(f"ERR - exception in BUY path: {e}")
        return 2

    # SELL test
    try:
        st, dec = _make_min_state_and_decision("SELL")
        st["freqtrade"]["open_trades"] = 1
        st["in_position"] = True
        dec2, executed = fn(st, dec)
        print(f"OK  - SELL call returned executed={executed} action={dec2.get('action')} rule={dec2.get('rule')}")
        if enabled != "1" and executed:
            print("ERR - disabled mode executed=True on SELL (must be False)")
            return 2
    except NetworkAttempt as ne:
        print(f"ERR - network attempt detected in SELL path: {ne}")
        return 2
    except KeyError as ke:
        print(f"ERR - missing key in SELL path: {ke}")
        return 2
    except Exception as e:
        print(f"ERR - exception in SELL path: {e}")
        return 2

    print("OK  - scenario PASS (no network attempts)")
    return 0


def main() -> int:
    rc_a = run_one_scenario(enabled="0", log_only="1")
    if rc_a != 0:
        return rc_a

    rc_b = run_one_scenario(enabled="1", log_only="1")
    if rc_b != 0:
        return rc_b

    print("\n=== RESULT ===")
    print("STABLE: executor dry-run validation PASS (network blocked, no attempts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
