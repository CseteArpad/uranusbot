from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests


BINANCE_BASE = "https://api.binance.com"
UA = "UranusBot/1.0 (+binance spot rest)"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_to_symbol(pair: str) -> str:
    """
    UI/State: 'XRP/USDC' -> Binance: 'XRPUSDC'
    """
    pair = (pair or "").strip().upper()
    if "/" in pair:
        a, b = pair.split("/", 1)
        return f"{a}{b}"
    return pair


def fetch_last_price(pair: str, timeout_s: float = 3.5, retries: int = 2) -> Dict:
    """
    Returns:
      {
        "ok": bool,
        "pair": "XRP/USDC",
        "symbol": "XRPUSDC",
        "last": float|None,
        "source": "binance_spot_rest",
        "tick_ts_utc": "ISO",
        "error": str|None,
        "http_status": int|None,
      }
    """
    symbol = _pair_to_symbol(pair)
    url = f"{BINANCE_BASE}/api/v3/ticker/price"
    params = {"symbol": symbol}

    last_err: Optional[str] = None
    last_status: Optional[int] = None

    for _ in range(max(1, retries)):
        try:
            r = requests.get(url, params=params, timeout=timeout_s, headers={"User-Agent": UA})
            last_status = r.status_code
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep(0.15)
                continue

            j = r.json()
            p = float(j["price"])
            return {
                "ok": True,
                "pair": pair,
                "symbol": symbol,
                "last": p,
                "source": "binance_spot_rest",
                "tick_ts_utc": _utc_iso_now(),
                "error": None,
                "http_status": r.status_code,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.15)

    return {
        "ok": False,
        "pair": pair,
        "symbol": symbol,
        "last": None,
        "source": "binance_spot_rest",
        "tick_ts_utc": _utc_iso_now(),
        "error": last_err or "unknown error",
        "http_status": last_status,
    }


def get_current_price(pair: str) -> float:
    out = fetch_last_price(pair)
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "price fetch failed")
    return float(out["last"])


def get_price(pair: str) -> float:
    return get_current_price(pair)
