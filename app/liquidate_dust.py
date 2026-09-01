#!/usr/bin/env python3
"""
Uranus – ``liquidate_dust`` : **KIVEZETVE** (tulajdonosi döntés, 2026-09-01).

Mi volt ez, és miért kellett megszüntetni
------------------------------------------
Ez a szkript a legveszélyesebb order-út volt az egész rendszerben, és a
2026-09-01-i production auditig nem volt leltárban:

* közvetlenül beolvasta az **éles Binance API-kulcsot és secretet** a
  Freqtrade ``config.json``-ból;
* saját HMAC-SHA256 aláírást épített, és
* **aláírt ``POST /api/v3/order`` MARKET SELL** kérést küldött az
  ``api.binance.com``-ra a ``pair_whitelist`` minden párjára.

Amit **teljes egészében megkerült**: ``EXECUTION_ENABLED``, ``EXECUTION_LOG_ONLY``,
``KILL_SWITCH``, a guardrail-számlálók, a freshness gate, a profit-floor, a
position authority és maga a Freqtrade is. Egyetlen kézi futtatás valódi
piaci eladást hajtott volna végre, a bot minden biztonsági rétegén kívül.

A tulajdonosi döntés (``URANUS_EXECUTION_VENUE_POLICY = OKX_SPOT_ONLY``) után
ennek az útnak nincs többé létjogosultsága, és nem is „átírandó OKX-re”:

* a dust-konverzió nem a kereskedési logika része, és
* ha valaha kell, akkor a kontrollált végrehajtási láncon keresztül kell
  megvalósulnia, nem egy külön, aláírt REST-kliensként.

Az eredeti implementáció a Git-történetben megmarad (``git log -p --
app/liquidate_dust.py``) – provenance-célra elérhető, futtatható artefaktumként
nem.

Ez a modul szándékosan **importálható**, hogy a hívói egyértelmű, beszédes
hibát kapjanak a néma ``ImportError`` helyett – de bármely művelete
``ExecutionVenueForbidden``-t dob.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, NoReturn

try:  # package import (pytest, repo gyökér a sys.path-on)
    from app import execution_venue_policy  # type: ignore
except ImportError:  # flat import (production runner)
    import execution_venue_policy  # type: ignore

RETIRED_REASON = (
    "app/liquidate_dust.py is PERMANENTLY RETIRED (owner decision 2026-09-01). "
    "It sent signed Binance MARKET SELL orders directly, bypassing every Uranus "
    "safety gate. URANUS_EXECUTION_VENUE_POLICY=OKX_SPOT_ONLY, "
    "BINANCE_EXECUTION_ALLOWED=NO."
)


def _refuse(operation: str) -> NoReturn:
    raise execution_venue_policy.ExecutionVenueForbidden(
        execution_venue_policy.StartupVerdict(
            allowed=False,
            code=execution_venue_policy.REFUSE_VENUE_RETIRED,
            reason=f"{operation}: {RETIRED_REASON}",
            venue="binance",
            findings=(execution_venue_policy.REFUSE_VENUE_RETIRED,),
        )
    )


def binance_signed_request(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Kivezetve: aláírt Binance REST hívás nem küldhető."""
    _refuse("binance_signed_request")


def get_account(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Kivezetve: az éles Binance kulcsot ez a fa többé nem használja."""
    _refuse("get_account")


def load_config() -> Dict[str, Any]:
    """Kivezetve: ez a modul nem olvashat kulcsanyagot a live configból."""
    _refuse("load_config")


def main() -> NoReturn:
    _refuse("main")


if __name__ == "__main__":  # pragma: no cover
    print(RETIRED_REASON, file=sys.stderr)
    raise SystemExit(execution_venue_policy.EXIT_STARTUP_REFUSED)
