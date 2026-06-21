import json
import csv
import math
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.pivot_engine import detect_pivot_flags
from app.atr_engine import calculate_atr_series
from app.state_manager import load_state, save_state
from app.zone_engine import classify_zone, classify_candidate
from app.candidate_audit_engine import audit_candidates, build_performance_summary

BASE = Path(".")
DATA_DIR = BASE / "data" / "candles"
REPORT_DIR = BASE / "reports"

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

TF_WEIGHTS = {"1h": 0.10, "4h": 0.20, "12h": 0.30, "1d": 0.40}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    return json.loads(Path("config.json").read_text(encoding="utf-8"))


def load_candles(symbol, timeframe):
    path = DATA_DIR / f"{symbol}_{timeframe}.csv"
    if not path.exists():
        print(f"HIÁNYZIK: {path}")
        return None

    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: hiányzó oszlopok: {missing}")

    df["timestamp"] = pd.to_datetime(df["timestamp"])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=REQUIRED_COLUMNS)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def run_replay(df, symbol, timeframe, cfg):
    left = int(cfg["pivot_left"])
    right = int(cfg["pivot_right"])
    atr_period = int(cfg["atr_period"])
    multiplier = float(cfg["atr_multiplier"])

    pivot_low, pivot_high = detect_pivot_flags(df, left=left, right=right)
    atr_series = calculate_atr_series(df, period=atr_period)

    # Pre-extract numpy arrays — eliminates per-row df.iloc overhead
    low_arr       = df["low"].to_numpy(dtype=float)
    high_arr      = df["high"].to_numpy(dtype=float)
    close_arr     = df["close"].to_numpy(dtype=float)
    timestamp_arr = df["timestamp"].to_numpy()
    atr_arr       = atr_series.to_numpy(dtype=float)

    rows = []
    hit_count = 0
    miss_count = 0
    overshoots = []

    last_low_price = None
    last_high_price = None

    min_i = max(left + right + 1, atr_period + 2)

    for i in range(min_i, len(df) - 1):
        confirm_idx = i - right

        if confirm_idx >= 0:
            if pivot_low[confirm_idx]:
                last_low_price = low_arr[confirm_idx]
            if pivot_high[confirm_idx]:
                last_high_price = high_arr[confirm_idx]

        atr = atr_arr[i]
        if np.isnan(atr) or last_low_price is None or last_high_price is None:
            continue

        range_low = last_low_price - multiplier * atr
        range_high = last_high_price + multiplier * atr

        if range_high <= range_low:
            continue

        next_close = close_arr[i + 1]
        hit = range_low <= next_close <= range_high

        if hit:
            overshoot = 0.0
            hit_count += 1
        else:
            miss_count += 1
            if next_close > range_high:
                overshoot = next_close - range_high
            else:
                overshoot = range_low - next_close

        overshoots.append(overshoot)

        # RP = Range Position of next_close within the range
        rp = round((next_close - range_low) / (range_high - range_low), 6)

        rows.append({
            "timestamp": str(timestamp_arr[i]),
            "symbol": symbol,
            "timeframe": timeframe,
            "range_low": range_low,
            "range_high": range_high,
            "next_close": next_close,
            "hit": int(hit),
            "overshoot": overshoot,
            "rp": rp,
        })

    total = hit_count + miss_count
    hit_rate = hit_count / total if total else 0.0
    outside_rate = 1.0 - hit_rate if total else 0.0
    avg_overshoot = sum(overshoots) / len(overshoots) if overshoots else 0.0
    max_overshoot = max(overshoots) if overshoots else 0.0

    summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "hit_count": hit_count,
        "miss_count": miss_count,
        "total_count": total,
        "hit_rate": round(hit_rate, 6),
        "outside_rate": round(outside_rate, 6),
        "average_overshoot": round(avg_overshoot, 10),
        "max_overshoot": round(max_overshoot, 10),
        "pass_fail": "PASS" if hit_rate > 0.70 else "FAIL"
    }

    return rows, summary


def _normalise_ts(series):
    """UTC-naive timestamp for merge_asof."""
    ts = pd.to_datetime(series)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


def _nan_to_empty(v):
    """Convert NaN/inf to empty string for CSV output."""
    if v is None:
        return ""
    try:
        if math.isnan(v) or math.isinf(v):
            return ""
    except TypeError:
        pass
    return v


