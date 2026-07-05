"""
Uranus Exchange Layer – OKX adapter (SKELETON).

FÁZIS 7.5.2: hálózati logika NINCS, éles kapcsolat NINCS.
Freqtrade oldalon az exchange azonosító "myokx" – a manager mindkét
nevet ("okx", "myokx") erre az adapterre oldja fel.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from . import pair_utils
from .base import ExchangeAdapter
from .capabilities import ExchangeCapabilities
from .exceptions import TickerFetchError

OKX_BASE_URL = "https://www.okx.com"
REQUEST_TIMEOUT = 10

_SKELETON_MSG = "OKXAdapter.{method} not implemented yet (FÁZIS 7.5.3)"


class OKXAdapter(ExchangeAdapter):
    name = "okx"

    CAPABILITIES = ExchangeCapabilities(
        supports_spot=True,
        supports_margin=False,
        supports_public_ohlcv=True,
        supports_balance=True,
        supports_market_orders=True,
        supports_limit_orders=True,
        supports_dust_conversion=False,
    )

    @property
    def capabilities(self) -> ExchangeCapabilities:
        return self.CAPABILITIES

    def to_exchange_symbol(self, pair: str) -> str:
        return pair_utils.to_okx_inst_id(pair)

    # -- implementált hálózati műveletek (FÁZIS 7.5.3a) --------------------

    def get_ticker(self, pair: str) -> Dict[str, Any]:
        """
        OKX publikus ticker:
          GET /api/v5/market/ticker?instId=XRP-USDC
        Válasz: {"code":"0","msg":"","data":[{"instId":...,"last":"0.52",...}]}
        Unit tesztben a requests.get mockolandó – élő hívás tesztből tilos.
        """
        pair_slash = self.normalize_pair(pair)
        inst_id = pair_utils.to_okx_inst_id(pair_slash)
        url = f"{OKX_BASE_URL}/api/v5/market/ticker"

        try:
            r = requests.get(url, params={"instId": inst_id}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            raise TickerFetchError(
                f"okx ticker request failed for {inst_id}: {e}"
            ) from e
        except ValueError as e:
            raise TickerFetchError(
                f"okx ticker returned non-JSON body for {inst_id}: {e}"
            ) from e

        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise TickerFetchError(
                f"okx ticker error response for {inst_id}: {str(payload)[:300]}"
            )
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise TickerFetchError(
                f"okx ticker response missing data for {inst_id}: {str(payload)[:300]}"
            )
        last_raw = data[0].get("last")
        if last_raw in (None, ""):
            raise TickerFetchError(
                f"okx ticker response missing last price for {inst_id}: {data[0]!r}"
            )
        try:
            price = float(last_raw)
        except (TypeError, ValueError) as e:
            raise TickerFetchError(
                f"okx ticker last price not a number for {inst_id}: {last_raw!r}"
            ) from e

        return self._build_ticker(pair_slash, inst_id, price)

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
