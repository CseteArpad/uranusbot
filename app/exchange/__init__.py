"""
Uranus Exchange Layer (FÁZIS 7.5.x).

Publikus API:
    from app.exchange import (
        ExchangeAdapter, ExchangeCapabilities,
        ExchangeManager, get_exchange_adapter,
        BinanceAdapter, OKXAdapter,
        ExchangeError, InvalidPairError,
        UnsupportedExchangeError, AdapterNotConfiguredError,
    )
"""
from .base import ExchangeAdapter
from .binance import BinanceAdapter
from .capabilities import ExchangeCapabilities
from .exceptions import (
    AdapterNotConfiguredError,
    ExchangeError,
    InvalidPairError,
    TickerFetchError,
    UnsupportedExchangeError,
)
from .manager import ExchangeManager, clear_adapter_cache, get_exchange_adapter
from .okx import OKXAdapter

__all__ = [
    "ExchangeAdapter",
    "ExchangeCapabilities",
    "ExchangeManager",
    "get_exchange_adapter",
    "clear_adapter_cache",
    "BinanceAdapter",
    "OKXAdapter",
    "ExchangeError",
    "InvalidPairError",
    "TickerFetchError",
    "UnsupportedExchangeError",
    "AdapterNotConfiguredError",
]
