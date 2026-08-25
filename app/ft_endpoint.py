"""
Uranus U-2B – kanonikus Freqtrade endpoint feloldás.

Miért létezik ez a modul
------------------------
Az U-2A production audit kimutatta, hogy az Uranus UI processzén belül EGYSZERRE
két Freqtrade-cím élt:

* ``freqtrade_ui.py`` modulszinten olvasta a ``FT_URL`` env-et, ami a
  production ``/etc/uranus/uranus.env``-ben ``http://127.0.0.1:8017`` volt –
  ez a **Neptunus UI** portja, nem az Uranus Freqtrade-é;
* az ``app.py`` nézetépítője ugyanabban a processzben a Freqtrade
  ``config.json``-ból származtatta a helyes ``http://127.0.0.1:8090``-et.

Egy kereskedő rendszerben az, hogy melyik Freqtrade-et kérdezzük, nem
kényelmi kérdés: ebből olvassuk ki, hogy van-e nyitott pozíció.

Feloldási sorrend
-----------------
1. ``URANUS_FT_URL`` – explicit, Uranus-specifikus felülbírálás;
2. az Uranus Freqtrade ``config.json`` ``api_server`` blokkja – ez *definíció
   szerint* az Uranus Freqtrade végpontja;
3. ``FT_URL`` env – örökölt, több rendszer között megosztott név;
4. ``CANONICAL_FT_URL`` (``http://127.0.0.1:8090``).

Minden feloldott értéken lefut az idegen-végpont ellenőrzés: ha az eredmény egy
ismerten NEM Uranus szolgáltatáshoz tartozik, elvetjük és a kanonikus címre
esünk vissza. Így a 8017 implicit módon soha nem szivároghat be.
"""
from __future__ import annotations

import json
import os
from typing import Optional
from urllib.parse import urlsplit

#: Az Uranus Freqtrade REST API kanonikus címe.
CANONICAL_FT_URL = "http://127.0.0.1:8090"

#: Ismerten NEM Uranus szolgáltatások loopback-portjai. Az U-2A audit a 8017-et
#: a ``neptunus-ui.service``-hez kötötte (pid cmdline: /opt/bots/neptunus/...).
#: A 8016 az Uranus SAJÁT UI-ja – szintén nem Freqtrade, ezért idegen ebben a
#: szerepben.
FOREIGN_FT_PORTS: dict[int, str] = {
    8016: "uranus-ui (Flask, nem Freqtrade)",
    8017: "neptunus-ui (idegen rendszer)",
}

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.abspath(os.path.join(_APP_DIR, ".."))

#: Az Uranus Freqtrade konfigurációja; env-ből felülbírálható (teszt/telepítés).
DEFAULT_FT_CONFIG_PATH = os.path.join(_ROOT_DIR, "freqtrade", "user_data", "config.json")


def ft_config_path() -> str:
    return os.getenv("URANUS_FT_CONFIG", DEFAULT_FT_CONFIG_PATH)


def _normalize(url: object) -> Optional[str]:
    """Alakilag érvényes http(s) URL kanonizált alakja, különben ``None``."""
    if not url:
        return None
    text = str(url).strip().rstrip("/")
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.hostname:
        return None
    try:
        parts.port  # érvénytelen port esetén ValueError
    except ValueError:
        return None
    return text


def endpoint_port(url: str) -> Optional[int]:
    """Az URL portja, explicit port hiányában a séma alapértelmezése."""
    try:
        parts = urlsplit(url)
        if parts.port is not None:
            return parts.port
    except ValueError:
        return None
    return 443 if parts.scheme == "https" else 80


def is_foreign_endpoint(url: object) -> bool:
    """
    Igaz, ha az URL egy ismerten nem-Uranus-Freqtrade loopback szolgáltatásra
    mutat. Csak a loopback címekre alkalmazzuk: távoli hostoknál a portszám
    önmagában nem hordoz jelentést.
    """
    normalized = _normalize(url)
    if normalized is None:
        return False
    host = (urlsplit(normalized).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False
    return endpoint_port(normalized) in FOREIGN_FT_PORTS


def _from_ft_config() -> Optional[str]:
    """Az Uranus Freqtrade ``api_server`` blokkjából származtatott cím."""
    try:
        with open(ft_config_path(), "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    api = cfg.get("api_server")
    if not isinstance(api, dict) or not api.get("enabled", True):
        return None
    host = api.get("listen_ip_address") or "127.0.0.1"
    port = api.get("listen_port") or 8090
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return _normalize(f"http://{host}:{port}")


def resolve(*, explain: bool = False):
    """
    A kanonikus Uranus Freqtrade endpoint feloldása.

    Args:
        explain: ha igaz, ``(url, source, rejected)`` hármast ad vissza, ahol a
            ``rejected`` a sorra elvetett (forrás, érték, ok) hármasok listája.
    """
    rejected: list[tuple[str, str, str]] = []

    candidates = (
        ("URANUS_FT_URL", os.getenv("URANUS_FT_URL")),
        ("freqtrade_config", _from_ft_config()),
        ("FT_URL", os.getenv("FT_URL")),
    )

    for source, raw in candidates:
        if raw is None or str(raw).strip() == "":
            continue
        normalized = _normalize(raw)
        if normalized is None:
            rejected.append((source, str(raw), "invalid_url"))
            continue
        if is_foreign_endpoint(normalized):
            port = endpoint_port(normalized)
            rejected.append(
                (source, normalized, f"foreign_endpoint:{FOREIGN_FT_PORTS.get(port, port)}")
            )
            continue
        return (normalized, source, rejected) if explain else normalized

    return (CANONICAL_FT_URL, "canonical_default", rejected) if explain else CANONICAL_FT_URL


def canonical_ft_url() -> str:
    """A kanonikus Uranus Freqtrade REST alapcím (per-hívás feloldás)."""
    return resolve()
