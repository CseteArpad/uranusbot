# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from freqtrade.strategy import IStrategy
from pandas import DataFrame

logger = logging.getLogger(__name__)


# --- U-2B: ONE LIVE EXECUTION AUTHORITY --------------------------------------
# A stratégia NEM generálhat önálló belépési jelet. Az éles belépés egyetlen
# megengedett útja a kontrollált Uranus végrehajtási lánc (freshness gate ->
# guardrail -> kill switch -> force_enter). Ez a kapcsoló szándékosan
# MODUL-KONSTANS és nem env-vezérelt: a jel-alapú belépés nem kapcsolható
# vissza konfigurációval, csak kódváltoztatással és külön review-val.
SIGNAL_BASED_ENTRY_ENABLED = False


# --- U-0.4: signal freshness -------------------------------------------------
# A strategy egyetlen bemenete a state.json-ból érkező jel. Ha az elavult vagy
# időbélyeg nélküli, a belépés FAIL-CLOSED módon elmarad: egy régi, ottfelejtett
# BUY jel újraindítás után nem indíthat valódi vételt.
# Szándékosan EGYETLEN, jól definiált paraméter; nem a state `updated_utc`-jét
# használjuk signal-frissességre, mert az a runner tick ideje, nem a jelé.
def signal_max_age_sec() -> float:
    try:
        return float(os.getenv("SIGNAL_MAX_AGE_SEC", "300"))
    except (TypeError, ValueError):
        return 300.0