def compute_rpagg_for_symbol(symbol_tf_rows):
    """
    Input:  {tf: [row_dict, ...]}  — each row already has 'rp' and 'timestamp'
    Output: {tf: [row_dict, ...]}  — each row enriched with 'rpagg' and 'bias'

    Uses backward asof-merge so each row gets the most recent RP from every TF
    visible at that timestamp. RPagg is NaN unless all four TF RPs are present.
    """
    # Build per-TF lookup tables for merge_asof
    ref_tables = {}
    for tf, rows in symbol_tf_rows.items():
        if not rows:
            continue
        tbl = pd.DataFrame({"timestamp": [r["timestamp"] for r in rows],
                             f"rp_{tf}":  [r["rp"]        for r in rows]})
        tbl["timestamp"] = _normalise_ts(tbl["timestamp"])
        tbl = tbl.sort_values("timestamp").reset_index(drop=True)
        ref_tables[tf] = tbl

    enriched = {}

    for tf, rows in symbol_tf_rows.items():
        if not rows:
            enriched[tf] = rows
            continue

        base = pd.DataFrame(rows)
        base["timestamp"] = _normalise_ts(base["timestamp"])
        base = base.sort_values("timestamp").reset_index(drop=True)

        # Asof-merge all four TF RPs onto this TF's time axis
        for other_tf in TF_WEIGHTS:
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
        rp_cols = [f"rp_{t}" for t in TF_WEIGHTS]
        all_present = base[rp_cols].notna().all(axis=1).to_numpy()

        rpagg_arr = np.where(
            all_present,
            sum(base[f"rp_{t}"].to_numpy() * w for t, w in TF_WEIGHTS.items()),
            np.nan,
        )
        rpagg_rounded = np.where(~np.isnan(rpagg_arr),
                                 np.round(rpagg_arr, 6), np.nan)

        # Bias — consecutive RPagg comparison within this TF series
        prev = np.empty_like(rpagg_arr)
        prev[0] = np.nan
        prev[1:] = rpagg_arr[:-1]

        bias = np.full(len(base), "NEUTRAL_BIAS", dtype=object)
        valid = ~np.isnan(rpagg_arr) & ~np.isnan(prev)
        bias[valid & (rpagg_arr > prev)] = "UPWARD_BIAS"
        bias[valid & (rpagg_arr < prev)] = "DOWNWARD_BIAS"

        base = base.drop(columns=rp_cols)
        base["rpagg"] = [_nan_to_empty(v) for v in rpagg_rounded.tolist()]
        base["bias"] = bias.tolist()

        # FAZIS 3 — zone + candidate audit labels (no orders, no live trading)
        zones = [classify_zone(v) for v in base["rpagg"]]
        candidates = [classify_candidate(z, b) for z, b in zip(zones, base["bias"])]
        base["zone"] = zones
        base["candidate_signal"] = [c[0] for c in candidates]
        base["signal_reason"]    = [c[1] for c in candidates]

        enriched[tf] = base.to_dict("records")

    return enriched


