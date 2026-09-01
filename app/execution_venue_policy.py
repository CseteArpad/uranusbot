"""
Uranus – EXECUTION VENUE POLICY (tulajdonosi architekturális döntés, 2026-09-01).

    URANUS_EXECUTION_VENUE_POLICY = OKX_SPOT_ONLY
    BINANCE_EXECUTION_ALLOWED     = NO
    BINANCE_PRODUCTION_SUPPORT    = RETIRED

Mi ez, és mi NEM
----------------
Ez **tulajdonosi döntés**, nem empirikus stratégiai verdikt. Nem abból
következik, hogy a Binance rosszabb végrehajtási helyszín lenne; abból
következik, hogy az UranusBot végrehajtási felülete egyetlen helyszínre szűkül,
és a többi út **véglegesen** lezárul.

A tiltás **kizárólag az éles végrehajtásra** vonatkozik. A Binance eredetű
történeti gyertyák, kutatási artefaktumok, manifestek és reprodukálhatósági
bizonyítékok **megmaradnak** és továbbra is használhatók::

    BINANCE_AS_RESEARCH_PROVENANCE = ALLOWED
    BINANCE_AS_LIVE_EXECUTION      = FORBIDDEN

Miért modul-konstans, és miért nem környezeti változó
-----------------------------------------------------
Ugyanaz a minta, mint a stratégia ``SIGNAL_BASED_ENTRY_ENABLED``-jénél: egy
kivezetett végrehajtási helyszín **nem kapcsolható vissza konfigurációval**.
Ha az ``ALLOWED_LIVE_EXECUTION_VENUES`` env-ből felülbírálható lenne, akkor egy
elgépelt drop-in, egy visszaállított régi config vagy egy félresikerült deploy
újra élesíthetné a Binance-utat – pontosan az a hibaosztály, amit ez a modul
megszüntet. A visszakapcsolás csak kódváltoztatással és külön review-val
lehetséges.

Ugyanezért **fail-closed** minden bemenetre: hiányzó, üres, ismeretlen vagy
értelmezhetetlen helyszín ugyanúgy elutasítás, mint egy kifejezetten tiltott.
Nincs „alapértelmezett helyszín”, és nincs fallback.

Két, egymástól független kapu
-----------------------------
1. **Helyszín-kapu** – melyik tőzsdén szabad *egyáltalán* élesen végrehajtani.
   Binance: soha. Ez a modul-konstans ``ALLOWED_LIVE_EXECUTION_VENUES``.
2. **Élesítési kapu** – szabad-e *most* élesen végrehajtani ott.
   ``LIVE_EXECUTION_AUTHORIZED`` jelenleg ``False``: az OKX helyszín
   engedélyezett, de az éles kereskedés **nincs jóváhagyva**.

A kettő szándékosan külön áll. A Binance kivezetése **nem** jelent OKX
élesítési engedélyt::

    OKX_LIVE_EXECUTION_AUTHORIZED = NO
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# --------------------------------------------------------------------------- #
# 1. A POLITIKA – modul-konstansok, env-ből NEM felülbírálhatók
# --------------------------------------------------------------------------- #

#: Az egyetlen helyszín, ahol éles végrehajtás egyáltalán szóba jöhet.
ALLOWED_LIVE_EXECUTION_VENUES: frozenset[str] = frozenset({"okx"})

#: Véglegesen kivezetett helyszínek. Ezekre külön, beszédes hibakód jár, hogy a
#: napló megkülönböztesse a „soha többé”-t az „ismeretlen”-től.
RETIRED_EXECUTION_VENUES: frozenset[str] = frozenset({"binance"})

#: Éles végrehajtási engedély. **Ez nem helyszín-kérdés.** Amíg ``False``, a
#: ``dry_run=false`` indulás akkor is elutasított, ha a helyszín OKX.
LIVE_EXECUTION_AUTHORIZED: bool = False

#: Production identity – az indítási guard ezt várja.
EXPECTED_BOT: str = "uranus"
EXPECTED_MARKET_TYPE: str = "spot"

#: A guard által használt kilépési kód. Szándékosan az ``EX_CONFIG`` (sysexits.h):
#: megkülönböztethető a véletlen összeomlástól, és a systemd
#: ``RestartPreventExitStatus=78`` pontosan erre tud szűrni, hogy egy
#: szándékos elutasítás NE váljon restart-hurokká.
EXIT_STARTUP_REFUSED: int = 78

#: Helyszín-alias -> kanonikus név. A Freqtrade OKX-oldali azonosítója ``myokx``;
#: a Binance-nak több regionális variánsa van, mind kivezetve.
_VENUE_ALIASES: dict[str, str] = {
    "okx": "okx",
    "myokx": "okx",
    "okex": "okx",
    "okcoin": "okx",
    "binance": "binance",
    "binanceus": "binance",
    "binanceusdm": "binance",
    "binancecoinm": "binance",
    "binanceje": "binance",
    "myokx_demo": "okx_demo",
}

#: Env-változó nevekben keresett Binance-hitelesítési nyomok. A modul SOHA nem
#: olvassa ki és nem naplózza az értéket – csak a név jelenlétét jelzi.
_BINANCE_ENV_MARKERS: tuple[str, ...] = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_KEY",
    "BINANCE_SECRET",
    "BINANCE_APIKEY",
    "URANUS_BINANCE_KEY",
    "URANUS_BINANCE_SECRET",
)

#: Config-értékekben keresett Binance-végpontok.
_BINANCE_HOST_MARKERS: tuple[str, ...] = (
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
    "stream.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "binance.us",
)


# --------------------------------------------------------------------------- #
# 2. VERDIKT
# --------------------------------------------------------------------------- #

# Elutasítási kódok. Stabil, gépi olvasásra szánt sztringek – tesztek és
# naplóelemzés ezekre támaszkodnak, ne írd át őket felelőtlenül.
REFUSE_VENUE_MISSING = "VENUE_MISSING"
REFUSE_VENUE_RETIRED = "VENUE_RETIRED"
REFUSE_VENUE_UNSUPPORTED = "VENUE_UNSUPPORTED"
REFUSE_BINANCE_CREDENTIAL = "BINANCE_CREDENTIAL_PRESENT"
REFUSE_BINANCE_ENDPOINT = "BINANCE_ENDPOINT_PRESENT"
REFUSE_MARKET_TYPE = "MARKET_TYPE_NOT_SPOT"
REFUSE_BOT_IDENTITY = "BOT_IDENTITY_MISMATCH"
REFUSE_LIVE_NOT_AUTHORIZED = "LIVE_EXECUTION_NOT_AUTHORIZED"

ALLOW_OK = "OK"


@dataclass(frozen=True)
class StartupVerdict:
    """
    Az indítási guard eredménye.

    ``allowed`` csak akkor igaz, ha MINDEN kapu átengedte. Az ``code`` az első
    (legsúlyosabb) elutasítási ok; a ``findings`` az összes megtalált ok, hogy
    egyetlen indítási kísérlet megmutassa a teljes képet, ne csak az elsőt.
    """

    allowed: bool
    code: str
    reason: str
    venue: Optional[str] = None
    findings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "venue": self.venue,
            "findings": list(self.findings),
        }

    def log_line(self) -> str:
        status = "ALLOWED" if self.allowed else "REFUSED"
        extra = f" findings={','.join(self.findings)}" if self.findings else ""
        return (
            f"STARTUP={status} code={self.code} venue={self.venue or '<none>'} "
            f"reason={self.reason}{extra}"
        )


class ExecutionVenueForbidden(RuntimeError):
    """Éles végrehajtási kísérlet tiltott vagy nem engedélyezett helyszínen."""

    def __init__(self, verdict: StartupVerdict):
        super().__init__(verdict.log_line())
        self.verdict = verdict


# --------------------------------------------------------------------------- #
# 3. HELYSZÍN-NORMALIZÁLÁS
# --------------------------------------------------------------------------- #

def canonical_venue(name: object) -> Optional[str]:
    """
    Tetszőleges helyszín-megnevezésből kanonikus név, vagy ``None``.

    ``None`` a válasz hiányzó, üres, nem sztring vagy nem ismert névre – a
    hívónak mindhármat elutasításként kell kezelnie (fail-closed).
    """
    if name is None or isinstance(name, bool):
        return None
    text = str(name).strip().lower()
    if not text:
        return None
    return _VENUE_ALIASES.get(text)


def is_retired_venue(name: object) -> bool:
    """Igaz, ha a megnevezés egy véglegesen kivezetett helyszínre mutat."""
    return canonical_venue(name) in RETIRED_EXECUTION_VENUES


def is_allowed_live_venue(name: object) -> bool:
    """Igaz, ha a helyszínen éles végrehajtás egyáltalán szóba jöhet."""
    return canonical_venue(name) in ALLOWED_LIVE_EXECUTION_VENUES


# --------------------------------------------------------------------------- #
# 4. HITELESÍTÉSI NYOMOK – nevek, soha nem értékek
# --------------------------------------------------------------------------- #

def _walk_strings(node: Any, path: str = ""):
    """Rekurzívan bejárja a configot, és (útvonal, sztring) párokat ad vissza."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def binance_credential_markers(
    config: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[str, ...]:
    """
    Binance-hitelesítési nyomok **nevei** a configban és a környezetben.

    A visszatérési érték kizárólag útvonalakat és változóneveket tartalmaz;
    titok-értéket ez a függvény soha nem ad vissza és soha nem naplóz.
    """
    markers: list[str] = []

    if isinstance(config, Mapping):
        exchange = config.get("exchange")
        if isinstance(exchange, Mapping):
            venue = canonical_venue(exchange.get("name"))
            has_material = any(
                str(exchange.get(field_name) or "").strip()
                for field_name in ("key", "secret", "password", "uid")
            )
            if has_material and venue in RETIRED_EXECUTION_VENUES:
                markers.append("exchange.key/secret@retired_venue")
            elif has_material and venue is None:
                # Ismeretlen helyszín + kulcsanyag: nem tudjuk kizárni, hogy
                # Binance-hoz tartozik, tehát fail-closed.
                markers.append("exchange.key/secret@unknown_venue")

    if isinstance(env, Mapping):
        for name in _BINANCE_ENV_MARKERS:
            if str(env.get(name) or "").strip():
                markers.append(f"env:{name}")

    return tuple(markers)


