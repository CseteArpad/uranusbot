"""
Edge Validation Engine — FÁZIS 6C.

Stability check for A- and B-grade edges across time windows:
  full dataset / last 90d / last 60d / last 30d.

EDGE_STABILITY:
  STRONG   : full winrate_48 >= 0.60, count >= 50,
             all available 90/60/30d windows also >= 0.55
  MODERATE : full winrate_48 >= 0.55, count >= 30
  WEAK     : everything else

No orders. No live trading. No Binance / Freqtrade calls.
"""
from __future__ import annotations

import re
from datetime import timedelta

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOWS_DAYS: list[int] = [90, 60, 30]

# Minimum sample counts for annotation
SAMPLE_LEVELS: list[int] = [100, 75, 50, 30]

STRONG_FULL_MIN_WR    = 0.60
STRONG_FULL_MIN_COUNT = 50
STRONG_WINDOW_MIN_WR  = 0.55
STRONG_WINDOW_MIN_N   = 10   # window must have this many rows to be evaluated

MODERATE_MIN_WR    = 0.55
MODERATE_MIN_COUNT = 30


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_ts(ts: pd.Series) -> pd.Series:
    ts = pd.to_datetime(ts)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


def _parse_condition(condition: str) -> tuple[str, float]:
    """
    Parse 'rpagg<-0.20' → ('<', -0.20)
    Parse 'rpagg>0.80'  → ('>', 0.80)
    """
    m = re.match(r"rpagg([<>])(.+)", condition)
    if not m:
        raise ValueError(f"Cannot parse condition: {condition!r}")
    return m.group(1), float(m.group(2))


def _condition_mask(rpagg_f: np.ndarray, op: str, threshold: float) -> np.ndarray:
    return rpagg_f < threshold if op == "<" else rpagg_f > threshold


def _direction(op: str) -> str:
    return "buy" if op == "<" else "sell"


def _compute_stats(
    df: pd.DataFrame, direction: str
) -> tuple[int, float | None, float | None]:
    """
    Returns (count, avg_return_48, winrate_48).
    avg/winrate are None when no valid rows exist.
    """
    n = len(df)
    if n == 0:
        return 0, None, None

    sig = df["close_at_signal"].to_numpy(dtype=np.float64)
    fut = df["close_future_48"].to_numpy(dtype=np.float64)
    valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)

    if direction == "buy":
        ret = np.where(valid, (fut - sig) / sig, np.nan)
    else:
        ret = np.where(valid, (sig - fut) / sig, np.nan)

    vr = ret[~np.isnan(ret)]
    if len(vr) == 0:
        return n, None, None

    return (
        n,
        round(float(vr.mean()), 6),
        round(float((vr > 0).mean()), 6),
    )


def _sample_level(count: int) -> str:
    for lvl in SAMPLE_LEVELS:
        if count >= lvl:
            return f">={lvl}"
    return "<30"


def _window_ok(wr: float | None, count: int) -> bool:
    """
    True if the window either has insufficient data to judge OR passes threshold.
    Prevents penalising edges for sparse recent windows.
    """
    if count < STRONG_WINDOW_MIN_N:
        return True
    return wr is not None and wr >= STRONG_WINDOW_MIN_WR


