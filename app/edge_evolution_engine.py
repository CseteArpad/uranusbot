"""
Edge Evolution Engine — FÁZIS 6D.1.

Analyses the temporal evolution of each shadow edge by splitting its signal
history into four chronological quarters and measuring winrate / return
progression.

Evolution status (evaluated in priority order):
  ACCELERATING : Q1 < Q2 < Q3 < Q4  (strictly monotone improvement)
  STABLE       : |Q4 - Q1| < 0.03   (small delta, regardless of direction)
  IMPROVING    : Q4 > Q1
  DEGRADING    : Q4 < Q1

PROMISING_EDGE : evolution_status == ACCELERATING  AND  q4_winrate >= 0.60

No live trading. No orders. No Binance / Freqtrade calls.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STABLE_EPSILON    = 0.03
PROMISING_MIN_WR4 = 0.60

_STATUS_ORDER = {"ACCELERATING": 0, "STABLE": 1, "IMPROVING": 2, "DEGRADING": 3}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _winrate(arr: np.ndarray) -> float | None:
    vr = arr[~np.isnan(arr)]
    return round(float((vr > 0).mean()), 6) if len(vr) else None


def _avg_return(arr: np.ndarray) -> float | None:
    vr = arr[~np.isnan(arr)]
    return round(float(vr.mean()), 6) if len(vr) else None


def _quarter_stats(
    ret48: np.ndarray,
) -> tuple[list[int], list[float | None], list[float | None]]:
    """
    Split ret48 into 4 chronological quarters.

    Returns (counts, winrates, avg_returns) — each a list of 4 values.
    Quarters with zero rows return (0, None, None).
    """
    n = len(ret48)
    q = max(1, n // 4)

    slices = [
        ret48[0    : q],
        ret48[q    : 2 * q],
        ret48[2*q  : 3 * q],
        ret48[3*q  :],
    ]

    counts   = [len(s)         for s in slices]
    winrates = [_winrate(s)    for s in slices]
    avgs     = [_avg_return(s) for s in slices]
    return counts, winrates, avgs


def _evolution_status(winrates: list[float | None]) -> str:
    w1, w2, w3, w4 = winrates

    # Need at least Q1 and Q4 to make any assessment
    if w1 is None or w4 is None:
        return "STABLE"

    # ACCELERATING: strictly increasing across all four quarters
    if (
        w2 is not None and w3 is not None
        and w1 < w2 < w3 < w4
    ):
        return "ACCELERATING"

    # STABLE: small absolute delta between Q1 and Q4
    if abs(w4 - w1) < STABLE_EPSILON:
        return "STABLE"

    if w4 > w1:
        return "IMPROVING"

    return "DEGRADING"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_edge_evolution(shadow_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute quartile progression stats for every edge in shadow_df.

    Parameters
    ----------
    shadow_df : output of shadow_edge_engine.compute_shadow_signals().
                Required columns: symbol, timeframe, condition, bias,
                                  timestamp, return_48.

    Returns
    -------
    DataFrame with columns:
      symbol, timeframe, condition, bias,
      q1_count/q2_count/q3_count/q4_count,
      q1_winrate/q2_winrate/q3_winrate/q4_winrate,
      q1_return/q2_return/q3_return/q4_return,
      evolution_status
    """
    if shadow_df.empty:
        return pd.DataFrame()

    group_cols = ["symbol", "timeframe", "condition", "bias"]
    ts_col     = "timestamp"

    records: list[dict] = []

    for keys, grp in shadow_df.groupby(group_cols, sort=False):
        grp_sorted = grp.sort_values(ts_col)
        ret48 = grp_sorted["return_48"].to_numpy(dtype=np.float64)

        counts, winrates, avgs = _quarter_stats(ret48)
        status = _evolution_status(winrates)

        rec = dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,)))
        for i, (cnt, wr, ar) in enumerate(zip(counts, winrates, avgs), start=1):
            rec[f"q{i}_count"]   = cnt
            rec[f"q{i}_winrate"] = wr if wr is not None else ""
            rec[f"q{i}_return"]  = ar if ar is not None else ""
        rec["evolution_status"] = status
        records.append(rec)

    if not records:
        return pd.DataFrame()

    out_cols = (
        group_cols
        + [f"q{i}_{m}" for i in range(1, 5) for m in ("count", "winrate", "return")]
        + ["evolution_status"]
    )
    return pd.DataFrame(records, columns=out_cols)


def build_evolution_ranking(evolution_df: pd.DataFrame) -> pd.DataFrame:
    """
    Sort edge_evolution by:
      1. evolution_status  (ACCELERATING > STABLE > IMPROVING > DEGRADING)
      2. q4_winrate  DESC
      3. q4_return   DESC
    """
    if evolution_df.empty:
        return pd.DataFrame()

    df = evolution_df.copy()
    df["_status_ord"] = df["evolution_status"].map(_STATUS_ORDER).fillna(99)
    df["_q4wr"]       = pd.to_numeric(df["q4_winrate"], errors="coerce").fillna(-1.0)
    df["_q4ret"]      = pd.to_numeric(df["q4_return"],  errors="coerce").fillna(-1.0)

    df = (
        df.sort_values(
            ["_status_ord", "_q4wr", "_q4ret"],
            ascending=[True, False, False],
        )
        .drop(columns=["_status_ord", "_q4wr", "_q4ret"])
        .reset_index(drop=True)
    )
    return df


def build_promising_edges(evolution_df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter to PROMISING_EDGE: ACCELERATING AND q4_winrate >= 0.60.
    """
    if evolution_df.empty:
        return pd.DataFrame()

    df = evolution_df.copy()
    df["_q4wr"] = pd.to_numeric(df["q4_winrate"], errors="coerce")

    result = (
        df[
            (df["evolution_status"] == "ACCELERATING")
            & (df["_q4wr"] >= PROMISING_MIN_WR4)
        ]
        .drop(columns=["_q4wr"])
        .reset_index(drop=True)
    )
    return result
