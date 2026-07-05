"""
DEPRECATED (FÁZIS 7.5.3b): backward-compatible shim a price_sources facade fölé.

Ez a modul már NEM tartalmaz közvetlen Binance HTTP logikát – minden hívás a
price_sources -> app/exchange BinanceAdapter útvonalon megy. Új kód ne ezt
használja, hanem a price_sources.get_tick()-et vagy közvetlenül az adaptert.

Megtartott publikus API:
  - fetch_last_price(pair) -> ok/error dict (nem dob kivételt)
  - get_current_price(pair) -> float (hibánál RuntimeError, örökölt viselkedés)
  - get_price(pair) -> float
"""
from __future__ import annotations

from typing import Dict, Optional

try:  # package import (repo gyökér a sys.path-on, pl. pytest)
    from app import price_sources
    from app.exchange import pair_utils
    from app.exchange.exceptions import ExchangeError
except ImportError:  # flat import mód (app/ a sys.path-on, legacy futtatás)
    import price_sources  # type: ignore
    from exchange import pair_utils  # type: ignore
    from exchange.exceptions import ExchangeError  # type: ignore

SOURCE_NAME = "binance_spot_rest"


def fetch_last_price(pair: str, timeout_s: float = 3.5, retries: int = 2) -> Dict:
    """
    Örökölt, kivételt nem dobó ticker API.

    A timeout_s/retries paraméterek csak szignatúra-kompatibilitás miatt
    maradtak meg; a kérés paramétereit már az exchange adapter kezeli.

    Returns:
      siker: {"ok": True,  "pair", "symbol", "last", "source",
              "tick_ts_utc", "error": None, "http_status": None}
      hiba:  {"ok": False, "pair", "symbol", "last": None, "source",
              "tick_ts_utc": None, "error": "...", "http_status": None}
    """
    symbol: Optional[str] = None
    try:
        symbol = pair_utils.to_binance_symbol(pair)
    except ExchangeError:
        pass  # a hibát a get_tick fogja értelmes üzenettel jelezni

    try:
        tick = price_sources.get_tick(pair, exchange="binance")
        return {
            "ok": True,
            "pair": tick["pair"],
            "symbol": tick["symbol"],
            "last": float(tick["price"]),
            "source": SOURCE_NAME,
            "tick_ts_utc": tick["ts_utc"],
            "error": None,
            "http_status": None,
        }
    except ExchangeError as e:
        return {
            "ok": False,
            "pair": pair,
            "symbol": symbol,
            "last": None,
            "source": SOURCE_NAME,
            "tick_ts_utc": None,
            "error": f"{type(e).__name__}: {e}",
            "http_status": None,
        }


def get_current_price(pair: str) -> float:
    out = fetch_last_price(pair)
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "price fetch failed")
    return float(out["last"])


def get_price(pair: str) -> float:
    return get_current_price(pair)
