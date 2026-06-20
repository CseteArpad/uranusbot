from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests


# =========================
#  Uranus - Price Sources
#  Canonical: Binance public ticker
#  Stable APIs:
#    - get_tick(pair) -> dict {pair, symbol, price, ts, ts_utc, source}
#    - get_current_price(pair) -> float
#  Back-compat:
#    - get_price(pair) -> float
# =========================

BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_TIMEOUT = 10
PRICE_CACHE_SECONDS = 2.0

SOURCE_NAME = "binance"


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_pair(pair: str) -> Tuple[str, str]:
    """
    Accepts:
      - "XRP/USDC"  -> ("XRP/USDC", "XRPUSDC")
      - "XRPUSDC"   -> ("XRP/USDC", "XRPUSDC")  (best-effort)
    Returns:
      (pair_slash, symbol_concat)
    """
    if not pair or not isinstance(pair, str):
        raise ValueError("pair must be a non-empty string")

    p = pair.strip().upper()

    if "/" in p:
        base, quote = p.split("/", 1)
        base = base.strip()
        quote = quote.strip()
        if not base or not quote:
            raise ValueError(f"invalid pair format: {pair!r}")
        return f"{base}/{quote}", f"{base}{quote}"

    # best-effort for already concatenated symbol
    # NOTE: if you ever use unusual quotes, prefer the slash format in config/state.
    common_quotes = ["USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB", "TRY", "EUR"]
    for q in common_quotes:
        if p.endswith(q) and len(p) > len(q):
            base = p[: -len(q)]
            return f"{base}/{q}", p

    # fallback: keep as-is
    return p, p


def _binance_public_ticker_price(symbol: str) -> float:
    """
    Binance public REST:
      GET /api/v3/ticker/price?symbol=XRPUSDC
    """
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
    r = requests.get(url, params={"symbol": symbol}, timeout=BINANCE_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    # expected: {"symbol":"XRPUSDC","price":"123.45"}
    price_s = j.get("price")
    if price_s is None:
        raise RuntimeError(f"binance ticker response missing price: {j!r}")
    try:
        return float(price_s)
    except Exception as e:
        raise RuntimeError(f"binance price not float: {price_s!r} ({e})") from e


# Very small in-process cache (helps if UI / runner call frequently)
_last_symbol: Optional[str] = None
_last_price: Optional[float] = None
_last_ts: float = 0.0


def get_tick(pair: str) -> Dict[str, object]:
    """
    CANONICAL API (Uranus):
      get_tick(pair) -> dict:
        {
          "pair": "XRP/USDC",
          "symbol": "XRPUSDC",
          "price": 123.45,
          "ts": 1730000000,
          "ts_utc": "2026-02-01T20:48:00Z",
          "source": "binance"
        }
    """
    global _last_symbol, _last_price, _last_ts

    pair_slash, symbol = _normalize_pair(pair)
    now = time.time()

    # cache hit
    if _last_symbol == symbol and _last_price is not None and (now - _last_ts) < PRICE_CACHE_SECONDS:
        price = float(_last_price)
        return {
            "pair": pair_slash,
            "symbol": symbol,
            "price": price,
            "ts": int(_last_ts),
            "ts_utc": _utc_iso(_last_ts),
            "source": SOURCE_NAME,
        }

    price = _binance_public_ticker_price(symbol)

    _last_symbol = symbol
    _last_price = float(price)
    _last_ts = now

    return {
        "pair": pair_slash,
        "symbol": symbol,
        "price": float(price),
        "ts": int(now),
        "ts_utc": _utc_iso(now),
        "source": SOURCE_NAME,
    }


def get_current_price(pair: str) -> float:
    """
    REQUIRED STABLE API (legacy):
      get_current_price(pair) -> float

    Uses Binance PUBLIC REST only. No Freqtrade dependency.
    """
    tick = get_tick(pair)
    return float(tick["price"])


# Backward-compatible aliases (some older modules may call these)
def get_price(pair: str) -> float:
    return get_current_price(pair)
