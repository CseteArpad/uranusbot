"""
Research Dimension Engine — FÁZIS 6A.

Dimensional breakdown of candidate performance:
  - by symbol
  - by timeframe
  - by symbol × timeframe
  - by signal_reason

Input: the enriched research DataFrame produced by research_engine.
enrich_with_forward_closes() — already has close_at_signal + close_future_N.

Return direction per row:
  BUY_CANDIDATE  → (close_future - close_signal) / close_signal
  SELL_CANDIDATE → (close_signal - close_future) / close_signal
  NEUTRAL rows   → excluded from all dimension performance reports.

No orders. No live trading. No Binance / Freqtrade calls.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS: tuple = (4, 12, 24, 48)

_PERF_COLS_ALL = [
    "count",
    "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
    "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
]

_BEST_DIM_COLS = [
    "dimension_type", "dimension_value",
    "count",
    "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
    "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_directional_returns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised: add return_N columns based on candidate_signal direction.
    Rows with candidate_signal not in {BUY_CANDIDATE, SELL_CANDIDATE} are kept
    but return_N = NaN so they fall out of downstream .dropna() calls.
    """
    df = df.copy()
    sig  = df["close_at_signal"].to_numpy(dtype=np.float64)
    is_buy = (df["candidate_signal"] == "BUY_CANDIDATE").to_numpy()

    for h in HORIZONS:
        fut   = df[f"close_future_{h}"].to_numpy(dtype=np.float64)
        valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)
        ret   = np.where(
            is_buy,
            (fut - sig) / sig,
            (sig - fut) / sig,
        )
        df[f"return_{h}"] = np.where(valid, np.round(ret, 6), np.nan)

    return df


def _agg_stats(df: pd.DataFrame) -> dict:
    """Return stats dict for one group."""
    rec: dict = {"count": len(df)}
    for h in HORIZONS:
        vr = df[f"return_{h}"].dropna().to_numpy(dtype=np.float64)
        rec[f"avg_return_{h}"] = round(float(vr.mean()), 6) if len(vr) else ""
        rec[f"winrate_{h}"]    = round(float((vr > 0).mean()), 6) if len(vr) else ""
    return rec


def _groupby_dimension(
    df: pd.DataFrame,
    dim_col: str | list[str],
    dim_label: str | list[str],
) -> pd.DataFrame:
    """
    Group df by dim_col, compute stats, return tidy DataFrame.
    dim_col may be a single column name or a list for multi-column grouping.
    dim_label defines the output column name(s).
    """
    if isinstance(dim_col, str):
        dim_col   = [dim_col]
        dim_label = [dim_label]

    records = []
    for keys, grp in df.groupby(dim_col, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        rec = dict(zip(dim_label, keys))
        rec.update(_agg_stats(grp))
        records.append(rec)

    if not records:
        return pd.DataFrame(columns=dim_label + _PERF_COLS_ALL)

    out = pd.DataFrame(records, columns=dim_label + _PERF_COLS_ALL)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_symbol_performance(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Performance stats grouped by symbol.
    Returns DataFrame with columns: symbol, count, avg_return_4/12/24/48,
    winrate_4/12/24/48.
    """
    df = _candidates_only(enriched_df)
    df = _add_directional_returns(df)
    return _groupby_dimension(df, "symbol", "symbol")


def compute_timeframe_performance(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Performance stats grouped by timeframe.
    """
    df = _candidates_only(enriched_df)
    df = _add_directional_returns(df)
    return _groupby_dimension(df, "timeframe", "timeframe")


def compute_symbol_timeframe_performance(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Performance stats grouped by (symbol, timeframe).
    Returns columns: symbol, timeframe, count, avg_return_48, winrate_48.
    """
    df = _candidates_only(enriched_df)
    df = _add_directional_returns(df)
    full = _groupby_dimension(df, ["symbol", "timeframe"], ["symbol", "timeframe"])

    # Keep only the summary columns requested
    keep = ["symbol", "timeframe", "count", "avg_return_48", "winrate_48"]
    existing = [c for c in keep if c in full.columns]
    return full[existing]


def compute_signal_reason_performance(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Performance stats grouped by signal_reason (candidates only).
    Returns columns: signal_reason, count, avg_return_4/12/24/48, winrate_4/12/24/48.
    """
    df = _candidates_only(enriched_df)
    df = _add_directional_returns(df)
    return _groupby_dimension(df, "signal_reason", "signal_reason")


def build_best_dimensions(
    symbol_df: pd.DataFrame,
    timeframe_df: pd.DataFrame,
    symbol_tf_df: pd.DataFrame,
    signal_reason_df: pd.DataFrame,
    min_count: int = 100,
    top_n: int = 20,
) -> pd.DataFrame:
    """
    Combine all dimension DataFrames into one ranked table.
    Rank by winrate_48 DESC, then avg_return_48 DESC.
    Filter to min_count samples.
    Return top_n rows.

    Output columns: dimension_type, dimension_value, count,
                    avg_return_4/12/24/48, winrate_4/12/24/48
    """
    frames: list[pd.DataFrame] = []

    def _tag(df: pd.DataFrame, dtype: str, val_col: str) -> None:
        if df.empty:
            return
        tmp = df.copy()
        # Combine multi-column keys into a single string
        if val_col == "symbol_timeframe":
            tmp["dimension_value"] = tmp["symbol"] + "/" + tmp["timeframe"]
        else:
            tmp["dimension_value"] = tmp[val_col].astype(str)
        tmp["dimension_type"] = dtype

        # Ensure all PERF cols exist (symbol_tf_df only has subset)
        for h in HORIZONS:
            for prefix in ("avg_return_", "winrate_"):
                col = f"{prefix}{h}"
                if col not in tmp.columns:
                    tmp[col] = np.nan

        frames.append(tmp[_BEST_DIM_COLS])

    _tag(symbol_df,        "symbol",         "symbol")
    _tag(timeframe_df,     "timeframe",       "timeframe")
    _tag(symbol_tf_df,     "symbol_timeframe","symbol_timeframe")
    _tag(signal_reason_df, "signal_reason",   "signal_reason")

    if not frames:
        return pd.DataFrame(columns=_BEST_DIM_COLS)

    combined = pd.concat(frames, ignore_index=True)

    combined["_wr48"]  = pd.to_numeric(combined["winrate_48"],    errors="coerce")
    combined["_ar48"]  = pd.to_numeric(combined["avg_return_48"], errors="coerce")
    combined["_count"] = pd.to_numeric(combined["count"],         errors="coerce")

    result = (
        combined[combined["_count"] >= min_count]
        .dropna(subset=["_wr48"])
        .sort_values(["_wr48", "_ar48"], ascending=[False, False])
        .head(top_n)
        .drop(columns=["_wr48", "_ar48", "_count"])
        .reset_index(drop=True)
    )
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _candidates_only(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only BUY_CANDIDATE and SELL_CANDIDATE rows."""
    return df[df["candidate_signal"].isin({"BUY_CANDIDATE", "SELL_CANDIDATE"})].copy()
