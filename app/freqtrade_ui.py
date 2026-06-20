from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

FT_URL = os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")
FT_CFG_PATH = os.getenv("URANUS_FT_CONFIG", "/opt/bots/uranus/freqtrade/user_data/config.json")

_TIMEOUT = 6
_CACHE_TTL = 3.0
_cache: Dict[str, Any] = {"ts": 0.0, "data": None}


def _read_cfg() -> Dict[str, Any]:
    try:
        with open(FT_CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _read_creds() -> Tuple[Optional[str], Optional[str]]:
    cfg = _read_cfg()
    api = cfg.get("api_server") if isinstance(cfg, dict) else None
    if not isinstance(api, dict):
        return (None, None)
    u = api.get("username")
    p = api.get("password")
    return (str(u) if u else None, str(p) if p else None)


def _get(path: str, auth: Optional[Tuple[str, str]]) -> Tuple[int, Any]:
    url = f"{FT_URL}{path}"
    try:
        r = requests.get(url, auth=auth, timeout=_TIMEOUT)
        sc = int(r.status_code)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return (sc, body)
    except Exception as e:
        return (0, {"error": f"exception:{type(e).__name__}"})


def snapshot(force: bool = False) -> Dict[str, Any]:
    """
    UI snapshot:
      - ping (no auth)
      - count/status/show_config/profit (basic auth from config.json)
    Returns a dict safe to embed into UI payload: data["ft"].
    """
    now = time.time()
    if (not force) and _cache["data"] is not None and (now - float(_cache["ts"])) < _CACHE_TTL:
        return dict(_cache["data"])

    out: Dict[str, Any] = {
        "ts": now,
        "url": FT_URL,
        "ok": False,
        "auth_ok": False,
        "error": "",
        "ping": None,
        "count": None,
        "status": None,
        "show_config": None,
        "profit": None,
        "open_trades_count": None,
    }

    sc_ping, ping = _get("/api/v1/ping", auth=None)
    out["ping"] = {"status_code": sc_ping, "body": ping}

    u, p = _read_creds()
    auth = (u, p) if (u and p) else None

    if auth is None:
        out["error"] = "creds_missing"
        _cache["ts"] = now
        _cache["data"] = dict(out)
        return out

    sc_c, body_c = _get("/api/v1/count", auth=auth)
    out["count"] = {"status_code": sc_c, "body": body_c}

    sc_s, body_s = _get("/api/v1/status", auth=auth)
    out["status"] = {"status_code": sc_s, "body": body_s}

    sc_cfg, body_cfg = _get("/api/v1/show_config", auth=auth)
    out["show_config"] = {"status_code": sc_cfg, "body": body_cfg}

    sc_p, body_p = _get("/api/v1/profit", auth=auth)
    out["profit"] = {"status_code": sc_p, "body": body_p}

    # auth_ok / ok logic
    unauthorized = False
    for k in ("count", "status", "show_config", "profit"):
        b = (out.get(k) or {}).get("body")
        if isinstance(b, dict) and b.get("detail") == "Unauthorized":
            unauthorized = True

    out["auth_ok"] = (not unauthorized) and sc_c == 200
    out["ok"] = out["auth_ok"] and sc_cfg == 200

    # open trades count
    st_body = (out.get("status") or {}).get("body")
    if isinstance(st_body, list):
        out["open_trades_count"] = int(len(st_body))
    elif isinstance(st_body, dict) and "trades" in st_body and isinstance(st_body["trades"], list):
        out["open_trades_count"] = int(len(st_body["trades"]))

    if unauthorized:
        out["error"] = "unauthorized"
    elif out["ok"]:
        out["error"] = ""
    else:
        out["error"] = "api_error"

    _cache["ts"] = now
    _cache["data"] = dict(out)
    return out
