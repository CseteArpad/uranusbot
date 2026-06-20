from __future__ import annotations

from freqtrade.strategy import IStrategy
from datetime import datetime, timezone
from pathlib import Path
import logging
import json

# Opcionális, de erősen ajánlott: ha van nyitott trade Freqtrade-ben,
# akkor NE küldjünk új BUY jelet akkor sem, ha az UI state még nem frissült.
try:
    from freqtrade.persistence import Trade
except Exception:
    Trade = None  # fallback

logger = logging.getLogger(__name__)

# A Flask UI ide írja az állapotot
STATE_PATH = Path("/opt/bots/uranus/app/state.json")

# --- 6 szabály paraméterei (a leírásod szerint) ---
SELL_PEAK_DD_PCT = 0.03       # 1) 3% esés a csúcshoz képest
SELL_PROFIT_TARGET = 0.03     # 2) 3% profitot nem érte el (peak < buy*(1+0.03))
SELL_FALLBACK_PCT = 0.001     #    de visszaesik 100,1%-ra (buy*1.001)
SELL_PANIC_PCT = 0.01         # 3) vételi ár 99%-ára csökken (panic sell)

BUY_REBOUND_PCT = 0.03        # 4) eladás után 3% emelkedés a mélyponthoz képest
BUY_RECOVER_PCT = 0.999       # 5) enyhe csökkenés után eléri a 99,9%-ot (sell*0.999)
BUY_IMMEDIATE_PCT = 1.01      # 6) azonnali emelkedés 101%-ra (sell*1.01)

STATE_MAX_AGE_SEC = 180

# Szabály-azonosítók magyar megnevezéssel (UI-hoz / trade taghez)
_RULES_HU = {
    1: "ELADÁS #1 – 3% esés a csúcshoz képest",
    2: "ELADÁS #2 – nem volt meg a +3%, visszaesett 100,1%-ra",
    3: "ELADÁS #3 – pánik: vételi ár 99%-ára csökkent",
    4: "VÉTEL #4 – eladás után +3% emelkedés a mélyponthoz képest",
    5: "VÉTEL #5 – enyhe csökkenés után eléri a 99,9%-ot",
    6: "VÉTEL #6 – azonnali emelkedés 101%-ra",
}
       # ha ennél régebbi a state, nem kereskedünk


def _parse_utc(ts) -> datetime | None:
    """
    Elfogad:
      - ISO string (pl. 2026-01-03T20:37:01Z)
      - "Z" végződés is ok
      - "AUTO" / üres / hibás -> None
    """
    if not ts or not isinstance(ts, str):
        return None
    if ts.strip().upper() == "AUTO":
        return None
    try:
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _read_ui_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("STATE READ ERROR: %s", e)
    return {}


def _num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _is_true(v) -> bool:
    if v is True:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "y", "on"):
        return True
    if isinstance(v, (int, float)) and v != 0:
        return True
    return False


def _has_open_trade_for_pair(pair: str) -> bool:
    """
    Ha Freqtrade-ben már van nyitott trade erre a párra, akkor tekintsük úgy,
    hogy poziban vagyunk (BUY tiltás).
    """
    if not pair:
        return False
    if Trade is None:
        return False
    try:
        for t in Trade.get_open_trades():
            if getattr(t, "pair", None) == pair:
                return True
    except Exception:
        return False
    return False


