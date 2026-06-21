"""
Research Engine — FÁZIS 5.

Historikus kutatás: minden replay sor forward return-je RPagg küszöb +
bias kombinációk szerint.

No orders. No live trading. No Binance / Freqtrade calls.

Return direction:
  LOW  conditions (rpagg < threshold) : BUY  direction
       return_n = (close_future - close_signal) / close_signal
  HIGH conditions (rpagg > threshold) : SELL direction
       return_n = (close_signal - close_future) / close_signal

winner_n = 1 if return_n > 0 else 0
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HORIZONS: tuple = (4, 12, 24, 48)

LOW_THRESHOLDS:  list[float] = [-0.20, -0.10, 0.00, 0.10, 0.20]
HIGH_THRESHOLDS: list[float] = [0.80,  0.90,  1.00, 1.10, 1.20]

BIASES: list[str] = ["UPWARD_BIAS", "DOWNWARD_BIAS", "NEUTRAL_BIAS"]

SIGNAL_REASONS: list[str] = [
    "low_zone_upward_bias",
    "below_range_recovery",
    "high_zone_downward_bias",
    "above_range_reversal",
]

_REASON_DIRECTION: dict[str, str] = {
    "low_zone_upward_bias":     "buy",
    "below_range_recovery":     "buy",
    "high_zone_downward_bias":  "sell",
    "above_range_reversal":     "sell",
}


def _normalise_ts(ts: pd.Series) -> pd.Series:
    ts = pd.to_datetime(ts)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


# ---------------------------------------------------------------------------
# Step 1 — enrich all rows with forward close prices
# ---------------------------------------------------------------------------

def enrich_with_forward_closes(
    rows: list,
    df_candles: pd.DataFrame,
    symbol: str,
    timeframe: str,
    horizons: tuple = HORIZONS,
) -> pd.DataFrame:
    """
    Vectorised: add close_at_signal and close_future_N to every row.

    Returns DataFrame with columns:
      timestamp, symbol, timeframe, rpagg_f, bias, signal_reason,
      candidate_signal, close_at_signal, close_future_4/12/24/48
    Empty DataFrame when rows is empty.
    """
    if not rows:
        return pd.DataFrame()

    # ------------------------------------------------------------------ #
    # Build candle close lookup
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
    # Build row DataFrame
    # ------------------------------------------------------------------ #
    df = pd.DataFrame([{
        "timestamp":        r["timestamp"],
        "symbol":           symbol,
        "timeframe":        timeframe,
        "rpagg":            r.get("rpagg", ""),
        "bias":             r.get("bias", "NEUTRAL_BIAS"),
        "signal_reason":    r.get("signal_reason", "no_signal"),
        "candidate_signal": r.get("candidate_signal", "NEUTRAL"),
    } for r in rows])

    df["_ts"]     = _normalise_ts(pd.to_datetime(df["timestamp"]))
    # rpagg stored as float or "" — convert to numeric NaN
    df["rpagg_f"] = pd.to_numeric(df["rpagg"], errors="coerce")

    # Row timestamp → candle index i → signal close at i+1
    raw_idx    = np.array([ts_map.get(pd.Timestamp(ts), -2) for ts in df["_ts"]])
    signal_idx = raw_idx + 1
    valid_sig  = (signal_idx >= 0) & (signal_idx < n_candles)

    close_sig = np.where(
        valid_sig,
        closes[np.clip(signal_idx, 0, n_candles - 1)],
        np.nan,
    )
    df["close_at_signal"] = close_sig

    for h in horizons:
        fut_idx   = signal_idx + h
        fut_valid = valid_sig & (fut_idx < n_candles)
        df[f"close_future_{h}"] = np.where(
            fut_valid,
            closes[np.clip(fut_idx, 0, n_candles - 1)],
            np.nan,
        )

    keep = [
        "timestamp", "symbol", "timeframe",
        "rpagg_f", "bias", "signal_reason", "candidate_signal",
        "close_at_signal",
    ] + [f"close_future_{h}" for h in horizons]

    return df[keep].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 2 — per-subset return stats (internal helper)
# ---------------------------------------------------------------------------

def _stats(subset: pd.DataFrame, direction: str) -> dict | None:
    n = len(subset)
    if n == 0:
        return None

    rec: dict = {"count": n}
    sig = subset["close_at_signal"].to_numpy(dtype=np.float64)

    for h in HORIZONS:
        fut   = subset[f"close_future_{h}"].to_numpy(dtype=np.float64)
        valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)

        if direction == "buy":
            ret = np.where(valid, (fut - sig) / sig, np.nan)
        else:
            ret = np.where(valid, (sig - fut) / sig, np.nan)

        vr = ret[~np.isnan(ret)]
        rec[f"avg_return_{h}"] = round(float(vr.mean()), 6) if len(vr) else ""
        rec[f"winrate_{h}"]    = round(float((vr > 0).mean()), 6) if len(vr) else ""

    return rec


# ---------------------------------------------------------------------------
# Step 3 — research summary: threshold × bias grid
# ---------------------------------------------------------------------------

def compute_research_stats(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    For every (rpagg threshold, bias) combination compute return stats.

    Low  thresholds: rpagg_f < threshold  → BUY  direction
    High thresholds: rpagg_f > threshold  → SELL direction

    Returns DataFrame with columns:
      condition, count, avg_return_4/12/24/48, winrate_4/12/24/48
    """
    if enriched_df.empty:
        return pd.DataFrame()

    records: list[dict] = []

    rpagg_f = enriched_df["rpagg_f"].to_numpy(dtype=np.float64)
    bias_arr = enriched_df["bias"].to_numpy()

    for thresh in LOW_THRESHOLDS:
        thresh_mask = rpagg_f < thresh
        for bias in BIASES:
            mask = thresh_mask & (bias_arr == bias)
            rec  = _stats(enriched_df[mask], direction="buy")
            if rec:
                rec["condition"] = f"rpagg<{thresh:.2f}|{bias}"
                records.append(rec)

    for thresh in HIGH_THRESHOLDS:
        thresh_mask = rpagg_f > thresh
        for bias in BIASES:
            mask = thresh_mask & (bias_arr == bias)
            rec  = _stats(enriched_df[mask], direction="sell")
            if rec:
                rec["condition"] = f"rpagg>{thresh:.2f}|{bias}"
                records.append(rec)

    cols = [
        "condition", "count",
        "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
        "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
    ]
    return pd.DataFrame(records, columns=cols)


