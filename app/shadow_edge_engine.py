"""
Shadow Edge Engine — FÁZIS 6D.

Continuously monitors validated MODERATE edges by replaying all matching rows
from the enriched research DataFrame.

No live trading. No orders. No Binance / Freqtrade calls.
Pure offline signal tracking.

Trend rule (live_edge_tracking):
  Sort signals by timestamp, split into first 25% and last 25%.
  Compare winrate_48 of each quarter.
  delta = last_wr - first_wr
  IMPROVING  : delta >  +0.02
  DEGRADING  : delta <  -0.02
  STABLE     : |delta| <= 0.02   (or < 4 rows → forced STABLE)

Promotion rule:
  count >= 100  AND  winrate_48 >= 0.60
  AND  avg_return_48 > 0  AND  trend != DEGRADING
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

HORIZONS: tuple = (4, 12, 24, 48)

TREND_EPSILON    = 0.02   # delta threshold between STABLE and IMPROVING/DEGRADING
TREND_MIN_ROWS   = 4      # minimum rows to compute a trend (otherwise STABLE)

PROMOTE_MIN_COUNT   = 100
PROMOTE_MIN_WR48    = 0.60
PROMOTE_MIN_AVG_RET = 0.0


# ---------------------------------------------------------------------------
# Internal helpers  (condition parsing shared with edge_validation_engine)
# ---------------------------------------------------------------------------

def _parse_condition(condition: str) -> tuple[str, float]:
    m = re.match(r"rpagg([<>])(.+)", condition)
    if not m:
        raise ValueError(f"Cannot parse condition: {condition!r}")
    return m.group(1), float(m.group(2))


def _condition_mask(rpagg_f: np.ndarray, op: str, threshold: float) -> np.ndarray:
    return rpagg_f < threshold if op == "<" else rpagg_f > threshold


def _direction(op: str) -> str:
    return "buy" if op == "<" else "sell"


def _compute_returns(
    sig: np.ndarray, fut: np.ndarray, direction: str
) -> np.ndarray:
    valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)
    if direction == "buy":
        ret = (fut - sig) / sig
    else:
        ret = (sig - fut) / sig
    return np.where(valid, np.round(ret, 6), np.nan)


def _winrate(ret_arr: np.ndarray) -> float | None:
    vr = ret_arr[~np.isnan(ret_arr)]
    return round(float((vr > 0).mean()), 6) if len(vr) else None


def _avg_return(ret_arr: np.ndarray) -> float | None:
    vr = ret_arr[~np.isnan(ret_arr)]
    return round(float(vr.mean()), 6) if len(vr) else None


def _compute_trend(ret48_sorted: np.ndarray) -> str:
    """Compare first-25% vs last-25% winrate_48."""
    n = len(ret48_sorted)
    if n < TREND_MIN_ROWS:
        return "STABLE"

    q = max(1, n // 4)
    first_q = ret48_sorted[:q]
    last_q  = ret48_sorted[n - q:]

    first_wr = _winrate(first_q)
    last_wr  = _winrate(last_q)

    if first_wr is None or last_wr is None:
        return "STABLE"

    delta = last_wr - first_wr
    if delta > TREND_EPSILON:
        return "IMPROVING"
    if delta < -TREND_EPSILON:
        return "DEGRADING"
    return "STABLE"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_shadow_signals(
    validated_df: pd.DataFrame,
    combined_research: pd.DataFrame,
) -> pd.DataFrame:
    """
    For every row in combined_research that matches a MODERATE validated edge,
    generate a shadow signal with directional forward returns.

    Parameters
    ----------
    validated_df      : output of build_validated_edges(); must have EDGE_STABILITY.
    combined_research : enriched DataFrame from research_engine with columns:
                        symbol, timeframe, rpagg_f, bias, timestamp,
                        close_at_signal, close_future_4/12/24/48.

    Returns
    -------
    DataFrame with one row per shadow signal.
    """
    moderate = (
        validated_df[validated_df["EDGE_STABILITY"] == "MODERATE"].copy()
        if not validated_df.empty
        else pd.DataFrame()
    )
    if moderate.empty or combined_research.empty:
        return pd.DataFrame()

    rpagg_f  = combined_research["rpagg_f"].to_numpy(dtype=np.float64)
    bias_arr = combined_research["bias"].to_numpy()
    sym_arr  = combined_research["symbol"].to_numpy()
    tf_arr   = combined_research["timeframe"].to_numpy()

    frames: list[pd.DataFrame] = []

    for _, edge in moderate.iterrows():
        try:
            op, threshold = _parse_condition(edge["condition"])
        except ValueError:
            continue

        direction = _direction(op)

        mask = (
            (sym_arr == edge["symbol"])
            & (tf_arr == edge["timeframe"])
            & _condition_mask(rpagg_f, op, threshold)
            & (bias_arr == edge["bias"])
        )

        sub = combined_research[mask].copy()
        if sub.empty:
            continue

        sig = sub["close_at_signal"].to_numpy(dtype=np.float64)

        for h in HORIZONS:
            fut = sub[f"close_future_{h}"].to_numpy(dtype=np.float64)
            sub[f"return_{h}"]      = _compute_returns(sig, fut, direction)
            sub[f"future_close_{h}"] = fut

        sub["condition"]     = edge["condition"]
        sub["edge_grade"]    = edge.get("EDGE_GRADE", "")
        sub["edge_stability"]= edge["EDGE_STABILITY"]

        frames.append(sub)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)

    out_cols = [
        "timestamp", "symbol", "timeframe",
        "condition", "bias",
        "edge_grade", "edge_stability",
        "close_at_signal",
        "future_close_4",  "future_close_12",  "future_close_24",  "future_close_48",
        "return_4",        "return_12",         "return_24",         "return_48",
    ]
    existing = [c for c in out_cols if c in combined.columns]
    return combined[existing].reset_index(drop=True)


def compute_shadow_performance(shadow_df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-edge aggregate performance stats.

    Output columns:
      symbol, timeframe, condition, bias, count,
      avg_return_4/12/24/48, winrate_4/12/24/48
    """
    if shadow_df.empty:
        return pd.DataFrame()

    records: list[dict] = []
    group_cols = ["symbol", "timeframe", "condition", "bias"]

    for keys, grp in shadow_df.groupby(group_cols, sort=False):
        rec = dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,)))
        rec["count"] = len(grp)
        for h in HORIZONS:
            arr = grp[f"return_{h}"].to_numpy(dtype=np.float64)
            rec[f"avg_return_{h}"] = _avg_return(arr) or ""
            rec[f"winrate_{h}"]    = _winrate(arr)    or ""
        records.append(rec)

    perf_cols = (
        group_cols
        + ["count"]
        + [f"avg_return_{h}" for h in HORIZONS]
        + [f"winrate_{h}"    for h in HORIZONS]
    )
    return pd.DataFrame(records, columns=perf_cols)