class UranusUiStateStrategy(IStrategy):
    timeframe = "1m"
    can_short = False

    # dry-run / teszt paraméterek (nem a 6 szabály része)
    minimal_roi = {"0": 0.01}
    stoploss = -0.10
    startup_candle_count = 10

    def populate_indicators(self, dataframe, metadata: dict):
        # Itt csak állapotolvasás logolás (vizualizációhoz)
        state = _read_ui_state()
        logger.warning("UI STATE LOADED: %s", state)
        return dataframe

    def populate_entry_trend(self, dataframe, metadata: dict):
        state = _read_ui_state()

        pair = (metadata or {}).get("pair") or state.get("pair") or ""
        last = float(dataframe["close"].iloc[-1])

        # 1) state frissesség ellenőrzés
        dt = _parse_utc(state.get("updated_utc"))
        if dt is not None:
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > STATE_MAX_AGE_SEC:
                logger.warning("UI STATE TOO OLD (age=%ss) -> NO BUY", int(age))
                return dataframe

        # 2) Pozíció-ellenőrzés (UI az elsődleges, Freqtrade open-trade guard a másodlagos)
        in_position_ui = _is_true(state.get("in_position"))
        if in_position_ui:
            return dataframe
        if _has_open_trade_for_pair(pair):
            logger.warning("OPEN TRADE DETECTED by Freqtrade -> NO BUY (pair=%s)", pair)
            return dataframe

        # 3) BUY szabályok (csak akkor engedjük, ha NEM vagyunk poziban)
        dataframe["enter_long"] = 0

        sell_price = _num(state.get("sell_price"))
        low_price = _num(state.get("low_price"))

        rule = None

        # 6) azonnali emelkedés 101%-ra (sell_price * 1.01)
        if sell_price and last >= sell_price * BUY_IMMEDIATE_PCT:
            rule = 6

        # 4) eladás után 3% emelkedés a mélyponthoz képest (low_price * 1.03)
        elif low_price and last >= low_price * (1 + BUY_REBOUND_PCT):
            rule = 4

        # 5) eladás utáni enyhe csökkenés után eléri a 99,9%-ot (sell_price*0.999)
        elif sell_price and low_price:
            thr = sell_price * BUY_RECOVER_PCT
            # "enyhe csökkenés után" minimum: low_price <= thr
            # majd "eléri": last >= thr
            if low_price <= thr and last >= thr:
                rule = 5

        if rule is not None:
            dataframe.loc[dataframe.index[-1], "enter_long"] = 1
            logger.warning("BUY SIGNAL rule=%s last=%s", rule, last)

        return dataframe

    def populate_exit_trend(self, dataframe, metadata: dict):
        state = _read_ui_state()

        pair = (metadata or {}).get("pair") or state.get("pair") or ""
        last = float(dataframe["close"].iloc[-1])

        # 1) state frissesség ellenőrzés
        dt = _parse_utc(state.get("updated_utc"))
        if dt is not None:
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > STATE_MAX_AGE_SEC:
                logger.warning("UI STATE TOO OLD (age=%ss) -> NO SELL", int(age))
                return dataframe

        # 2) Pozíció-ellenőrzés (UI az elsődleges, Freqtrade open-trade guard a másodlagos)
        in_position_ui = _is_true(state.get("in_position"))
        has_open = _has_open_trade_for_pair(pair)

        # SELL csak akkor, ha tényleg van pozíció (UI vagy Freqtrade szerint)
        if not in_position_ui and not has_open:
            dataframe["exit_long"] = 0
            return dataframe

        dataframe["exit_long"] = 0

        buy = _num(state.get("base_price"))
        peak = _num(state.get("peak_price"))

        rule = None

        # 3) panic sell: vételi ár 99%-ára csökken
        if buy and last <= buy * (1 - SELL_PANIC_PCT):
            rule = 3

        # 1) 3% esés a csúcshoz képest
        elif peak and last <= peak * (1 - SELL_PEAK_DD_PCT):
            rule = 1

        # 2) nem érte el a 3% nyereséget (peak < buy*1.03), de visszaesik 100.1%-ra
        elif buy and peak and peak < buy * (1 + SELL_PROFIT_TARGET) and last <= buy * (1 + SELL_FALLBACK_PCT):
            rule = 2

        if rule is not None:
            dataframe.loc[dataframe.index[-1], "exit_long"] = 1
            logger.warning("SELL SIGNAL rule=%s last=%s", rule, last)

        return dataframe