# ---------------------------------------------------------------------------
# Step 4 — signal_reason summary
# ---------------------------------------------------------------------------

def compute_signal_reason_stats(enriched_df: pd.DataFrame) -> pd.DataFrame:
    """
    Stats for the four named signal reasons (count, avg_return_48, winrate_48).
    """
    records: list[dict] = []

    for reason in SIGNAL_REASONS:
        direction = _REASON_DIRECTION.get(reason, "buy")
        subset = enriched_df[enriched_df["signal_reason"] == reason]
        n = len(subset)

        if n == 0:
            records.append({
                "signal_reason": reason,
                "count": 0,
                "avg_return_48": "",
                "winrate_48": "",
            })
            continue

        sig = subset["close_at_signal"].to_numpy(dtype=np.float64)
        fut = subset["close_future_48"].to_numpy(dtype=np.float64)
        valid = ~np.isnan(sig) & ~np.isnan(fut) & (sig > 0)

        if direction == "buy":
            ret = np.where(valid, (fut - sig) / sig, np.nan)
        else:
            ret = np.where(valid, (sig - fut) / sig, np.nan)

        vr = ret[~np.isnan(ret)]
        records.append({
            "signal_reason": reason,
            "count":         n,
            "avg_return_48": round(float(vr.mean()), 6)       if len(vr) else "",
            "winrate_48":    round(float((vr > 0).mean()), 6) if len(vr) else "",
        })

    return pd.DataFrame(records, columns=["signal_reason", "count", "avg_return_48", "winrate_48"])


# ---------------------------------------------------------------------------
# Step 5 — TOP10 conditions
# ---------------------------------------------------------------------------

def build_top10(research_df: pd.DataFrame, min_count: int = 100) -> pd.DataFrame:
    """
    Return the 10 conditions with highest winrate_48, filtered by min_count.
    """
    if research_df.empty:
        return pd.DataFrame()

    df = research_df.copy()
    df["_wr48"] = pd.to_numeric(df["winrate_48"], errors="coerce")
    df = df[df["count"] >= min_count].dropna(subset=["_wr48"])
    df = df.sort_values("_wr48", ascending=False).head(10)
    return df.drop(columns=["_wr48"]).reset_index(drop=True)
