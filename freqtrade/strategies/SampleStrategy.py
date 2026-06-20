from freqtrade.strategy import IStrategy
from pandas import DataFrame

class SampleStrategy(IStrategy):
    pair_whitelist = ["SOL/USDC"]

    timeframe = "15m"
    minimal_roi = {"0": 0.01}
    stoploss = -0.10

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
