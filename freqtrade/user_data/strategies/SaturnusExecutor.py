# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from freqtrade.strategy import IStrategy
from pandas import DataFrame


class UranusExecutor(IStrategy):
    """
    EXECUTOR ONLY STRATEGY
    - Nem számol saját logikát, nem használ indikátorokat.
    - A Uranus app által frissített state.json alapján ad belépési/kilépési jelet.
    - Stoploss védőháló: -10%.
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
        pair = metadata.get("pair", "")

        dataframe["enter_long"] = 0

        state = self._load_json(self.STATE_JSON_PATH)
        sig = self._get_pair_signal(state, pair)

        if sig["action"] == "BUY" and not self._already_processed(pair, sig["id"], "BUY"):
            # Egyetlen jel = egy végrehajtás (one-shot)
            dataframe.loc[dataframe.index[-1], "enter_long"] = 1
            self._mark_processed(pair, sig["id"], "BUY")

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

