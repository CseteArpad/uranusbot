"""
Candidate Audit Engine — FÁZIS 4.

Measures forward returns for BUY_CANDIDATE and SELL_CANDIDATE rows.
No orders, no live trading, no Binance / Freqtrade calls.

return_n:
  BUY_CANDIDATE : (close_future - close_signal) / close_signal
  SELL_CANDIDATE: (close_signal - close_future) / close_signal

winner_n = 1 if return_n > 0 else 0
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (4, 12, 24, 48)
CANDIDATE_SIGNALS = frozenset({"BUY_CANDIDATE", "SELL_CANDIDATE"})


def _normalise_ts(ts: pd.Series) -> pd.Series:
    ts = pd.to_datetime(ts)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


def audit_candidates(
    rows: list,
    df_candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    horizons: tuple = HORIZONS,
) -> pd.DataFrame:
    """
    Compute forward returns for every BUY/SELL candidate row.

    Parameters
    ----------
    rows       : enriched replay rows (list of dicts) for one symbol/tf.
                 Each row must contain 'timestamp', 'candidate_signal',
                 'signal_reason', 'next_close'.
    df_candles : original candle DataFrame with 'timestamp' and 'close'.
    symbol     : label written to the output column.
    timeframe  : label written to the output column.
    horizons   : tuple of forward-candle offsets to evaluate.

    Returns
    -------
    DataFrame — one row per BUY/SELL candidate with return_n and winner_n.
    Empty DataFrame when no candidates present.
    """
    candidates = [r for r in rows if r.get("candidate_signal") in CANDIDATE_SIGNALS]
    if not candidates:
        return pd.DataFrame()

    # ------------------------------------------------------------------ #
    # Candle lookup array
    # ------------------------------------------------------------------ #
    candles = df_candles[["timestamp", "close"]].copy()
    candles["_ts"] = _normalise_ts(candles["timestamp"])
    candles = candles.sort_values("_ts").reset_index(drop=True)
    closes   = candles["close"].to_numpy(dtype=np.float64)
    n_candles = len(closes)

    ts_map: dict[pd.Timestamp, int] = {
        pd.Timestamp(ts): i for i, ts in enumerate(candles["_ts"])
    }

    # ------------------------------------------------------------------ #
    # Candidate arrays (vectorised)
    # ------------------------------------------------------------------ #
    cdf = pd.DataFrame([{
        "timestamp":        r["timestamp"],
        "candidate_signal": r["candidate_signal"],
        "signal_reason":    r.get("signal_reason", ""),
    } for r in candidates])

    cdf["_ts"] = _normalise_ts(pd.to_datetime(cdf["timestamp"]))

    # Row timestamp → candle index i → signal close at candle i+1
    raw_idx    = np.array([ts_map.get(pd.Timestamp(ts), -2) for ts in cdf["_ts"]])
    signal_idx = raw_idx + 1
    valid_sig  = (signal_idx >= 0) & (signal_idx < n_candles)

    close_sig = np.where(
        valid_sig,
        closes[np.clip(signal_idx, 0, n_candles - 1)],
        np.nan,
    )

    cdf["symbol"]         = symbol
    cdf["timeframe"]      = timeframe
    cdf["close_at_signal"] = np.round(close_sig, 6)

    is_buy = (cdf["candidate_signal"] == "BUY_CANDIDATE").to_numpy()

    for n in horizons:
        future_idx   = signal_idx + n
        future_valid = valid_sig & (future_idx < n_candles)

        close_fut = np.where(
            future_valid,
            closes[np.clip(future_idx, 0, n_candles - 1)],
            np.nan,
        )

        ret_buy  = (close_fut - close_sig) / close_sig
        ret_sell = (close_sig - close_fut) / close_sig
        ret      = np.where(is_buy, ret_buy, ret_sell)
        ret      = np.where(future_valid, np.round(ret, 6), np.nan)

        cdf[f"return_{n}"] = ret
        cdf[f"winner_{n}"] = np.where(
            ~np.isnan(ret), (ret > 0).astype(np.float64), np.nan
        )

    out_cols = [
        "timestamp", "symbol", "timeframe",
        "candidate_signal", "signal_reason", "close_at_signal",
        "return_4",  "return_12",  "return_24",  "return_48",
        "winner_4",  "winner_12",  "winner_24",  "winner_48",
    ]
    return cdf[out_cols]


def build_performance_summary(audit_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate return / winrate per candidate_signal type across all rows.

    Returns a DataFrame with columns:
      candidate_signal, count,
      avg_return_4/12/24/48, winrate_4/12/24/48
    """
    if audit_df.empty:
        return pd.DataFrame()

    records = []
    for sig, grp in audit_df.groupby("candidate_signal"):
        rec = {"candidate_signal": sig, "count": len(grp)}
        for n in HORIZONS:
            valid_ret = grp[f"return_{n}"].dropna()
            valid_win = grp[f"winner_{n}"].dropna()
            rec[f"avg_return_{n}"] = (
                round(float(valid_ret.mean()), 6) if not valid_ret.empty else ""
            )
            rec[f"winrate_{n}"] = (
                round(float(valid_win.mean()), 6) if not valid_win.empty else ""
            )
        records.append(rec)

    cols = [
        "candidate_signal", "count",
        "avg_return_4", "avg_return_12", "avg_return_24", "avg_return_48",
        "winrate_4",    "winrate_12",    "winrate_24",    "winrate_48",
    ]
    return pd.DataFrame(records, columns=cols)
