"""
Uranus U-0.4 – runtime freshness és startup execution gate.

Miért létezik ez a modul
------------------------
A runner a state.json-ból perzisztált ``base`` horgonyt használja árszint-
számításhoz, de indításkor csak a *piaci árat* frissíti, a horgonyt nem.
Egy hónapokkal korábbi ``base`` mellett a friss ár triviálisan átlépi a
``catastrophe_buy_level = base * 1.10`` küszöböt, és a végrehajtási láncban
korábban semmilyen frissesség-ellenőrzés nem volt.

Ez a modul két, egymástól független dolgot ad:

1. **Startup execution gate** – amíg a *jelenlegi processz* nem bizonyított
   friss piaci adatot ÉS nem horgonyozta újra az állapotot, addig valódi
   végrehajtás nem történhet, akkor sem, ha ``EXECUTION_ENABLED=1``.
2. **Kanonikus tick-timestamp** – egyetlen ``now`` értékből származik az
   összes tick-szintű időbélyeg, hogy ne legyen négy, néhány ezredmásodperccel
   eltérő időpont ugyanarra a tickre.

Alapelv: **fail-closed**. Ha a frissesség nem bizonyítható, a gate tilt.

Az állapot szándékosan **processz-lokális** (modulszintű), nem perzisztált:
pontosan ez teszi lehetővé, hogy egy újraindítás után a gate újra zárva
induljon, függetlenül attól, mi van a state.json-ban.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

#: A megengedett gyertyakor timeframe-szorzója. 3 = elbír egy-két kimaradt
#: gyertyát, de egy tartósan megszakadt adatfolyamot már tilt.
TIMEFRAME_MULTIPLIER = 3.0

#: Alsó korlát a szállítási/óra-eltérés kezelésére kis timeframe-en.
TRANSPORT_TOLERANCE_SEC = 90.0

#: Ha a timeframe nem ismerhető fel, konzervatív fix korlát.
UNKNOWN_TIMEFRAME_MAX_AGE_SEC = 300.0


def configured_max_age_sec() -> Optional[float]:
    """
    Az üzemeltetői felülbírálás, vagy ``None``, ha nincs / érvénytelen / <= 0.

    Fontos biztonsági döntés: **nincs olyan konfigurációs érték, amely
    kikapcsolná a kor-ellenőrzést.** A korábbi ``<= 0 -> kikapcsol`` viselkedés
    egy konfigurációból elérhető bypass volt, ami sértette a
    "NO LIVE EXECUTION WITH UNBOUNDED MARKET STALENESS" alapelvet: hibás vagy
    rosszindulatú beállítással hónapokkal régi gyertyán is indulhatott volna
    valódi order. Érvénytelen érték esetén a származtatott, biztonságos
    alapértelmezés lép életbe (fail-closed irány).
    """
    raw = os.getenv("MARKET_DATA_MAX_AGE_SEC")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# Blokkolási okok – strukturált, naplóban és state-ben is ez jelenik meg.
REASON_OK = "OK"
REASON_NOT_ARMED = "STARTUP_NOT_ARMED"
REASON_STALE_MARKET = "MARKET_DATA_STALE"
REASON_BAD_TIMESTAMP = "MARKET_DATA_TIMESTAMP_INVALID"
REASON_NOT_RECONCILED = "STALE_STATE_BLOCKED"

# --------------------------------------------------------------------------- #
# Processz-lokális állapot
# --------------------------------------------------------------------------- #

_fetch_ok: bool = False
_last_candle_ts: Optional[float] = None
_last_fetch_monotonic: Optional[float] = None
_timeframe_sec: Optional[float] = None
_reconciled: bool = False


def reset_process_state() -> None:
    """Csak teszthez: visszaállítja a processz-lokális gate-állapotot."""
    global _fetch_ok, _last_candle_ts, _last_fetch_monotonic, _timeframe_sec, _reconciled
    _fetch_ok = False
    _last_candle_ts = None
    _last_fetch_monotonic = None
    _timeframe_sec = None
    _reconciled = False


# --------------------------------------------------------------------------- #
# Időbélyeg-segédfüggvények
# --------------------------------------------------------------------------- #

def utc_iso(epoch: Optional[float] = None) -> str:
    """Kanonikus ISO-8601 UTC alak, 'Z' végződéssel."""
    ts = time.time() if epoch is None else float(epoch)
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_ts(value: Any) -> Optional[float]:
    """
    Toleráns timestamp-értelmezés -> epoch másodperc.

    Elfogad: int/float epoch (másodperc vagy ezredmásodperc), ISO-8601 stringet
    'Z'-vel, offsettel vagy naiv alakban (utóbbit UTC-nek tekintve).
    Bármi más esetén ``None`` (a hívó ilyenkor fail-closed módon jár el).
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        # Ezredmásodperces epoch felismerése (kb. 2001 utáni másodperc-epoch
        # sosem éri el az 1e11-et, az ms-epoch viszont már 1970 óta felette van).
        if num >= 1e11:
            num /= 1000.0
        return num

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return parse_ts(float(text))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    return None


