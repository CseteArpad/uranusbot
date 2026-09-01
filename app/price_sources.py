from __future__ import annotations

import os
from typing import Dict, Optional

# =========================
#  Uranus - Price Sources
#  FÁZIS 7.5.3b: exchange-layer FACADE
#
#  A publikus API változatlan:
#    - get_tick(pair) -> dict {pair, symbol, price, ts, ts_utc, source}
#    - get_current_price(pair) -> float
#    - get_price(pair) -> float (legacy alias)
#
#  Belül az app/exchange adapter-réteg dolgozik:
#    get_exchange_adapter(name).get_ticker(pair)
#
#  Exchange választás:
#    1. explicit `exchange=` argumentum
#    2. URANUS_EXCHANGE vagy EXCHANGE_NAME környezeti változó
#    3. default: DEFAULT_EXCHANGE
#
#  Hiba esetén ExchangeError / TickerFetchError propagál (nincs csendes nyelés).
#
#  2026-09-01 (tulajdonosi döntés, OKX_SPOT_ONLY): az alapértelmezett ár-forrás
#  "binance" -> "okx". Indok: ez a modul PIACI ADATOT szolgáltat, nem hajt végre
#  ordert, ezért a Binance-tiltás közvetlenül nem érinti – de az alapértelmezés
#  ne mutasson más tőzsdére, mint ahol a bot kereskedik. Egy OKX-en kereskedő
#  bot Binance-árat használó alapértelmezése csendes bázis-eltérést okozna.
#  A Binance továbbra is explicit kérhető (`exchange="binance"`) történeti és
#  kutatási célra: BINANCE_AS_RESEARCH_PROVENANCE = ALLOWED.
# =========================

try:  # package import (repo gyökér a sys.path-on, pl. pytest)
    from app.exchange.manager import get_exchange_adapter
except ImportError:  # flat import mód (app/ a sys.path-on, legacy futtatás)
    from exchange.manager import get_exchange_adapter  # type: ignore

DEFAULT_EXCHANGE = "okx"


def _resolve_exchange_name(exchange: Optional[str] = None) -> str:
    return (
        exchange
        or os.getenv("URANUS_EXCHANGE")
        or os.getenv("EXCHANGE_NAME")
        or DEFAULT_EXCHANGE
    )


def get_tick(pair: str, exchange: Optional[str] = None) -> Dict[str, object]:
    """
    CANONICAL API (Uranus):
      get_tick(pair) -> dict:
        {
          "pair": "XRP/USDC",
          "symbol": "XRPUSDC" | "XRP-USDC",
          "price": 123.45,
          "ts": 1730000000,
          "ts_utc": "2026-02-01T20:48:00Z",
          "source": "binance" | "okx"
        }
    """
    adapter = get_exchange_adapter(_resolve_exchange_name(exchange))
    return adapter.get_ticker(pair)


def get_current_price(pair: str, exchange: Optional[str] = None) -> float:
    """
    REQUIRED STABLE API (legacy):
      get_current_price(pair) -> float
    """
    return float(get_tick(pair, exchange=exchange)["price"])


# Backward-compatible alias (some older modules may call this)
def get_price(pair: str, exchange: Optional[str] = None) -> float:
    return get_current_price(pair, exchange=exchange)
