"""
Uranus U-2B – kanonikus végrehajtási alapértelmezések.

Miért létezik ez a modul
------------------------
Az U-2A production audit kimutatta, hogy a végrehajtási posztúrát ma nem a
konfiguráció, hanem a *forráskód alapértelmezései* tartják: a runner
környezetében egyetlen execution-flag sincs beállítva, tehát minden érték a
kódbeli defaultból jön. Ugyanakkor a UI oldalán ezek az alapértelmezések az
ELLENKEZŐJÜKRE voltak állítva (``execution_enabled=True``,
``execution_log_only=False``), így egy hiányzó konfiguráció a UI szemszögéből
"éles végrehajtás"-t jelentett.

Ez a modul egyetlen helyre vonja össze a kanonikus értékeket, hogy a runner és
a UI ne tudjon szétcsúszni. **Egyetlen szabály:** hiányzó, üres vagy
értelmezhetetlen konfiguráció SOHA nem eredményezhet éles végrehajtást.

A modul szándékosan függőségmentes (csak ``os``), hogy bárhonnan importálható
legyen anélkül, hogy körkörös importot vagy mellékhatást hozna be.
"""
from __future__ import annotations

import os

#: Éles order csak akkor mehet ki, ha ezt valaki EXPLICITEN bekapcsolta.
EXECUTION_ENABLED_DEFAULT: bool = False

#: Alapértelmezésben a döntés megszületik és naplózódik, de API-hívás nincs.
EXECUTION_LOG_ONLY_DEFAULT: bool = True

#: Végrehajtás után alapértelmezésben visszaellenőrizzük a nyitott trade-eket.
EXECUTION_CONFIRM_DEFAULT: bool = True

#: Igaznak számító env-értékek. Bármi más (üres, "maybe", None) HAMIS.
_TRUE_TOKENS = ("1", "true", "yes", "on")


def env_bool(name: str, default: bool) -> bool:
    """
    Env-flag olvasása fail-safe módon.

    A hiányzó vagy értelmezhetetlen érték a megadott ``default``-ra esik vissza;
    igazra KIZÁRÓLAG a ``_TRUE_TOKENS`` egyike értékel ki. Ez azt jelenti, hogy
    egy elgépelt ``EXECUTION_ENABLED=ture`` nem kapcsol be semmit.
    """
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in _TRUE_TOKENS


def coerce_bool(value, default: bool) -> bool:
    """
    Tetszőleges (UI/JSON eredetű) érték biztonságos bool-lá alakítása.

    ``None`` és hiányzó érték esetén a ``default`` jön vissza. Stringnél csak a
    ``_TRUE_TOKENS`` ad igazat – így egy hibás vagy hiányzó bemenet nem tud
    "igaz" irányba dőlni.
    """
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in _TRUE_TOKENS


def runtime_execution_flags() -> tuple[bool, bool, bool]:
    """A runner által használt effektív (enabled, log_only, confirm) hármas."""
    return (
        env_bool("EXECUTION_ENABLED", EXECUTION_ENABLED_DEFAULT),
        env_bool("EXECUTION_LOG_ONLY", EXECUTION_LOG_ONLY_DEFAULT),
        env_bool("EXECUTION_CONFIRM", EXECUTION_CONFIRM_DEFAULT),
    )


def safe_settings_defaults() -> dict:
    """
    A UI beállítás-űrlap kanonikus, biztonságos kiinduló értékei.

    Ezeket a ``read_trading_settings()`` használja, MIELŐTT a systemd drop-in
    tartalmát ráolvasná. Ha a drop-in nem rendelkezik a végrehajtásról, ez az
    érték marad érvényben – és ez tiltó irányú.
    """
    return {
        "execution_enabled": EXECUTION_ENABLED_DEFAULT,
        "execution_log_only": EXECUTION_LOG_ONLY_DEFAULT,
    }
