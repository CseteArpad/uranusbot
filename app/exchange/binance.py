"""
Uranus Exchange Layer – Binance adapter (SKELETON).

FÁZIS 7.5.2: hálózati logika NINCS, éles kapcsolat NINCS.
A meglévő Binance LIVE működést (price_sources.py, binance_wallet.py,
binance_spot_rest.py) ez a modul NEM érinti – azok átterelése FÁZIS 7.5.3.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from . import pair_utils
from .base import ExchangeAdapter
from .capabilities import ExchangeCapabilities
from .exceptions import TickerFetchError

BINANCE_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT = 10

_SKELETON_MSG = "BinanceAdapter.{method} not implemented yet (FÁZIS 7.5.3)"


class BinanceAdapter(ExchangeAdapter):
    name = "binance"

    CAPABILITIES = ExchangeCapabilities(
        supports_spot=True,
        supports_margin=False,
        supports_public_ohlcv=True,
        supports_balance=True,
        supports_market_orders=True,
        supports_limit_orders=True,
        supports_dust_conversion=True,
    )

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return self.CAPABILITIES

    def to_exchange_symbol(self, pair: str) -> str:
        return pair_utils.to_binance_symbol(pair)

    # -- implementált hálózati műveletek (FÁZIS 7.5.3a) --------------------

    def get_ticker(self, pair: str) -> Dict[str, Any]:
        """
        Binance publikus ticker:
          GET /api/v3/ticker/price?symbol=XRPUSDC
        Unit tesztben a requests.get mockolandó – élő hívás tesztből tilos.
        """
        pair_slash = self.normalize_pair(pair)
        symbol = pair_utils.to_binance_symbol(pair_slash)
        url = f"{BINANCE_BASE_URL}/api/v3/ticker/price"

        try:
            r = requests.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            raise TickerFetchError(
                f"binance ticker request failed for {symbol}: {e}"
            ) from e
        except ValueError as e:
            raise TickerFetchError(
                f"binance ticker returned non-JSON body for {symbol}: {e}"
            ) from e

        price_raw = payload.get("price") if isinstance(payload, dict) else None
        if price_raw is None:
            raise TickerFetchError(
                f"binance ticker response missing price for {symbol}: {payload!r}"
            )
        try:
            price = float(price_raw)
        except (TypeError, ValueError) as e:
            raise TickerFetchError(
                f"binance ticker price not a number for {symbol}: {price_raw!r}"
            ) from e

        return self._build_ticker(pair_slash, symbol, price)

    # -- hálózati műveletek: skeleton -------------------------------------

    def get_ohlcv(
        self,
        pair: str,
        timeframe: str,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[List[float]]:
        raise NotImplementedError(_SKELETON_MSG.format(method="get_ohlcv"))

    def get_balances(self) -> Dict[str, Dict[str, float]]:
        raise NotImplementedError(_SKELETON_MSG.format(method="get_balances"))

    def create_order(
        self,
        pair: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError(_SKELETON_MSG.format(method="create_order"))

    def cancel_order(self, order_id: str, pair: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError(_SKELETON_MSG.format(method="cancel_order"))

    def convert_dust(self, assets: List[str]) -> Dict[str, Any]:
        raise NotImplementedError(_SKELETON_MSG.format(method="convert_dust"))