def _edge_stability(
    full_count: int,
    full_wr: float | None,
    window_stats: list[tuple[int, float | None]],
) -> str:
    """
    Determine EDGE_STABILITY label.

    window_stats: [(count_90d, wr_90d), (count_60d, wr_60d), (count_30d, wr_30d)]
    """
    if full_wr is None:
        return "WEAK"

    # STRONG: full winrate >= 0.60, count >= 50, all evaluable windows >= 0.55
    if (
        full_wr >= STRONG_FULL_MIN_WR
        and full_count >= STRONG_FULL_MIN_COUNT
        and all(_window_ok(wr, cnt) for cnt, wr in window_stats)
    ):
        return "STRONG"

    # MODERATE: full winrate >= 0.55, count >= 30
    if full_wr >= MODERATE_MIN_WR and full_count >= MODERATE_MIN_COUNT:
        return "MODERATE"

    return "WEAK"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_edges(
    ab_edges: pd.DataFrame,
    combined_research: pd.DataFrame,
) -> pd.DataFrame:
    """
    For every A/B edge in ab_edges, compute stability stats.

    Parameters
    ----------
    ab_edges          : DataFrame from build_top_edges() filtered to A and B grades.
                        Required columns: symbol, timeframe, condition, bias, EDGE_GRADE.
    combined_research : enriched DataFrame from research_engine.enrich_with_forward_closes().
                        Required columns: symbol, timeframe, rpagg_f, bias,
                                          close_at_signal, close_future_48, timestamp.

    Returns
    -------
    DataFrame with columns:
      symbol, timeframe, condition, bias,
      EDGE_GRADE, sample_level,
      full_count, full_avg_return_48, full_winrate_48,
      count_90d, winrate_90d,
      count_60d, winrate_60d,
      count_30d, winrate_30d,
      EDGE_STABILITY
    """
    if ab_edges.empty or combined_research.empty:
        return pd.DataFrame()

    # Normalise timestamps in research data
    res = combined_research.copy()
    res["_ts"] = _normalise_ts(pd.to_datetime(res["timestamp"]))
    ref_date   = res["_ts"].max()          # most recent candle in dataset

    cutoffs = {d: ref_date - timedelta(days=d) for d in WINDOWS_DAYS}

    rpagg_f_arr = res["rpagg_f"].to_numpy(dtype=np.float64)
    bias_arr    = res["bias"].to_numpy()
    sym_arr     = res["symbol"].to_numpy()
    tf_arr      = res["timeframe"].to_numpy()
    ts_arr      = res["_ts"].to_numpy()    # numpy datetime64

    records: list[dict] = []

    for _, edge in ab_edges.iterrows():
        symbol    = edge["symbol"]
        timeframe = edge["timeframe"]
        condition = edge["condition"]
        bias      = edge["bias"]
        grade     = edge.get("EDGE_GRADE", "")

        try:
            op, threshold = _parse_condition(condition)
        except ValueError:
            continue

        direction = _direction(op)

        # Base mask: symbol + timeframe + condition + bias
        base_mask = (
            (sym_arr == symbol)
            & (tf_arr == timeframe)
            & _condition_mask(rpagg_f_arr, op, threshold)
            & (bias_arr == bias)
        )

        full_sub = res[base_mask]
        full_count, full_avg, full_wr = _compute_stats(full_sub, direction)

        window_rows: list[tuple[int, float | None]] = []
        row: dict = {
            "symbol":             symbol,
            "timeframe":          timeframe,
            "condition":          condition,
            "bias":               bias,
            "EDGE_GRADE":         grade,
            "sample_level":       _sample_level(full_count),
            "full_count":         full_count,
            "full_avg_return_48": full_avg if full_avg is not None else "",
            "full_winrate_48":    full_wr  if full_wr  is not None else "",
        }

        for days in WINDOWS_DAYS:
            cutoff   = cutoffs[days]
            w_mask   = base_mask & (ts_arr >= np.datetime64(cutoff))
            w_sub    = res[w_mask]
            w_cnt, _, w_wr = _compute_stats(w_sub, direction)
            window_rows.append((w_cnt, w_wr))
            row[f"count_{days}d"]   = w_cnt
            row[f"winrate_{days}d"] = w_wr if w_wr is not None else ""

        row["EDGE_STABILITY"] = _edge_stability(full_count, full_wr, window_rows)
        records.append(row)

    if not records:
        return pd.DataFrame()

    col_order = [
        "symbol", "timeframe", "condition", "bias",
        "EDGE_GRADE", "sample_level",
        "full_count", "full_avg_return_48", "full_winrate_48",
        "count_90d", "winrate_90d",
        "count_60d", "winrate_60d",
        "count_30d", "winrate_30d",
        "EDGE_STABILITY",
    ]
    return pd.DataFrame(records, columns=col_order)


def build_validated_edges(validation_df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter to STRONG and MODERATE edges, sort:
      1. EDGE_STABILITY (STRONG first)
      2. winrate_30d DESC
      3. full_winrate_48 DESC
    """
    if validation_df.empty:
        return pd.DataFrame()

    df = validation_df[
        validation_df["EDGE_STABILITY"].isin({"STRONG", "MODERATE"})
    ].copy()

    if df.empty:
        return df

    # Convert winrate columns to numeric for sorting (may contain "" strings)
    df["_wr30"]  = pd.to_numeric(df["winrate_30d"],     errors="coerce").fillna(-1.0)
    df["_wr48"]  = pd.to_numeric(df["full_winrate_48"], errors="coerce").fillna(-1.0)
    df["_stab"]  = df["EDGE_STABILITY"].map({"STRONG": 0, "MODERATE": 1})

    df = (
        df.sort_values(["_stab", "_wr30", "_wr48"], ascending=[True, False, False])
        .drop(columns=["_stab", "_wr30", "_wr48"])
        .reset_index(drop=True)
    )
    return df