def binance_endpoint_markers(
    config: Optional[Mapping[str, Any]] = None,
) -> tuple[str, ...]:
    """Binance-végpontra mutató config-útvonalak **nevei** (érték nélkül)."""
    if not isinstance(config, Mapping):
        return ()
    markers: list[str] = []
    for path, value in _walk_strings(config):
        lowered = value.lower()
        if any(host in lowered for host in _BINANCE_HOST_MARKERS):
            markers.append(path)
    return tuple(markers)


# --------------------------------------------------------------------------- #
# 5. AZ INDÍTÁSI GUARD
# --------------------------------------------------------------------------- #

def _truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def evaluate_startup(
    config: Optional[Mapping[str, Any]],
    env: Optional[Mapping[str, str]] = None,
    *,
    bot_name: Optional[str] = None,
) -> StartupVerdict:
    """
    Production identity + helyszín-politika kiértékelése indulás előtt.

    Ellenőrzött invariánsok::

        BOT              = URANUS
        EXECUTION_VENUE  ∈ ALLOWED_LIVE_EXECUTION_VENUES   (= {"okx"})
        MARKET_TYPE      = SPOT
        LIVE_AUTHORIZATION = explicit   (ha dry_run=false)

    Bármelyik eltérése ``allowed=False``. A függvény **nem dob kivételt** és
    nem végez I/O-t: verdiktet ad vissza, a következményt a hívó vonja le.
    """
    env = os.environ if env is None else env
    findings: list[str] = []

    if not isinstance(config, Mapping):
        return StartupVerdict(
            allowed=False,
            code=REFUSE_VENUE_MISSING,
            reason="freqtrade config is missing or unreadable; venue cannot be established",
            venue=None,
            findings=(REFUSE_VENUE_MISSING,),
        )

    exchange = config.get("exchange")
    raw_venue = exchange.get("name") if isinstance(exchange, Mapping) else None
    venue = canonical_venue(raw_venue)

    # --- 1. helyszín ------------------------------------------------------- #
    venue_code: Optional[str] = None
    if raw_venue is None or not str(raw_venue).strip():
        venue_code = REFUSE_VENUE_MISSING
        venue_reason = "exchange.name is missing or empty"
    elif venue in RETIRED_EXECUTION_VENUES:
        venue_code = REFUSE_VENUE_RETIRED
        venue_reason = (
            f"exchange.name={str(raw_venue).strip().lower()!r} resolves to a PERMANENTLY "
            f"RETIRED venue; Binance live execution was retired by owner decision "
            f"(2026-09-01) and cannot be re-enabled by configuration"
        )
    elif venue not in ALLOWED_LIVE_EXECUTION_VENUES:
        venue_code = REFUSE_VENUE_UNSUPPORTED
        venue_reason = (
            f"exchange.name={str(raw_venue).strip().lower()!r} is not an allowed venue "
            f"(allowed: {sorted(ALLOWED_LIVE_EXECUTION_VENUES)})"
        )
    else:
        venue_reason = ""

    if venue_code:
        findings.append(venue_code)

    # --- 2. hitelesítési nyomok ------------------------------------------- #
    cred_markers = binance_credential_markers(config, env)
    if cred_markers:
        findings.append(REFUSE_BINANCE_CREDENTIAL)

    endpoint_markers = binance_endpoint_markers(config)
    if endpoint_markers:
        findings.append(REFUSE_BINANCE_ENDPOINT)

    # --- 3. piactípus ------------------------------------------------------ #
    market_type = str(config.get("trading_mode") or "").strip().lower()
    if market_type != EXPECTED_MARKET_TYPE:
        findings.append(REFUSE_MARKET_TYPE)

    # --- 4. bot identity --------------------------------------------------- #
    actual_bot = str(bot_name if bot_name is not None else config.get("bot_name") or "").strip().lower()
    if not actual_bot.startswith(EXPECTED_BOT):
        findings.append(REFUSE_BOT_IDENTITY)

    # --- 5. éles engedély -------------------------------------------------- #
    dry_run = _truthy(config.get("dry_run"), default=False)
    if not dry_run and not LIVE_EXECUTION_AUTHORIZED:
        findings.append(REFUSE_LIVE_NOT_AUTHORIZED)

    if not findings:
        return StartupVerdict(
            allowed=True,
            code=ALLOW_OK,
            reason="venue, identity and live-authorization gates all satisfied",
            venue=venue,
            findings=(),
        )

    # Az első finding a legsúlyosabb: a helyszín-hiba megelőz mindent, mert az
    # a véglegesen lezárt kapu.
    primary = findings[0]
    reasons = {
        REFUSE_VENUE_MISSING: venue_reason or "execution venue could not be established",
        REFUSE_VENUE_RETIRED: venue_reason,
        REFUSE_VENUE_UNSUPPORTED: venue_reason,
        REFUSE_BINANCE_CREDENTIAL: (
            "Binance credential material is present in the live configuration or "
            f"environment ({len(cred_markers)} marker(s): {', '.join(cred_markers)})"
        ),
        REFUSE_BINANCE_ENDPOINT: (
            f"configuration references a Binance endpoint at: {', '.join(endpoint_markers)}"
        ),
        REFUSE_MARKET_TYPE: (
            f"trading_mode={market_type!r}, expected {EXPECTED_MARKET_TYPE!r}"
        ),
        REFUSE_BOT_IDENTITY: (
            f"bot_name={actual_bot!r}, expected an identity starting with {EXPECTED_BOT!r}"
        ),
        REFUSE_LIVE_NOT_AUTHORIZED: (
            "dry_run=false but LIVE_EXECUTION_AUTHORIZED is False; live execution on "
            "OKX has not been authorized (owner decision 2026-09-01: retiring Binance "
            "does NOT authorize OKX live trading)"
        ),
    }
    return StartupVerdict(
        allowed=False,
        code=primary,
        reason=reasons.get(primary, "startup refused"),
        venue=venue,
        findings=tuple(findings),
    )


