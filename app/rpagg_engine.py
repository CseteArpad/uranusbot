"""
RPagg Engine — weighted aggregate of RP across timeframes + Bias signal.

RPagg = 0.10 * RP_1h + 0.20 * RP_4h + 0.30 * RP_12h + 0.40 * RP_1d

RPagg is only defined when ALL four timeframe RPs are available at a given timestamp.
If any TF is missing, rpagg = NaN and bias = NEUTRAL_BIAS.

Bias per row (within a timeframe series):
  RPagg[i] > RPagg[i-1]  -> UPWARD_BIAS
  RPagg[i] < RPagg[i-1]  -> DOWNWARD_BIAS
  otherwise               -> NEUTRAL_BIAS
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TF_WEIGHTS: dict[str, float] = {
    "1h":  0.10,
    "4h":  0.20,
    "12h": 0.30,
    "1d":  0.40,
}
ALL_TFS = list(TF_WEIGHTS.keys())


def compute_rpagg_for_symbol(
    tf_results: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """
    Input:
      tf_results — {timeframe: DataFrame} where each DF already has a 'rp' column
                   and a 'timestamp' column (timezone-aware or naive, but consistent).

    Output:
      Same dict, each DF enriched with 'rpagg' and 'bias' columns.
      Only timeframes present in tf_results are processed; missing TFs yield NaN rpagg.
    """
    # Build per-TF lookup tables (sorted by timestamp) for asof merge
    ref_tables: dict[str, pd.DataFrame] = {}
    for tf, df in tf_results.items():
        tbl = (
            df[["timestamp", "rp"]]
            .copy()
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        tbl["timestamp"] = _normalise_ts(tbl["timestamp"])
        tbl = tbl.rename(columns={"rp": f"rp_{tf}"})
        ref_tables[tf] = tbl

    enriched: dict[str, pd.DataFrame] = {}

    for tf, df in tf_results.items():
        base = df.copy().sort_values("timestamp").reset_index(drop=True)
        base["timestamp"] = _normalise_ts(base["timestamp"])

        # Asof-merge every TF's RP onto this TF's time axis
        for other_tf in ALL_TFS:
            col = f"rp_{other_tf}"
            if other_tf in ref_tables:
                merged = pd.merge_asof(
                    base[["timestamp"]],
                    ref_tables[other_tf],
                    on="timestamp",
                    direction="backward",
                )
                base[col] = merged[col].to_numpy()
            else:
                base[col] = np.nan

        # RPagg — only when all four TF RPs are present
        rp_cols = [f"rp_{t}" for t in ALL_TFS]
        all_present = base[rp_cols].notna().all(axis=1)

        rpagg = np.where(
            all_present,
            sum(base[f"rp_{t}"].to_numpy() * w for t, w in TF_WEIGHTS.items()),
            np.nan,
        )
        base["rpagg"] = np.round(rpagg, 6)

        # Bias — compare consecutive RPagg values within this TF series
        prev = np.roll(rpagg, 1)
        prev[0] = np.nan

        bias = np.full(len(base), "NEUTRAL_BIAS", dtype=object)
        valid = ~np.isnan(rpagg) & ~np.isnan(prev)
        bias[valid & (rpagg > prev)] = "UPWARD_BIAS"
        bias[valid & (rpagg < prev)] = "DOWNWARD_BIAS"
        bias[np.isnan(rpagg)] = "NEUTRAL_BIAS"
        base["bias"] = bias

        # Drop the helper rp_{tf} merge columns (keep only rp, rpagg, bias)
        base = base.drop(columns=rp_cols)

        enriched[tf] = base

    return enriched


def build_rpagg_summary(symbol: str, tf_results: dict[str, pd.DataFrame]) -> dict:
    """
    Aggregates RPagg stats across all TF result DataFrames for one symbol.
    Each DF must already have 'rpagg' and 'bias' columns.
    """
    all_rpagg = pd.concat(
        [df["rpagg"] for df in tf_results.values()], ignore_index=True
    ).dropna()

    all_bias = pd.concat(
        [df["bias"] for df in tf_results.values()], ignore_index=True
    )

    total = sum(len(df) for df in tf_results.values())

    return {
        "symbol": symbol,
        "total_count": total,
        "rpagg_min": round(float(all_rpagg.min()), 6) if not all_rpagg.empty else None,
        "rpagg_max": round(float(all_rpagg.max()), 6) if not all_rpagg.empty else None,
        "rpagg_avg": round(float(all_rpagg.mean()), 6) if not all_rpagg.empty else None,
        "upward_bias_count":   int((all_bias == "UPWARD_BIAS").sum()),
        "downward_bias_count": int((all_bias == "DOWNWARD_BIAS").sum()),
        "neutral_bias_count":  int((all_bias == "NEUTRAL_BIAS").sum()),
    }


def _normalise_ts(ts: pd.Series) -> pd.Series:
    """Convert to UTC-naive for consistent merge_asof comparison."""
    ts = pd.to_datetime(ts)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts
