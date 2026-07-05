"""
Uranus Exchange Layer – adapter-képességek leírója.

Minden adapter deklarálja, hogy mit tud; a hívó kód képesség alapján
dönthet (pl. dust-konverzió csak ott, ahol supports_dust_conversion=True).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExchangeCapabilities:
    supports_spot: bool = False
    supports_margin: bool = False
    supports_public_ohlcv: bool = False
    supports_balance: bool = False
    supports_market_orders: bool = False
    supports_limit_orders: bool = False
    supports_dust_conversion: bool = False
