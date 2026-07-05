"""
Uranus Exchange Layer – ExchangeManager.

Konfiguráció (exchange név) alapján választ adaptert.

Használat:
    from app.exchange.manager import ExchangeManager, get_exchange_adapter

    manager = ExchangeManager(exchange_name="binance")
    adapter = manager.get_adapter()

    adapter = get_exchange_adapter("okx")
"""
from __future__ import annotations

from typing import Dict, Type

from .base import ExchangeAdapter
from .binance import BinanceAdapter
from .exceptions import AdapterNotConfiguredError, UnsupportedExchangeError
from .okx import OKXAdapter

#: név -> adapter osztály; a "myokx" a Freqtrade-oldali OKX azonosító
_ADAPTER_REGISTRY: Dict[str, Type[ExchangeAdapter]] = {
    "binance": BinanceAdapter,
    "okx": OKXAdapter,
    "myokx": OKXAdapter,
}


def _normalize_name(exchange_name: object) -> str:
    if exchange_name is None or not str(exchange_name).strip():
        raise AdapterNotConfiguredError(
            "exchange_name is missing or empty; set it in config (e.g. 'binance', 'okx')"
        )
    return str(exchange_name).strip().lower()


class ExchangeManager:
    """
    Adapter-választó. A nevet case-insensitive módon kezeli, ismeretlen
    exchange-re UnsupportedExchangeError-t dob (már a konstruktorban).
    Az adaptert példányonként cache-eli.
    """

    def __init__(self, exchange_name: str):
        name = _normalize_name(exchange_name)
        if name not in _ADAPTER_REGISTRY:
            raise UnsupportedExchangeError(
                f"unsupported exchange: {exchange_name!r} "
                f"(supported: {sorted(set(_ADAPTER_REGISTRY))})"
            )
        self._exchange_name = name
        self._adapter: ExchangeAdapter | None = None

    @property
    def exchange_name(self) -> str:
        return self._exchange_name

    def get_adapter(self) -> ExchangeAdapter:
        if self._adapter is None:
            self._adapter = _ADAPTER_REGISTRY[self._exchange_name]()
        return self._adapter


# --------------------------------------------------------------------- #
# Modul-szintű convenience API, tesztelhető cache-sel
# --------------------------------------------------------------------- #

_adapter_cache: Dict[str, ExchangeAdapter] = {}


def get_exchange_adapter(exchange_name: str, fresh: bool = False) -> ExchangeAdapter:
    """
    Egységes belépési pont: névből adapter példány.

    Azonos névre (case-insensitive) ugyanazt a példányt adja vissza;
    fresh=True új példányt kényszerít és frissíti a cache-t.
    """
    name = _normalize_name(exchange_name)
    if fresh or name not in _adapter_cache:
        _adapter_cache[name] = ExchangeManager(name).get_adapter()
    return _adapter_cache[name]


def clear_adapter_cache() -> None:
    """Teszt-hook: a modul-szintű adapter cache ürítése."""
    _adapter_cache.clear()
