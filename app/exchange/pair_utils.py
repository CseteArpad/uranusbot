"""
Uranus Exchange Layer – pár-normalizálás.

A projektben három symbol-dialektus él:
  - kanonikus / Freqtrade / ccxt: "XRP/USDC"
  - Binance REST:                 "XRPUSDC"
  - OKX natív instId:             "XRP-USDC"

A kanonikus formátum a slash-es; minden konverzió ezen keresztül megy.
"""
from __future__ import annotations

from typing import Tuple

from .exceptions import InvalidPairError

# Sorrend számít: a hosszabb quote-okat előbb próbáljuk (pl. FDUSD a USD előtt állna).
COMMON_QUOTES: Tuple[str, ...] = (
    "USDT", "USDC", "BUSD", "FDUSD", "BTC", "ETH", "BNB", "TRY", "EUR",
)


def split_pair(pair: str) -> Tuple[str, str]:
    """
    Bármely támogatott dialektusból (XRP/USDC, XRP-USDC, XRPUSDC)
    visszaadja a (base, quote) párost, nagybetűsítve.

    Konkatenált formátumnál best-effort: a COMMON_QUOTES végződések
    alapján bont. Ismeretlen quote esetén InvalidPairError.
    """
    if not pair or not isinstance(pair, str) or not pair.strip():
        raise InvalidPairError(f"pair must be a non-empty string, got: {pair!r}")

    p = pair.strip().upper()

    for sep in ("/", "-"):
        if sep in p:
            base, _, quote = p.partition(sep)
            base, quote = base.strip(), quote.strip()
            if not base or not quote or "/" in quote or "-" in quote:
                raise InvalidPairError(f"invalid pair format: {pair!r}")
            return base, quote

    for q in COMMON_QUOTES:
        if p.endswith(q) and len(p) > len(q):
            return p[: -len(q)], q

    raise InvalidPairError(
        f"cannot split concatenated symbol {pair!r}; use the slash format (BASE/QUOTE)"
    )


def normalize_pair(pair: str) -> str:
    """Kanonikus formátum: 'XRP/USDC'."""
    base, quote = split_pair(pair)
    return f"{base}/{quote}"


def to_binance_symbol(pair: str) -> str:
    """'XRP/USDC' | 'XRP-USDC' | 'xrpusdc' -> 'XRPUSDC'"""
    base, quote = split_pair(pair)
    return f"{base}{quote}"


def to_okx_inst_id(pair: str) -> str:
    """'XRP/USDC' | 'XRPUSDC' -> 'XRP-USDC'"""
    base, quote = split_pair(pair)
    return f"{base}-{quote}"
