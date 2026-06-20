# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement

from datetime import timedelta
from pandas import DataFrame

from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade


class UranusSixRules(IStrategy):
    """
    DEBUG STRATEGY - csak teszthez!
    Cél: dry-runban biztosan legyen trade -> trades.sqlite létrejön.

    - Entry: mindig (startup_candle_count után)
    - Exit: trade nyitás után ~1 perc múlva mindig
    """

    # Alapok
    timeframe = "1m"
    startup_candle_count = 30
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True
    use_custom_stoploss = False

    can_short = False

    # Ne ROI/stoploss zárjon, mi kézzel zárunk custom_exit-tel
    minimal_roi = {"0": 999}
    stoploss = -0.10

    # Kötelező meta (a te configod úgyis felülírhatja, de maradjon tiszta)
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
        "force_entry": "market",
        "force_exit": "market",
        "emergency_exit": "market",
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Nem számolunk indikátort, itt most a cél csak: legyen trade
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # TEST – FORCE ENTRY
        dataframe["enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit jelzést nem adunk itt, custom_exit intézi idő alapján
        dataframe["exit_long"] = 0
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        # Biztos zárás: nyitás után 1 perc elteltével zárjuk
        if current_time - trade.open_date_utc >= timedelta(minutes=1):
            return "debug_exit_1min"
        return None
