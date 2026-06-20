# /opt/bots/uranus/app/ft_ui.py
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests


FT_BASE = os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")
FT_CFG_PATH = os.getenv("URANUS_FT_CONFIG", "/opt/bots/uranus/freqtrade/user_data/config.json")

_TIMEOUT = 6
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def _read_creds() -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(FT_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api = cfg.get("api_server") or {}
        u = api.get("username")
        p = api.get("password")
        return (u, p)
    except Exception:
        return (None, None)


def _get(path: str) -> Tuple[int, Any]:
    url = f"{FT_BASE}{path}"
    u, p = _read_creds()
    auth = (u, p) if u and p else None
    r = requests.get(url, auth=auth, timeout=_TIMEOUT)
    try:
        body = r.json()
    except Exception:
        body = r.text
    return (r.status_code, body)


def snapshot(throttle_sec: int = 2) -> Dict[str, Any]:
    now = time.time()
    if _cache["data"] is not None and (now - float(_cache["ts"])) < throttle_sec:
        return _cache["data"]

    out: Dict[str, Any] = {
        "ok": False,
        "base": FT_BASE,
        "error": None,
        "ping": {"status_code": None, "body": None},
        "count": {"status_code": None, "body": None},
        "status": {"status_code": None, "body": None},
        "show_config": {"status_code": None, "body": None},
        "profit": {"status_code": None, "body": None},
    }

    try:
        sc, body = _get("/api/v1/ping")
        out["ping"] = {"status_code": sc, "body": body}

        sc, body = _get("/api/v1/count")
        out["count"] = {"status_code": sc, "body": body}

        sc, body = _get("/api/v1/status")
        out["status"] = {"status_code": sc, "body": body}

        sc, body = _get("/api/v1/show_config")
        out["show_config"] = {"status_code": sc, "body": body}

        sc, body = _get("/api/v1/profit")
        out["profit"] = {"status_code": sc, "body": body}

        # OK feltétel: ping pong + count/status/show_config nem 401
        pong = isinstance(out["ping"]["body"], dict) and out["ping"]["body"].get("status") == "pong"
        unauthorized = any(x.get("status_code") == 401 for x in [out["count"], out["status"], out["show_config"], out["profit"]])
        out["ok"] = bool(pong and not unauthorized)
        if unauthorized:
            out["error"] = "unauthorized"
    except Exception as e:
        out["error"] = f"exception:{type(e).__name__}"

    _cache["ts"] = now
    _cache["data"] = out
    return out
