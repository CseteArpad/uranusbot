"""
Uranus Exchange Layer – OKX adapter (SKELETON).

FÁZIS 7.5.2: hálózati logika NINCS, éles kapcsolat NINCS.
Freqtrade oldalon az exchange azonosító "myokx" – a manager mindkét
nevet ("okx", "myokx") erre az adapterre oldja fel.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import pair_utils
from .base import ExchangeAdapter
from .capabilities import ExchangeCapabilities

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

    # -- hálózati műveletek: skeleton -------------------------------------

    def get_ticker(self, pair: str) -> Dict[str, Any]:
        raise NotImplementedError(_SKELETON_MSG.format(method="get_ticker"))

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