def assert_live_execution_allowed(
    venue: object,
    *,
    dry_run: bool = False,
    context: str = "live execution",
) -> None:
    """
    Utolsó kapu közvetlenül az order-küldés előtt (mélységi védelem).

    Akkor is meg kell hívni, ha az indítási guard már lefutott: az indulás óta a
    konfiguráció változhatott, és egy order-küldő útnak önmagában is bizonyítania
    kell, hogy engedélyezett helyszínen jár.

    Raises:
        ExecutionVenueForbidden: tiltott/ismeretlen helyszín, vagy éles
            végrehajtás engedély nélkül.
    """
    canonical = canonical_venue(venue)

    if canonical in RETIRED_EXECUTION_VENUES:
        raise ExecutionVenueForbidden(
            StartupVerdict(
                allowed=False,
                code=REFUSE_VENUE_RETIRED,
                reason=f"{context} refused: venue is permanently retired",
                venue=canonical,
                findings=(REFUSE_VENUE_RETIRED,),
            )
        )

    if canonical not in ALLOWED_LIVE_EXECUTION_VENUES:
        raise ExecutionVenueForbidden(
            StartupVerdict(
                allowed=False,
                code=REFUSE_VENUE_UNSUPPORTED if canonical else REFUSE_VENUE_MISSING,
                reason=f"{context} refused: venue is not in ALLOWED_LIVE_EXECUTION_VENUES",
                venue=canonical,
                findings=(REFUSE_VENUE_UNSUPPORTED if canonical else REFUSE_VENUE_MISSING,),
            )
        )

    if not dry_run and not LIVE_EXECUTION_AUTHORIZED:
        raise ExecutionVenueForbidden(
            StartupVerdict(
                allowed=False,
                code=REFUSE_LIVE_NOT_AUTHORIZED,
                reason=f"{context} refused: live execution is not authorized",
                venue=canonical,
                findings=(REFUSE_LIVE_NOT_AUTHORIZED,),
            )
        )


