"""
Uranus Exchange Layer – Binance adapter, **MARKET DATA / PROVENANCE ONLY**.

Tulajdonosi architekturális döntés (2026-09-01)::

    BINANCE_AS_RESEARCH_PROVENANCE = ALLOWED
    BINANCE_AS_LIVE_EXECUTION      = FORBIDDEN

Ez az adapter **megmarad**, mert a Binance továbbra is legitim publikus
ár- és történeti adatforrás, és a korábbi kutatás reprodukálhatóságához
szükséges. Amit **véglegesen elveszít**, az a rendelési felület.

Miért nem elég a korábbi ``NotImplementedError``
------------------------------------------------
A ``create_order``/``cancel_order``/``convert_dust`` korábban „FÁZIS 7.5.3-ban
implementálandó” skeletonként állt itt. Ez **nyitva hagyott ajtó** volt: egy
jövőbeli fejlesztő jóhiszeműen kitölthette volna őket, és ezzel csendben
visszaállította volna az éles Binance végrehajtást.

Ezért ezek most nem „még nem implementált”, hanem ``ExecutionVenueForbidden``
kivételt dobó, **szándékosan lezárt** metódusok, és a ``CAPABILITIES`` is
``supports_market_orders=False``/``supports_limit_orders=False``-ra vált – a
képesség-alapú hívó kód így már a próbálkozás előtt tudja, hogy itt nincs
rendelési út. Az újranyitás csak kódváltoztatással, az
``execution_venue_policy`` módosításával és külön review-val lehetséges.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from . import pair_utils
from .base import ExchangeAdapter
from .capabilities import ExchangeCapabilities
from .exceptions import TickerFetchError

try:  # package import (pytest, repo gyökér a sys.path-on)
    from app import execution_venue_policy  # type: ignore
except ImportError:  # flat import (production runner)
    import execution_venue_policy  # type: ignore

BINANCE_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT = 10

_SKELETON_MSG = "BinanceAdapter.{method} not implemented yet (FÁZIS 7.5.3)"

#: A rendelési felület nem „hiányzik”, hanem KIVEZETVE van. Ez az üzenet
#: szándékosan más, mint a skeleton-üzenet, hogy a napló megkülönböztesse
#: a „még nincs kész”-t a „soha többé”-től.
_RETIRED_MSG = (
    "BinanceAdapter.{method} is PERMANENTLY RETIRED (owner decision 2026-09-01: "
    "URANUS_EXECUTION_VENUE_POLICY=OKX_SPOT_ONLY). Binance remains available for "
    "market data and research provenance only."
)


def _refuse(method: str) -> "execution_venue_policy.ExecutionVenueForbidden":
    return execution_venue_policy.ExecutionVenueForbidden(
        execution_venue_policy.StartupVerdict(
            allowed=False,
            code=execution_venue_policy.REFUSE_VENUE_RETIRED,
            reason=_RETIRED_MSG.format(method=method),
            venue="binance",
            findings=(execution_venue_policy.REFUSE_VENUE_RETIRED,),
        )
    )


class BinanceAdapter(ExchangeAdapter):
    name = "binance"

    #: Rendelési képességek KIKAPCSOLVA – lásd a modul docstringjét.
    CAPABILITIES = ExchangeCapabilities(
        supports_spot=True,
        supports_margin=False,
        supports_public_ohlcv=True,
        supports_balance=True,
        supports_market_orders=False,
        supports_limit_orders=False,
        supports_dust_conversion=False,
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

    # -- rendelési felület: VÉGLEGESEN KIVEZETVE --------------------------
    #
    # Ezeket NE implementáld. Ha valaha mégis szükség lenne rá, az nem itt
    # kezdődik, hanem az `execution_venue_policy` megváltoztatásával és külön
    # tulajdonosi döntéssel.

    def create_order(
        self,
        pair: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        raise _refuse("create_order")

    def cancel_order(self, order_id: str, pair: Optional[str] = None) -> Dict[str, Any]:
        raise _refuse("cancel_order")

    def convert_dust(self, assets: List[str]) -> Dict[str, Any]:
        raise _refuse("convert_dust")