def compute_live_edge_tracking(shadow_df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-edge live tracking: count, last_signal_timestamp, avg_return_48,
    winrate_48, trend (IMPROVING / STABLE / DEGRADING).

    Output columns:
      symbol, timeframe, condition, bias, count,
      last_signal_timestamp, avg_return_48, winrate_48, trend
    """
    if shadow_df.empty:
        return pd.DataFrame()

    records: list[dict] = []
    group_cols = ["symbol", "timeframe", "condition", "bias"]

    ts_col = "timestamp"

    for keys, grp in shadow_df.groupby(group_cols, sort=False):
        grp_sorted = grp.sort_values(ts_col)
        ret48 = grp_sorted["return_48"].to_numpy(dtype=np.float64)

        rec = dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,)))
        rec["count"]                 = len(grp_sorted)
        rec["last_signal_timestamp"] = str(grp_sorted[ts_col].iloc[-1])
        rec["avg_return_48"]         = _avg_return(ret48) or ""
        rec["winrate_48"]            = _winrate(ret48)    or ""
        rec["trend"]                 = _compute_trend(ret48)
        records.append(rec)

    out_cols = [
        "symbol", "timeframe", "condition", "bias",
        "count", "last_signal_timestamp",
        "avg_return_48", "winrate_48", "trend",
    ]
    return pd.DataFrame(records, columns=out_cols)


def build_promotion_candidates(tracking_df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter live_edge_tracking to edges meeting PROMOTE_TO_STRONG criteria:
      count >= 100
      winrate_48 >= 0.60
      avg_return_48 > 0
      trend != DEGRADING

    Output: same columns as tracking_df, sorted by winrate_48 DESC.
    """
    if tracking_df.empty:
        return pd.DataFrame()

    df = tracking_df.copy()
    df["_wr48"]  = pd.to_numeric(df["winrate_48"],    errors="coerce")
    df["_ar48"]  = pd.to_numeric(df["avg_return_48"], errors="coerce")
    df["_count"] = pd.to_numeric(df["count"],         errors="coerce")

    mask = (
        (df["_count"] >= PROMOTE_MIN_COUNT)
        & (df["_wr48"]  >= PROMOTE_MIN_WR48)
        & (df["_ar48"]  >  PROMOTE_MIN_AVG_RET)
        & (df["trend"]  != "DEGRADING")
    )

    result = (
        df[mask]
        .drop(columns=["_wr48", "_ar48", "_count"])
        .sort_values("winrate_48", ascending=False)
        .reset_index(drop=True)
    )
    return result
