"""
Uranus Exchange Layer – absztrakt adapter interfész.

FÁZIS 7.5.2: az adapterek még skeletonok, hálózati logika NINCS.
A tényleges implementáció (ticker/ohlcv/balance/order) FÁZIS 7.5.3+.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from . import pair_utils
from .capabilities import ExchangeCapabilities


class ExchangeAdapter(ABC):
    """
    Egységes exchange-interfész az app-réteg számára.

    Konkrét adapter: BinanceAdapter, OKXAdapter (app/exchange/binance.py, okx.py).
    """

    #: az adapter kanonikus neve, pl. "binance", "okx"
    name: str = ""

    # ------------------------------------------------------------------ #
    # Konkrét (már most működő) rész
    # ------------------------------------------------------------------ #

    def normalize_pair(self, pair: str) -> str:
        """Bármely dialektusból kanonikus 'BASE/QUOTE' formátum."""
        return pair_utils.normalize_pair(pair)

    @property
    @abstractmethod
    def capabilities(self) -> ExchangeCapabilities:
        """Az adapter képesség-deklarációja."""

    @abstractmethod
    def to_exchange_symbol(self, pair: str) -> str:
        """Kanonikus pár -> az exchange natív symbol formátuma."""

    # ------------------------------------------------------------------ #
    # Hálózati műveletek – FÁZIS 7.5.3-ig minden adapterben
    # NotImplementedError-t dobnak.
    # ------------------------------------------------------------------ #

    @abstractmethod
    def get_ticker(self, pair: str) -> Dict[str, Any]:
        """Utolsó ár: {pair, symbol, price, ts, ts_utc, source}."""

    @abstractmethod
    def get_ohlcv(
        self,
        pair: str,
        timeframe: str,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[List[float]]:
        """Publikus OHLCV gyertyák."""

    @abstractmethod
    def get_balances(self) -> Dict[str, Dict[str, float]]:
        """Spot balance-ok: {asset: {free, locked, total}}."""

    @abstractmethod
    def create_order(
        self,
        pair: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Order létrehozás. Skeleton fázisban TILOS implementálni."""

    @abstractmethod
    def cancel_order(self, order_id: str, pair: Optional[str] = None) -> Dict[str, Any]:
        """Order visszavonás."""

    @abstractmethod
    def convert_dust(self, assets: List[str]) -> Dict[str, Any]:
        """Dust-konverzió (csak ha capabilities.supports_dust_conversion)."""

    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"
