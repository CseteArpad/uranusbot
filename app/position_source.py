"""
Uranus U-3 – pozíció-forrás (transport réteg).

Ez a modul EGYETLEN dolgot csinál: megkérdezi a Freqtrade-et, hogy van-e nyitott
pozíció, és a választ háromértékű, strukturált verdikté alakítja.

Miért külön modul
-----------------
A pozícióállapotra épülő döntés – „mehet-e ki éles order” – biztonságkritikus.
Az azt eldöntő réteget (``position_authority``) ezért szándékosan mentesítjük a
hálózattól, a titkoktól és az urllib hibataxonómiájától: így az authority
tiszta, mellékhatás nélküli függvényekből áll, önmagában auditálható és
replay-elhető, anélkül hogy transport-kódot kellene olvasni hozzá.

Határ:

    position_source     -> „mit mond a Freqtrade”   (hálózat, credential, parse)
    position_authority  -> „mit kezdünk vele”       (reconcile, persist, gate)

A függés egyirányú: az authority importálja a source-ot, fordítva soha.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------- #
# Állapotok – a verdikt háromértékű, mert a „nem tudjuk” önálló valóság
# --------------------------------------------------------------------------- #

STATE_OPEN = "OPEN"
STATE_FLAT = "FLAT"
STATE_UNKNOWN = "UNKNOWN"

VERIFIED_STATES = (STATE_OPEN, STATE_FLAT)

# --------------------------------------------------------------------------- #
# Hibataxonómia – strukturált, auditálható kódok. Nincs külön exception osztály:
# a cél nem kivétel-hierarchia, hanem a state-ben látható, gépi ok.
# --------------------------------------------------------------------------- #

ERR_AUTH = "AUTH_ERROR"
ERR_TIMEOUT = "TIMEOUT"
ERR_NETWORK = "NETWORK_ERROR"
ERR_BAD_RESPONSE = "BAD_RESPONSE"
ERR_MULTIPLE_TRADES = "MULTIPLE_TRADES"
ERR_PAIR_MISMATCH = "PAIR_MISMATCH"
ERR_DATA_INCONSISTENT = "DATA_INCONSISTENT"
ERR_UNKNOWN = "UNKNOWN_ERROR"

DEFAULT_TIMEOUT = 8.0

#: Az EGYETLEN ismert REST-útvonal. A korábbi hat URL-es fallback-lánc épp azt
#: tette lehetetlenné, hogy megmondjuk, MIÉRT nem kaptunk eredményt.
STATUS_PATH = "/api/v1/status"


def as_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PositionFetchResult:
    """
    Egyetlen Freqtrade-lekérdezés eredménye.

    Ez oldja fel az U-3 root cause-t: a ``FLAT`` (sikeres lekérdezés, nulla
    trade) és az ``UNKNOWN`` (a lekérdezés meghiúsult) többé nem ugyanaz az
    érték. A régi ``dict | None`` szerződés ezt a két valóságot ugyanazzal a
    ``None``-nal jelezte.
    """
    status: str
    trades: Tuple[dict, ...] = ()
    error: Optional[str] = None
    error_detail: Optional[str] = None
    http_status: Optional[int] = None
    observed_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in VERIFIED_STATES


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

def _auth_header() -> dict:
    """Basic auth fejléc. Titkot SOHA nem naplózunk és nem adunk vissza."""
    user = os.getenv("FT_USERNAME", "").strip()
    password = os.getenv("FT_PASSWORD", "").strip()
    if not user and not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def base_url() -> str:
    """A kanonikus Uranus Freqtrade alapcím (U-2B feloldás)."""
    try:
        import ft_endpoint
        return ft_endpoint.canonical_ft_url()
    except Exception:
        return os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")


def fetch_open_position(
    pair: str,
    *,
    timeout: Optional[float] = None,
    url_base: Optional[str] = None,
) -> PositionFetchResult:
    """
    Az Uranus kanonikus pozíció-lekérdezése.

    KIZÁRÓLAG a ``/api/v1/status`` végpontot használja, pár-szűrés NÉLKÜL – a
    szűrés Uranus oldalon történik (``_classify``), hogy a más páron nyitott
    trade se tűnhessen el csendben.
    """
    now = time.time()
    url = (url_base or base_url()).rstrip("/") + STATUS_PATH
    tmo = DEFAULT_TIMEOUT if timeout is None else float(timeout)

    try:
        req = Request(url, headers=_auth_header())
        with urlopen(req, timeout=tmo) as resp:
            http_status = int(getattr(resp, "status", 200) or 200)
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        err = ERR_AUTH if code in (401, 403) else ERR_BAD_RESPONSE
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=err,
            error_detail=f"HTTP {code}", http_status=code, observed_at=now,
        )
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        is_timeout = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
        return PositionFetchResult(
            status=STATE_UNKNOWN,
            error=ERR_TIMEOUT if is_timeout else ERR_NETWORK,
            error_detail=f"{type(reason).__name__}: {reason}", observed_at=now,
        )
    except TimeoutError as exc:
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=ERR_TIMEOUT,
            error_detail=f"{type(exc).__name__}: {exc}", observed_at=now,
        )
    except Exception as exc:  # pragma: no cover - védőháló
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=ERR_UNKNOWN,
            error_detail=f"{type(exc).__name__}: {exc}", observed_at=now,
        )

    try:
        body = json.loads(raw)
    except Exception as exc:
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=ERR_BAD_RESPONSE,
            error_detail=f"non-JSON body: {type(exc).__name__}",
            http_status=http_status, observed_at=now,
        )

    if not isinstance(body, list):
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=ERR_BAD_RESPONSE,
            error_detail=f"unexpected body type: {type(body).__name__}",
            http_status=http_status, observed_at=now,
        )

    trades = tuple(t for t in body if isinstance(t, dict))
    if len(trades) != len(body):
        return PositionFetchResult(
            status=STATE_UNKNOWN, error=ERR_BAD_RESPONSE,
            error_detail="list contains non-object entries",
            http_status=http_status, observed_at=now,
        )

    return classify(trades, pair, http_status, now)


def classify(
    trades: Tuple[dict, ...], pair: str, http_status: Optional[int], now: float
) -> PositionFetchResult:
    """
    Sikeres válasz -> FLAT / OPEN / UNKNOWN.

    Tiszta függvény: nincs benne hálózat, óra vagy fájlrendszer – az ``now``
    paraméterként jön, hogy a besorolás determinisztikusan tesztelhető legyen.
    """
    if not trades:
        return PositionFetchResult(
            status=STATE_FLAT, trades=(), http_status=http_status, observed_at=now
        )

    want = str(pair or "").strip()
    matching = [t for t in trades if str(t.get("pair") or "").strip() == want]
    foreign = [t for t in trades if str(t.get("pair") or "").strip() != want]

    if len(matching) > 1:
        return PositionFetchResult(
            status=STATE_UNKNOWN, trades=trades, error=ERR_MULTIPLE_TRADES,
            error_detail=f"{len(matching)} open trades on {want}",
            http_status=http_status, observed_at=now,
        )

    if foreign:
        # Egypáras, egypozíciós modellben egy idegen páron nyitott trade
        # inkonzisztencia: nem a miénk, de a max nyitott pozíció keretét fogyasztja.
        pairs = sorted({str(t.get("pair")) for t in foreign})
        return PositionFetchResult(
            status=STATE_UNKNOWN, trades=trades, error=ERR_PAIR_MISMATCH,
            error_detail=f"open trade(s) on unexpected pair(s): {pairs}",
            http_status=http_status, observed_at=now,
        )

    if not matching:
        # Ide nem lehet jutni (foreign üres és matching üres => trades üres),
        # de a defenzív ág egyértelmű: nem állítunk FLAT-et bizonyíték nélkül.
        return PositionFetchResult(
            status=STATE_UNKNOWN, trades=trades, error=ERR_DATA_INCONSISTENT,
            error_detail="no matching and no foreign trades in non-empty list",
            http_status=http_status, observed_at=now,
        )

    trade = matching[0]
    if as_int(trade.get("trade_id")) is None:
        return PositionFetchResult(
            status=STATE_UNKNOWN, trades=trades, error=ERR_DATA_INCONSISTENT,
            error_detail="missing or invalid trade_id",
            http_status=http_status, observed_at=now,
        )
    if as_float(trade.get("open_rate")) is None and as_float(
        trade.get("open_rate_requested")
    ) is None:
        return PositionFetchResult(
            status=STATE_UNKNOWN, trades=trades, error=ERR_DATA_INCONSISTENT,
            error_detail="missing or invalid open_rate",
            http_status=http_status, observed_at=now,
        )

    return PositionFetchResult(
        status=STATE_OPEN, trades=(trade,), http_status=http_status, observed_at=now
    )