def build_rpagg_summary(symbol, enriched_tf_rows):
    """Per-symbol RPagg aggregate statistics."""
    all_rpagg = []
    all_bias = []

    for rows in enriched_tf_rows.values():
        for r in rows:
            v = r.get("rpagg", "")
            if v != "" and v is not None:
                try:
                    all_rpagg.append(float(v))
                except (TypeError, ValueError):
                    pass
            all_bias.append(r.get("bias", "NEUTRAL_BIAS"))

    total = sum(len(rows) for rows in enriched_tf_rows.values())

    return {
        "symbol": symbol,
        "total_count": total,
        "rpagg_min":  round(min(all_rpagg), 6)                         if all_rpagg else "",
        "rpagg_max":  round(max(all_rpagg), 6)                         if all_rpagg else "",
        "rpagg_avg":  round(sum(all_rpagg) / len(all_rpagg), 6)        if all_rpagg else "",
        "upward_bias_count":   all_bias.count("UPWARD_BIAS"),
        "downward_bias_count": all_bias.count("DOWNWARD_BIAS"),
        "neutral_bias_count":  all_bias.count("NEUTRAL_BIAS"),
    }


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    summaries = []
    rpagg_summaries = []

    # symbol_tf_rows[symbol][tf] = [row_dict, ...]
    # symbol_tf_dfs[symbol][tf]  = original candle DataFrame (for FÁZIS 4 audit)
    symbol_tf_rows = {sym: {} for sym in cfg["symbols"]}
    symbol_tf_dfs  = {sym: {} for sym in cfg["symbols"]}

    for symbol in cfg["symbols"]:
        for tf in cfg["timeframes"]:
            df = load_candles(symbol, tf)

            if df is None or len(df) < 40:
                summaries.append({
                    "symbol": symbol,
                    "timeframe": tf,
                    "hit_count": 0,
                    "miss_count": 0,
                    "total_count": 0,
                    "hit_rate": 0.0,
                    "outside_rate": 0.0,
                    "average_overshoot": 0.0,
                    "max_overshoot": 0.0,
                    "pass_fail": "NO_DATA"
                })
                continue

            rows, summary = run_replay(df, symbol, tf, cfg)
            symbol_tf_rows[symbol][tf] = rows
            symbol_tf_dfs[symbol][tf]  = df
            summaries.append(summary)

    # RPagg + Bias + FÁZIS 4 candidate audit — per symbol, cross-TF merge
    all_rows = []
    all_audit_dfs = []
    for symbol in cfg["symbols"]:
        tf_rows = symbol_tf_rows[symbol]
        if not tf_rows:
            continue
        enriched = compute_rpagg_for_symbol(tf_rows)
        for tf, tf_rows_list in enriched.items():
            all_rows.extend(tf_rows_list)
            df_c = symbol_tf_dfs.get(symbol, {}).get(tf)
            if df_c is not None:
                adf = audit_candidates(tf_rows_list, df_c, symbol, tf)
                if not adf.empty:
                    all_audit_dfs.append(adf)
        rpagg_summaries.append(build_rpagg_summary(symbol, enriched))

    # --- Write replay_results.csv ---
    replay_path = REPORT_DIR / "replay_results.csv"
    replay_fields = [
        "timestamp", "symbol", "timeframe",
        "range_low", "range_high", "next_close",
        "hit", "overshoot", "rp", "rpagg", "bias",
        "zone", "candidate_signal", "signal_reason"
    ]
    with replay_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=replay_fields,
                                delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    # --- Write summary.csv (unchanged structure) ---
    summary_path = REPORT_DIR / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summaries[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(summaries)

    # --- Write audit_report.json ---
    audit_path = REPORT_DIR / "audit_report.json"
    audit = {
        "project": "Uranus Range Lab V1",
        "phase": "FAZIS-3",
        "generated_at": utc_now(),
        "summaries": summaries
    }
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- Write rpagg_summary.csv ---
    if rpagg_summaries:
        rpagg_path = REPORT_DIR / "rpagg_summary.csv"
        rpagg_fields = [
            "symbol", "total_count",
            "rpagg_min", "rpagg_max", "rpagg_avg",
            "upward_bias_count", "downward_bias_count", "neutral_bias_count"
        ]
        with rpagg_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rpagg_fields, delimiter=";")
            writer.writeheader()
            writer.writerows(rpagg_summaries)

    # --- Write candidate_summary.csv (FAZIS 3) ---
    candidate_summary_path = REPORT_DIR / "candidate_summary.csv"
    candidate_fields = [
        "symbol", "timeframe", "total_count",
        "buy_candidate_count", "sell_candidate_count", "neutral_count",
        "low_zone_count", "mid_zone_count", "high_zone_count",
        "below_range_count", "above_range_count", "no_data_count"
    ]
    # Build per symbol/tf stats from all_rows
    from collections import defaultdict
    cand_stats: dict[tuple, dict] = defaultdict(lambda: {f: 0 for f in candidate_fields})
    for row in all_rows:
        key = (row["symbol"], row["timeframe"])
        s = cand_stats[key]
        s["symbol"]    = row["symbol"]
        s["timeframe"] = row["timeframe"]
        s["total_count"] += 1
        sig = row.get("candidate_signal", "NEUTRAL")
        if sig == "BUY_CANDIDATE":
            s["buy_candidate_count"] += 1
        elif sig == "SELL_CANDIDATE":
            s["sell_candidate_count"] += 1
        else:
            s["neutral_count"] += 1
        zone = row.get("zone", "NO_DATA")
        if zone == "LOW_ZONE":       s["low_zone_count"]    += 1
        elif zone == "MID_ZONE":     s["mid_zone_count"]    += 1
        elif zone == "HIGH_ZONE":    s["high_zone_count"]   += 1
        elif zone == "BELOW_RANGE":  s["below_range_count"] += 1
        elif zone == "ABOVE_RANGE":  s["above_range_count"] += 1
        else:                        s["no_data_count"]     += 1

    with candidate_summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=candidate_fields, delimiter=";")
        writer.writeheader()
        writer.writerows(cand_stats.values())

    # --- Write candidate_audit.csv (FÁZIS 4) ---
    candidate_audit_path = REPORT_DIR / "candidate_audit.csv"
    audit_fields = [
        "timestamp", "symbol", "timeframe",
        "candidate_signal", "signal_reason", "close_at_signal",
        "return_4",  "return_12",  "return_24",  "return_48",
        "winner_4",  "winner_12",  "winner_24",  "winner_48",
    ]
    if all_audit_dfs:
        combined_audit = pd.concat(all_audit_dfs, ignore_index=True)
        combined_audit.to_csv(
            candidate_audit_path, sep=";", index=False,
            columns=audit_fields, float_format="%.6f"
        )

        # --- Write candidate_performance.csv (FÁZIS 4) ---
        perf_df = build_performance_summary(combined_audit)
        perf_path = REPORT_DIR / "candidate_performance.csv"
        perf_fields = [
            "candidate_signal", "count",
            "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
            "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
        ]
        perf_df.to_csv(perf_path, sep=";", index=False, columns=perf_fields)
    else:
        combined_audit = pd.DataFrame()
        perf_path = None

    state = load_state()
    state["last_run"] = utc_now()
    save_state(state)

    print(f"Kész: {summary_path}")
    print(f"Kész: {audit_path}")
    print(f"Kész: {replay_path}")
    if rpagg_summaries:
        print(f"Kész: {REPORT_DIR / 'rpagg_summary.csv'}")
    print(f"Kész: {candidate_summary_path}")
    if all_audit_dfs:
        print(f"Kész: {candidate_audit_path}  ({len(combined_audit)} sor)")
        print(f"Kész: {perf_path}")


if __name__ == "__main__":
    main()
