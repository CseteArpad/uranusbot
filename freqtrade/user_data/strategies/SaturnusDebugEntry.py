from freqtrade.strategy import IStrategy
import pandas as pd
import logging
import json
from pathlib import Path


logger = logging.getLogger(__name__)
STATE_PATH = Path("/opt/bots/uranus/app/state.json")


def read_ui_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("STATE READ ERROR: %s", e)
    return {}


class UranusDebugEntry(IStrategy):
    timeframe = "1m"
    can_short = False

    minimal_roi = {"0": 0.01}
    stoploss = -0.10
    startup_candle_count = 10

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        state = read_ui_state()
        logger.warning("UI STATE LOADED: %s", state)
        logger.warning("DEBUG populate_indicators HIT - pair=%s rows=%s", metadata.get("pair"), len(dataframe))
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        logger.warning("DEBUG populate_entry_trend HIT - pair=%s rows=%s", metadata.get("pair"), len(dataframe))
        dataframe["enter_long"] = 0
        if len(dataframe) > 0:
            dataframe.loc[dataframe.index[-1], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        logger.warning("DEBUG populate_exit_trend HIT - pair=%s rows=%s", metadata.get("pair"), len(dataframe))
        dataframe["exit_long"] = 0
        return dataframe
