"""
Edge Finder Engine — FÁZIS 6B.

Finds the best (symbol × timeframe × RPagg zone × bias) combinations.

Return direction:
  LOW  zones (rpagg < threshold) → BUY  direction
  HIGH zones (rpagg > threshold) → SELL direction

EDGE_GRADE:
  A : winrate_48 >= 0.60
  B : 0.55 <= winrate_48 < 0.60
  C : 0.50 <= winrate_48 < 0.55
  Rows with winrate_48 < 0.50 excluded from top_edges.

No orders. No live trading. No Binance / Freqtrade calls.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOW_THRESHOLDS:  list[float] = [-0.20, -0.10, 0.00, 0.10, 0.20]
HIGH_THRESHOLDS: list[float] = [0.80,  0.90,  1.00, 1.10, 1.20]
BIASES:          list[str]   = ["UPWARD_BIAS", "DOWNWARD_BIAS", "NEUTRAL_BIAS"]
SYMBOLS:         list[str]   = ["XRPUSDC", "BTCUSDC", "ETHUSDC", "SOLUSDC"]
TIMEFRAMES:      list[str]   = ["1h", "4h", "12h", "1d"]

MIN_COUNT: int   = 30
TOP_N:     int   = 50

GRADE_A_THRESHOLD = 0.60
GRADE_B_THRESHOLD = 0.55
GRADE_C_THRESHOLD = 0.50


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _edge_grade(winrate: float) -> str:
    if winrate >= GRADE_A_THRESHOLD:
        return "A"
    if winrate >= GRADE_B_THRESHOLD:
        return "B"
    return "C"


def _compute_stats_48(
    df: pd.DataFrame,
    direction: str,
) -> dict | None:
    """
    Compute count, avg_return_48, winrate_48 for a subset.
    Returns None if fewer than MIN_COUNT rows or no valid future closes.
    """
    n = len(df)
    if n < MIN_COUNT:
        return None

    sig = df["close_at_signal"].to_numpy(dtype=np.float64)
    fut = df["close_future_48"].to_numpy(dtype=np.float64)
    valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)

    if direction == "buy":
        ret = np.where(valid, (fut - sig) / sig, np.nan)
    else:
        ret = np.where(valid, (sig - fut) / sig, np.nan)

    vr = ret[~np.isnan(ret)]
    if len(vr) == 0:
        return None

    return {
        "count":          n,
        "avg_return_48":  round(float(vr.mean()), 6),
        "winrate_48":     round(float((vr > 0).mean()), 6),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_edge_grid(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute return stats for every (symbol × timeframe × rpagg_zone × bias).

    Input : combined_research DataFrame from research_engine.enrich_with_forward_closes()
            Must have columns: symbol, timeframe, rpagg_f, bias,
                               close_at_signal, close_future_48.

    Output: DataFrame with columns:
              symbol, timeframe, condition, bias,
              count, avg_return_48, winrate_48
            Rows with count < MIN_COUNT are excluded.
    """
    if enriched_df.empty:
        return pd.DataFrame()

    rpagg_f  = enriched_df["rpagg_f"].to_numpy(dtype=np.float64)
    bias_arr = enriched_df["bias"].to_numpy()
    sym_arr  = enriched_df["symbol"].to_numpy()
    tf_arr   = enriched_df["timeframe"].to_numpy()

    records: list[dict] = []

    for symbol in SYMBOLS:
        sym_mask = sym_arr == symbol
        if not sym_mask.any():
            continue

        for timeframe in TIMEFRAMES:
            tf_mask = sym_mask & (tf_arr == timeframe)
            if not tf_mask.any():
                continue

            for thresh in LOW_THRESHOLDS:
                zone_mask = tf_mask & (rpagg_f < thresh)
                if not zone_mask.any():
                    continue
                condition = f"rpagg<{thresh:.2f}"

                for bias in BIASES:
                    full_mask = zone_mask & (bias_arr == bias)
                    subset = enriched_df[full_mask]
                    stats = _compute_stats_48(subset, direction="buy")
                    if stats:
                        records.append({
                            "symbol":    symbol,
                            "timeframe": timeframe,
                            "condition": condition,
                            "bias":      bias,
                            **stats,
                        })

            for thresh in HIGH_THRESHOLDS:
                zone_mask = tf_mask & (rpagg_f > thresh)
                if not zone_mask.any():
                    continue
                condition = f"rpagg>{thresh:.2f}"

                for bias in BIASES:
                    full_mask = zone_mask & (bias_arr == bias)
                    subset = enriched_df[full_mask]
                    stats = _compute_stats_48(subset, direction="sell")
                    if stats:
                        records.append({
                            "symbol":    symbol,
                            "timeframe": timeframe,
                            "condition": condition,
                            "bias":      bias,
                            **stats,
                        })

    if not records:
        return pd.DataFrame()

    cols = [
        "symbol", "timeframe", "condition", "bias",
        "count", "avg_return_48", "winrate_48",
    ]
    return pd.DataFrame(records, columns=cols)


def build_top_edges(edge_df: pd.DataFrame, top_n: int = TOP_N) -> pd.DataFrame:
    """
    From the full edge grid, return the TOP N edges:
      - winrate_48 >= GRADE_C_THRESHOLD (0.50)
      - count >= MIN_COUNT  (already enforced by compute_edge_grid)
      - sorted by winrate_48 DESC, avg_return_48 DESC
      - EDGE_GRADE column added (A / B / C)

    Output columns:
      symbol, timeframe, condition, bias,
      count, avg_return_48, winrate_48, EDGE_GRADE
    """
    if edge_df.empty:
        return pd.DataFrame()

    df = edge_df[edge_df["winrate_48"] >= GRADE_C_THRESHOLD].copy()
    if df.empty:
        return df

    df["EDGE_GRADE"] = df["winrate_48"].apply(_edge_grade)
    df = (
        df.sort_values(
            ["winrate_48", "avg_return_48"], ascending=[False, False]
        )
        .head(top_n)
        .reset_index(drop=True)
    )

    out_cols = [
        "symbol", "timeframe", "condition", "bias",
        "count", "avg_return_48", "winrate_48", "EDGE_GRADE",
    ]
    return df[out_cols]