def timeframe_to_seconds(timeframe: Any) -> Optional[float]:
    """'1m' -> 60, '4h' -> 14400, '1d' -> 86400. Ismeretlen alakra None."""
    if not isinstance(timeframe, str) or len(timeframe) < 2:
        return None
    unit = timeframe[-1].lower()
    try:
        amount = float(timeframe[:-1])
    except ValueError:
        return None
    factor = {"m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}.get(unit)
    if factor is None or amount <= 0:
        return None
    return amount * factor


def stamp_tick_timestamps(state: dict, epoch: Optional[float] = None) -> str:
    """
    Kanonikus tick-időbélyegzés: EGY ``now`` érték, több mezőnév.

    ``updated_utc`` a kanonikus mező; az ``updated_at`` és a ``time``
    backward-compatibility aliasok, ``tick_ts`` ugyanennek epoch alakja.
    Szemantika: "a legutóbbi *sikeres* tick befejezésének ideje" – hibaágon
    ezért nem hívjuk.
    """
    ts = time.time() if epoch is None else float(epoch)
    iso = utc_iso(ts)
    state["updated_utc"] = iso
    state["updated_at"] = iso
    state["time"] = iso
    state["tick_ts"] = int(ts)
    return iso


# --------------------------------------------------------------------------- #
# Gate: market fetch
# --------------------------------------------------------------------------- #

def mark_market_fetch_ok(candle_ts: Any, timeframe: Any = None) -> None:
    """
    Sikeres, aktuális piaci adatlekérés rögzítése a jelenlegi processzben.

    A ``candle_ts`` a legfrissebb gyertya nyers időbélyege (bármely támogatott
    alakban). Értelmezhetetlen időbélyeg esetén a fetch tényét rögzítjük, de a
    gate a kor-ellenőrzésnél fail-closed módon tiltani fog.
    """
    global _fetch_ok, _last_candle_ts, _last_fetch_monotonic, _timeframe_sec
    _fetch_ok = True
    _last_candle_ts = parse_ts(candle_ts)
    _last_fetch_monotonic = time.monotonic()
    _timeframe_sec = timeframe_to_seconds(timeframe)


def mark_market_fetch_failed() -> None:
    """Sikertelen adatlekérés: a friss-adat bizonyíték elveszik."""
    global _fetch_ok, _last_candle_ts
    _fetch_ok = False
    _last_candle_ts = None


def mark_reconciled() -> None:
    """A startup állapot-újrahorgonyzás lefutott ebben a processzben."""
    global _reconciled
    _reconciled = True


def is_reconciled() -> bool:
    return _reconciled


def effective_max_age_sec() -> float:
    """
    A megengedett gyertyakor – **mindig pozitív**, sosem kikapcsolható.

    Az alapértelmezés a timeframe-ből származik, nem fix érték: egy 1 perces
    timeframe-en egy 15 perces gyertya már törött adatfolyamot jelent, míg egy
    4 órás timeframe-en a legfrissebb gyertya nyitóideje jogosan órákkal régi.
    Ezért::

        alapértelmezés = max(timeframe * 3, 90 s)

    Explicit felülbírálás esetén az üzemeltetői érték érvényesül, de sosem
    eshet a timeframe kétszerese alá – különben egy örökölt kis érték nagy
    timeframe-en hamisan tiltana.
    """
    if _timeframe_sec:
        derived = max(_timeframe_sec * TIMEFRAME_MULTIPLIER, TRANSPORT_TOLERANCE_SEC)
    else:
        derived = UNKNOWN_TIMEFRAME_MAX_AGE_SEC

    override = configured_max_age_sec()
    if override is None:
        return derived

    floor = _timeframe_sec * 2.0 if _timeframe_sec else 0.0
    return max(override, floor)


def execution_gate_status(now: Optional[float] = None) -> Tuple[bool, str]:
    """
    A valódi végrehajtás előtti hard gate.

    Returns:
        (engedélyezett, ok) – az ok gépi feldolgozásra alkalmas konstans.
    """
    if not _fetch_ok:
        return False, REASON_NOT_ARMED

    # A kor-ellenőrzés MINDIG fut: nincs konfigurációs út, amely megkerülné.
    if _last_candle_ts is None:
        return False, REASON_BAD_TIMESTAMP
    current = time.time() if now is None else float(now)
    if (current - _last_candle_ts) > effective_max_age_sec():
        return False, REASON_STALE_MARKET

    if not _reconciled:
        return False, REASON_NOT_RECONCILED

    return True, REASON_OK


def gate_snapshot(now: Optional[float] = None) -> dict:
    """Diagnosztikai pillanatkép a state/UI számára (nem vezérel semmit)."""
    ok, reason = execution_gate_status(now=now)
    current = time.time() if now is None else float(now)
    age = None if _last_candle_ts is None else round(current - _last_candle_ts, 3)
    return {
        "armed": bool(ok),
        "reason": reason,
        "market_fetch_ok": bool(_fetch_ok),
        "reconciled": bool(_reconciled),
        "last_candle_age_sec": age,
        "max_age_sec": effective_max_age_sec(),
    }
