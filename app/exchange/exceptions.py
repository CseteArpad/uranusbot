"""
Uranus Exchange Layer – kivételek.

FÁZIS 7.5.1/7.5.2 – nincs hálózati logika, nincs éles kapcsolat.
"""
from __future__ import annotations


class ExchangeError(Exception):
    """Az exchange layer összes saját kivételének közös őse."""


class InvalidPairError(ExchangeError):
    """Érvénytelen vagy felismerhetetlen pár-formátum."""


class UnsupportedExchangeError(ExchangeError):
    """Ismeretlen / nem támogatott exchange név."""


class AdapterNotConfiguredError(ExchangeError):
    """Hiányzó vagy üres exchange-konfiguráció."""


class TickerFetchError(ExchangeError):
    """Ticker-lekérdezés hibája: hálózati hiba, HTTP hiba vagy érvénytelen válasz."""
