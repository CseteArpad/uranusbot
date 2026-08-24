"""
Uranus U-0.1 – systemd drop-in bemenet-validáció (U0-SEC-001).

Minden felhasználói input, amely a
``/etc/systemd/system/uranus-runner.service.d/30-canonical-env.conf``
drop-inba kerül, ezen a modulon megy át.

Alapelv: **fail-closed**. Érvénytelen input esetén ``SettingsValidationError``
száll, és a hívó ilyenkor NEM ír fájlt, NEM hív ``daemon-reload``-ot és
NEM indít újra service-t.

A modulnak szándékosan nincs Flask-, fájlrendszer- és subprocess-függősége,
így önállóan, mellékhatás nélkül tesztelhető.

Miért nem elég önmagában a pair_utils:
    ``pair_utils.split_pair()`` a ``.strip()`` miatt csak a *külső* whitespace-t
    vágja le. A ``"XRP/USDC\\nExecStartPre=..."`` bemenetet átengedné, mert a
    beágyazott sortörés a quote részbe kerül. Ezért a karakterkészlet-ellenőrzés
    (``ensure_safe_scalar``) MINDIG megelőzi a kanonikus pár-feloldást.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

try:  # flat layout: az app/ könyvtár van a sys.path-on (élő futás)
    from exchange import pair_utils
except ImportError:  # csomag-layout: a repo gyökere van a sys.path-on (tesztek)
    from app.exchange import pair_utils


class SettingsValidationError(ValueError):
    """Érvénytelen settings-bemenet. A hívó fail-closed módon utasítsa el."""


#: Egy mező maximális hossza a drop-inban.
MAX_FIELD_LEN = 255

#: A Freqtrade által ténylegesen támogatott timeframe-ek.
#: Figyelem: "1m" = perc, "1M" = hónap – a kis/nagybetű jelentést hordoz,
#: ezért itt NINCS case-normalizálás.
SUPPORTED_TIMEFRAMES = frozenset(
    {
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    }
)

#: Az egyetlen elfogadott pár-alak a drop-inban (kanonikus, slash-es).
_PAIR_RE = re.compile(r"^[A-Z0-9]{2,20}/[A-Z0-9]{2,20}$")

#: RFC 1123 hostname (label-enként max 63 karakter).
_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)

#: Az FT_URL-ben megengedett path-karakterek (query/fragment tiltott).
_URL_PATH_RE = re.compile(r"^[A-Za-z0-9/_.\-]*$")

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: A drop-inban engedélyezett Environment-kulcsok. Bármi más strukturális hiba.
ALLOWED_ENV_KEYS = frozenset(
    {
        "TICK_SECONDS", "PAIR", "TIMEFRAME", "LIMIT",
        "EXECUTION_ENABLED", "EXECUTION_LOG_ONLY", "BUY_LOCK_TTL_SEC",
        "STD_SELL_ENABLED", "STD_SELL_PCT",
        "RECOVERY_SELL_RETRACE_PCT", "RECOVERY_PROFIT_TARGET_PCT",
        "PANIC_SELL_ENABLED", "PANIC_SELL_PCT",
        "SELL_REVERSAL_MIN_PCT", "CATASTROPHE_SELL_PCT",
        "STD_BUY_ENABLED", "STD_BUY_PCT", "RECOVERY_BUY_REBOUND_PCT",
        "PANIC_BUY_ENABLED", "PANIC_BUY_PCT", "PANIC_BUY_CONFIRM_TICKS",
        "CATASTROPHE_BUY_PCT",
        "MA_FILTER_ENABLED", "MA_PERIOD", "MA_SIDEWAYS_BAND_PCT",
        "KILL_SWITCH", "MAX_TRADES_PER_DAY", "DAILY_LOSS_CAP_PCT", "FT_URL",
        "SHADOW_ENABLED", "SHADOW_START_EQUITY_USDC",
    }
)

#: A drop-in szabad szöveges (user-controlled) mezői.
FREE_TEXT_FIELDS = ("pair", "timeframe", "ft_url")


# --------------------------------------------------------------------------- #
# Alapszintű karakter-ellenőrzés
# --------------------------------------------------------------------------- #

def ensure_safe_scalar(field: str, value) -> str:
    """
    Minden drop-inba kerülő skalár első szűrője.

    Elutasít: nem-string típust, üres/túl hosszú értéket, továbbá minden
    control karaktert (CR, LF, NUL, TAB, DEL) és nem-ASCII bájtot.
    Ez zárja le a systemd-direktíva injekciót a forrásánál.
    """
    if not isinstance(value, str):
        raise SettingsValidationError(
            f"{field}: string érték szükséges, kapott típus: {type(value).__name__}"
        )
    if len(value) > MAX_FIELD_LEN:
        raise SettingsValidationError(
            f"{field}: túl hosszú érték (max {MAX_FIELD_LEN} karakter)"
        )
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise SettingsValidationError(
                f"{field}: tiltott vezérlőkarakter az értékben (U+{code:04X})"
            )
        if code > 0x7E:
            raise SettingsValidationError(
                f"{field}: csak ASCII karakter engedélyezett (U+{code:04X})"
            )
    return value


# --------------------------------------------------------------------------- #
# Mező-specifikus validátorok
# --------------------------------------------------------------------------- #

def validate_pair(raw) -> str:
    """
    Kanonikus 'BASE/QUOTE' pár. A karakterkészlet-ellenőrzés megelőzi az
    Exchange Layer hívását, így sortörés sosem jut el a pár-parserig.
    """
    text = ensure_safe_scalar("pair", raw).strip()
    if not text:
        raise SettingsValidationError("pair: a mező nem lehet üres")

    candidate = text.upper()
    if not _PAIR_RE.match(candidate):
        raise SettingsValidationError(
            "pair: csak 'BASE/QUOTE' alak engedélyezett (pl. XRP/USDC)"
        )

    # Kanonikus feloldás az Exchange Layerrel – nincs duplikált symbol parser.
    try:
        canonical = pair_utils.normalize_pair(candidate)
    except Exception as exc:  # InvalidPairError és minden más parser-hiba
        raise SettingsValidationError(f"pair: érvénytelen pár ({exc})") from exc

    if not _PAIR_RE.match(canonical):
        raise SettingsValidationError("pair: a normalizált pár nem kanonikus alakú")
    return canonical


def validate_timeframe(raw) -> str:
    """Explicit whitelist – csak ténylegesen támogatott Freqtrade timeframe."""
    text = ensure_safe_scalar("timeframe", raw).strip()
    if text not in SUPPORTED_TIMEFRAMES:
        allowed = ", ".join(sorted(SUPPORTED_TIMEFRAMES))
        raise SettingsValidationError(
            f"timeframe: nem támogatott érték. Engedélyezett: {allowed}"
        )
    return text


def validate_ft_url(raw) -> str:
    """
    Freqtrade REST base URL.

    Kötelező: http/https séma, érvényes host, opcionális 1..65535 port.
    Tiltott: userinfo (user:pass@), query, fragment, vezérlőkarakter.
    A visszaadott érték kanonikusan újraépített, tehát a drop-inba csak
    ellenőrzött komponensekből összerakott string kerül.
    """
    text = ensure_safe_scalar("ft_url", raw).strip()
    if not text:
        raise SettingsValidationError("ft_url: a mező nem lehet üres")

    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise SettingsValidationError(f"ft_url: nem értelmezhető URL ({exc})") from exc

    if parts.scheme not in _ALLOWED_URL_SCHEMES:
        raise SettingsValidationError(
            "ft_url: csak 'http' vagy 'https' séma engedélyezett"
        )
    if "@" in parts.netloc:
        raise SettingsValidationError(
            "ft_url: a felhasználónév/jelszó az URL-ben nem engedélyezett"
        )
    if parts.query or parts.fragment:
        raise SettingsValidationError("ft_url: query és fragment nem engedélyezett")

    try:
        port = parts.port
    except ValueError as exc:
        raise SettingsValidationError(f"ft_url: érvénytelen port ({exc})") from exc
    if port is not None and not (1 <= port <= 65535):
        raise SettingsValidationError("ft_url: a port 1 és 65535 közé essen")

    host = parts.hostname
    if not host:
        raise SettingsValidationError("ft_url: hiányzó hostname")

    is_ipv6 = parts.netloc.startswith("[") or ":" in host
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False

    if not is_ip:
        if is_ipv6:
            raise SettingsValidationError("ft_url: érvénytelen IPv6 cím")
        if len(host) > 253 or not _HOSTNAME_RE.match(host):
            raise SettingsValidationError("ft_url: érvénytelen hostname")

    path = parts.path.rstrip("/")
    if path and not _URL_PATH_RE.match(path):
        raise SettingsValidationError("ft_url: érvénytelen karakter az útvonalban")

    host_part = f"[{host}]" if is_ip and is_ipv6 else host
    port_part = f":{port}" if port is not None else ""
    return f"{parts.scheme}://{host_part}{port_part}{path}"


# --------------------------------------------------------------------------- #
# Teljes payload + strukturális kimenet-ellenőrzés
# --------------------------------------------------------------------------- #

def validate_settings_payload(cleaned: dict) -> dict:
    """
    A ``/api/settings`` már típuskonvertált payloadjának biztonsági validációja.

    A numerikus és bool mezőket a hívó már ``float()``/``int()``/``bool()``
    hívással kényszerítette, ezért azok nem hordozhatnak injekciót; a szabad
    szöveges mezők viszont itt kapnak szigorú validációt. Védelmi mélységként
    minden szerializálandó érték is átmegy a karakter-ellenőrzésen.

    Returns:
        Új dict a validált (kanonizált) értékekkel. Az eredetit nem módosítja.
    """
    if not isinstance(cleaned, dict):
        raise SettingsValidationError("settings: dict payload szükséges")

    validated = dict(cleaned)
    validated["pair"] = validate_pair(cleaned.get("pair"))
    validated["timeframe"] = validate_timeframe(cleaned.get("timeframe"))
    validated["ft_url"] = validate_ft_url(cleaned.get("ft_url"))

    # Védelmi mélység: minden más érték szerializált alakja is legyen ártalmatlan.
    for key, value in validated.items():
        if key in FREE_TEXT_FIELDS:
            continue
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            continue
        ensure_safe_scalar(key, value)

    return validated


def assert_dropin_safe(text: str) -> str:
    """
    Strukturális záróellenőrzés a kész drop-in szövegen.

    Minden sor csak az alábbiak egyike lehet:
      * üres sor,
      * ``[Service]``,
      * ``Environment=KEY=VALUE``, ahol KEY az ``ALLOWED_ENV_KEYS`` eleme.

    Így akkor sem kerülhet idegen systemd-direktíva a fájlba, ha a fenti
    mező-validátorok valamelyike a jövőben meggyengülne.
    """
    if not isinstance(text, str):
        raise SettingsValidationError("drop-in: string tartalom szükséges")
    if "\r" in text or "\x00" in text:
        raise SettingsValidationError("drop-in: tiltott vezérlőkarakter a tartalomban")

    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        if line == "[Service]":
            continue
        if not line.startswith("Environment="):
            raise SettingsValidationError(
                f"drop-in: nem engedélyezett direktíva a(z) {lineno}. sorban"
            )
        remainder = line[len("Environment="):]
        key, sep, _ = remainder.partition("=")
        if not sep or key not in ALLOWED_ENV_KEYS:
            raise SettingsValidationError(
                f"drop-in: ismeretlen Environment kulcs a(z) {lineno}. sorban"
            )
    return text