# --------------------------------------------------------------------------- #
# 6. I/O – szándékosan elkülönítve, hogy a politika tesztelhető maradjon
# --------------------------------------------------------------------------- #

def load_ft_config(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """
    Az Uranus Freqtrade ``config.json`` betöltése, vagy ``None``.

    A ``None`` nem hiba-elnyelés: az ``evaluate_startup`` a ``None``-t
    ``VENUE_MISSING`` elutasításként kezeli (fail-closed).
    """
    import json

    if path is None:
        try:  # package import
            from app import ft_endpoint  # type: ignore
        except ImportError:  # flat import (runner)
            try:
                import ft_endpoint  # type: ignore
            except ImportError:
                ft_endpoint = None  # type: ignore
        path = ft_endpoint.ft_config_path() if ft_endpoint else None

    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def policy_summary() -> dict[str, Any]:
    """Gépi olvasásra szánt politika-összefoglaló (naplóhoz, riporthoz)."""
    return {
        "URANUS_EXECUTION_VENUE_POLICY": "OKX_SPOT_ONLY",
        "ALLOWED_LIVE_EXECUTION_VENUES": sorted(ALLOWED_LIVE_EXECUTION_VENUES),
        "RETIRED_EXECUTION_VENUES": sorted(RETIRED_EXECUTION_VENUES),
        "BINANCE_EXECUTION_ALLOWED": "NO",
        "BINANCE_PRODUCTION_SUPPORT": "RETIRED",
        "BINANCE_AS_RESEARCH_PROVENANCE": "ALLOWED",
        "LIVE_EXECUTION_AUTHORIZED": LIVE_EXECUTION_AUTHORIZED,
        "OKX_LIVE_EXECUTION_AUTHORIZED": "NO" if not LIVE_EXECUTION_AUTHORIZED else "YES",
        "EXPECTED_BOT": EXPECTED_BOT,
        "EXPECTED_MARKET_TYPE": EXPECTED_MARKET_TYPE,
        "owner_decision": "OWNER_EXECUTION_VENUE_DECISION_2026-09-01.md",
    }