def parse_signal_ts(value: Any) -> float | None:
    """
    Toleráns jel-időbélyeg értelmezés -> epoch másodperc.

    Elfogad epoch számot (másodperc vagy ezredmásodperc, stringként is) és
    ISO-8601 alakot ('Z', offset, vagy naiv = UTC). Bármi más -> None.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return None
        return num / 1000.0 if num >= 1e11 else num

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return parse_signal_ts(float(text))
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


def signal_is_fresh(ts_value: Any, now: float | None = None,
                    max_age: float | None = None) -> tuple[bool, str]:
    """
    (friss?, ok) – fail-closed: hiányzó vagy értelmezhetetlen időbélyeg NEM friss.
    """
    limit = signal_max_age_sec() if max_age is None else float(max_age)
    parsed = parse_signal_ts(ts_value)
    if parsed is None:
        return False, "SIGNAL_TS_MISSING_OR_INVALID"
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    age = current - parsed
    if age > limit:
        return False, f"SIGNAL_STALE age={int(age)}s max={int(limit)}s"
    return True, "OK"


class UranusExecutor(IStrategy):
    """
    PASSZÍV EXECUTOR STRATEGY (U-2B óta)

    - Nem számol saját logikát, nem használ indikátorokat.
    - **Nem ad belépési jelet** és **nem ad kilépési jelet.** Mindkét irány
      kizárólag a kontrollált Uranus végrehajtási láncból, a Freqtrade
      ``/api/v1/forceenter`` és ``/api/v1/forceexit`` végpontjain keresztül
      keletkezhet – azokat a force-utakat ez a stratégia nem befolyásolja.
    - Ami MEGMARAD a stratégia felelősségének: a Freqtrade-szintű stoploss
      védőháló (-10%), ami a jel-oszlopoktól függetlenül működik.
    """

    # --- Freqtrade kötelező / alap paraméterek ---
    timeframe = "1m"
    can_short = False

    # Védőháló (freqtrade stoploss) - ha a 6 szabályos rendszer/infra összeomlik
    stoploss = -0.10

    # Minimal ROI: ne legyen ROI miatti automata zárás
    minimal_roi = {"0": 10}

    # Trailing kikapcsolva (a logika a Uranus app-ban van)
    trailing_stop = False

    # Csak új gyertyán értékeljen (stabilabb, determinisztikusabb)
    process_only_new_candles = True
    startup_candle_count = 10

    # --- Uranus állományok helye ---
    # Kanonikus state.json az app oldalon
    STATE_JSON_PATH = "/opt/bots/uranus/app/state.json"

    # Executor saját “last processed” nyilvántartása (hogy ne ismételje a jeleket)
    EXECUTOR_STATE_PATH = "/opt/bots/uranus/freqtrade/user_data/.uranus_executor_state.json"

    def informative_pairs(self):
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Nincs indikátor.
        return dataframe

    # ----------------- helpers -----------------

    def _load_json(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _save_json(self, path: str, data: Dict[str, Any]) -> None:
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _get_pair_signal(self, state: Dict[str, Any], pair: str) -> Dict[str, Any]:
        """
        Elvárt jel formák (rugalmas, de determinisztikus):
        A) state["signals"][PAIR] = {"action":"BUY|SELL|NONE", "id":"...", "ts":"..."}
        B) state["signal"] = {"pair":"...", "action":"BUY|SELL|NONE", "id":"...", "ts":"..."}
        Ha nincs jel: NONE.
        """
        sig = {}

        signals = state.get("signals")
        if isinstance(signals, dict) and isinstance(signals.get(pair), dict):
            sig = signals.get(pair, {}) or {}

        if not sig:
            root = state.get("signal")
            if isinstance(root, dict) and root.get("pair") == pair:
                sig = root

        action = (sig.get("action") or "NONE").upper()
        sig_id = str(sig.get("id") or "")
        sig_ts = str(sig.get("ts") or "")

        return {"action": action, "id": sig_id, "ts": sig_ts}

    def _already_processed(self, pair: str, sig_id: str, action: str) -> bool:
        if not sig_id:
            return False
        ex = self._load_json(self.EXECUTOR_STATE_PATH)
        last = ex.get(pair, {})
        return (
            isinstance(last, dict)
            and last.get("id") == sig_id
            and last.get("action") == action
        )

    def _mark_processed(self, pair: str, sig_id: str, action: str) -> None:
        if not sig_id:
            return
        ex = self._load_json(self.EXECUTOR_STATE_PATH)
        if not isinstance(ex, dict):
            ex = {}
        ex[pair] = {"id": sig_id, "action": action, "processed_at": self._now_iso()}
        self._save_json(self.EXECUTOR_STATE_PATH, ex)

    # ----------------- signals -----------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        U-2B: a stratégia SOHA nem generál belépési jelet.

        Korábban ez a metódus a ``state.json``-ból olvasott ``BUY`` jelre maga
        állított ``enter_long=1``-et. Az így keletkező order **megkerülte** a
        teljes Uranus végrehajtási kapuláncot: ``EXECUTION_ENABLED``,
        ``EXECUTION_LOG_ONLY``, a runtime freshness gate, a guardrailek és a
        kill switch egyike sem érintette. Hogy ez a gyakorlatban nem sült el,
        annak egyetlen oka volt: a jelet soha senki nem írta a state-be. Ez
        hiányzó funkció, nem biztonsági kontroll.

        Az éles belépés innentől KIZÁRÓLAG a kontrollált Uranus úton keletkezhet
        (``tick_runner.maybe_execute_via_api`` -> ``FreqtradeExecutor.force_enter``
        -> Freqtrade ``/api/v1/forceenter``). A force entry a Freqtrade-ben nem
        a stratégia belépési jelén keresztül megy, ezért a runner szerződését ez
        a változtatás nem érinti.
        """
        pair = metadata.get("pair", "")

        # Kanonikus kimenet: nincs stratégiai belépés, semmilyen bemenetre.
        dataframe["enter_long"] = 0

        if SIGNAL_BASED_ENTRY_ENABLED:  # pragma: no cover - konstans False
            raise RuntimeError(
                "SIGNAL_BASED_ENTRY_ENABLED=True: a jel-alapú belépés az U-2B "
                "óta tiltott (ONE LIVE EXECUTION AUTHORITY)."
            )

        # Bizonyítéknyom: ha valaha megjelenik egy jelíró, azt látni akarjuk.
        # Az olvasás kizárólag naplózásra szolgál, semmilyen kimenetet nem
        # befolyásol, és a feldolgozott-jelölést sem írja.
        try:
            state = self._load_json(self.STATE_JSON_PATH)
            sig = self._get_pair_signal(state, pair)
            if sig["action"] in ("BUY", "SELL"):
                fresh, fresh_reason = signal_is_fresh(sig["ts"])
                logger.warning(
                    "URANUS SIGNAL IGNORED (U-2B strategy is entry-passive) "
                    "pair=%s action=%s id=%s fresh=%s reason=%s",
                    pair, sig["action"], sig["id"], fresh, fresh_reason,
                )
        except Exception as exc:  # a naplózás soha nem törhet meg egy tickel
            logger.warning("URANUS SIGNAL AUDIT READ FAILED: %s", exc)

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Canonical Uranus mode:
        Freqtrade strategy MUST NOT create independent SELL/exit signals.
        SELL is executed only by /opt/bots/uranus/app/tick_runner.py via force_exit
        after Uranus v2.1 engine decision + execution guards.
        """
        dataframe["exit_long"] = 0
        dataframe["exit_tag"] = None
        return dataframe

