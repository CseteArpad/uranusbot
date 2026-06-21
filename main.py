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
from app.research_engine import (
    enrich_with_forward_closes,
    compute_research_stats,
    compute_signal_reason_stats,
    build_top10,
)
from app.research_dimension_engine import (
    compute_symbol_performance,
    compute_timeframe_performance,
    compute_symbol_timeframe_performance,
    compute_signal_reason_performance,
    build_best_dimensions,
)
from app.edge_finder_engine import compute_edge_grid, build_top_edges
from app.edge_validation_engine import validate_edges, build_validated_edges
from app.shadow_edge_engine import (
    compute_shadow_signals,
    compute_shadow_performance,
    compute_live_edge_tracking,
    build_promotion_candidates,
)

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

    # RPagg + Bias + FÁZIS 4 audit + FÁZIS 5 research — per symbol, cross-TF merge
    all_rows = []
    all_audit_dfs    = []
    all_research_dfs = []
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
                rdf = enrich_with_forward_closes(tf_rows_list, df_c, symbol, tf)
                if not rdf.empty:
                    all_research_dfs.append(rdf)
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

    # --- Write FÁZIS 5 research reports ---
    research_path      = REPORT_DIR / "research_summary.csv"
    sig_reason_path    = REPORT_DIR / "signal_reason_summary.csv"
    top10_path         = REPORT_DIR / "top10_conditions.csv"

    research_cols = [
        "condition", "count",
        "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
        "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
    ]
    if all_research_dfs:
        combined_research = pd.concat(all_research_dfs, ignore_index=True)

        research_df = compute_research_stats(combined_research)
        research_df.to_csv(research_path, sep=";", index=False, columns=research_cols)

        sig_reason_df = compute_signal_reason_stats(combined_research)
        sig_reason_df.to_csv(sig_reason_path, sep=";", index=False)

        top10_df = build_top10(research_df)
        top10_df.to_csv(top10_path, sep=";", index=False, columns=research_cols)
    else:
        combined_research = pd.DataFrame()
        research_df = sig_reason_df = top10_df = pd.DataFrame()

    # --- Write FÁZIS 6A dimension reports ---
    sym_perf_path    = REPORT_DIR / "symbol_performance.csv"
    tf_perf_path     = REPORT_DIR / "timeframe_performance.csv"
    symtf_perf_path  = REPORT_DIR / "symbol_timeframe_performance.csv"
    best_dim_path    = REPORT_DIR / "best_dimensions.csv"

    perf_all_cols = [
        "count",
        "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
        "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
    ]
    best_dim_cols = [
        "dimension_type", "dimension_value",
        "count",
        "avg_return_4",  "avg_return_12",  "avg_return_24",  "avg_return_48",
        "winrate_4",     "winrate_12",     "winrate_24",     "winrate_48",
    ]

    if not combined_research.empty:
        sym_perf_df   = compute_symbol_performance(combined_research)
        tf_perf_df    = compute_timeframe_performance(combined_research)
        symtf_perf_df = compute_symbol_timeframe_performance(combined_research)
        srp_df        = compute_signal_reason_performance(combined_research)
        best_dim_df   = build_best_dimensions(
            sym_perf_df, tf_perf_df, symtf_perf_df, srp_df
        )

        sym_perf_df.to_csv(
            sym_perf_path, sep=";", index=False,
            columns=["symbol"] + perf_all_cols
        )
        tf_perf_df.to_csv(
            tf_perf_path, sep=";", index=False,
            columns=["timeframe"] + perf_all_cols
        )
        symtf_perf_df.to_csv(symtf_perf_path, sep=";", index=False)
        best_dim_df.to_csv(best_dim_path, sep=";", index=False)

        # --- Write FÁZIS 6B edge finder reports ---
        edge_path     = REPORT_DIR / "edge_finder.csv"
        top_edge_path = REPORT_DIR / "top_edges.csv"

        edge_df     = compute_edge_grid(combined_research)
        top_edge_df = build_top_edges(edge_df)

        edge_df.to_csv(edge_path, sep=";", index=False)
        top_edge_df.to_csv(top_edge_path, sep=";", index=False)

        # --- Write FÁZIS 6C edge validation reports ---
        edge_validation_path  = REPORT_DIR / "edge_validation.csv"
        validated_edges_path  = REPORT_DIR / "validated_edges.csv"

        ab_edges = top_edge_df[
            top_edge_df["EDGE_GRADE"].isin({"A", "B"})
        ].copy() if not top_edge_df.empty else pd.DataFrame()

        validation_df  = validate_edges(ab_edges, combined_research)
        validated_df   = build_validated_edges(validation_df)

        validation_df.to_csv(edge_validation_path,  sep=";", index=False)
        validated_df.to_csv(validated_edges_path,   sep=";", index=False)

        # --- Write FÁZIS 6D shadow edge tracking reports ---
        shadow_signals_path    = REPORT_DIR / "shadow_signals.csv"
        shadow_perf_path       = REPORT_DIR / "shadow_performance.csv"
        live_tracking_path     = REPORT_DIR / "live_edge_tracking.csv"
        promotion_path         = REPORT_DIR / "promotion_candidates.csv"

        shadow_df      = compute_shadow_signals(validated_df, combined_research)
        shadow_perf_df = compute_shadow_performance(shadow_df)
        live_track_df  = compute_live_edge_tracking(shadow_df)
        promotion_df   = build_promotion_candidates(live_track_df)

        shadow_df.to_csv(shadow_signals_path, sep=";", index=False,
                         float_format="%.6f")
        shadow_perf_df.to_csv(shadow_perf_path,   sep=";", index=False)
        live_track_df.to_csv(live_tracking_path,  sep=";", index=False)
        promotion_df.to_csv(promotion_path,        sep=";", index=False)
    else:
        sym_perf_df = tf_perf_df = symtf_perf_df = best_dim_df = pd.DataFrame()
        edge_df = top_edge_df = pd.DataFrame()
        validation_df = validated_df = pd.DataFrame()
        shadow_df = shadow_perf_df = live_track_df = promotion_df = pd.DataFrame()
        edge_path = top_edge_path = None
        edge_validation_path = validated_edges_path = None
        shadow_signals_path = shadow_perf_path = live_tracking_path = promotion_path = None

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
    if all_research_dfs:
        n_cond = len(research_df)
        n_res  = len(combined_research)
        print(f"Kész: {research_path}  ({n_cond} feltétel, {n_res} sor)")
        print(f"Kész: {sig_reason_path}")
        print(f"Kész: {top10_path}  ({len(top10_df)} sor)")
    if not combined_research.empty:
        print(f"Kész: {sym_perf_path}  ({len(sym_perf_df)} sor)")
        print(f"Kész: {tf_perf_path}  ({len(tf_perf_df)} sor)")
        print(f"Kész: {symtf_perf_path}  ({len(symtf_perf_df)} sor)")
        print(f"Kész: {best_dim_path}  ({len(best_dim_df)} sor)")
        if edge_path:
            n_a = int((top_edge_df["EDGE_GRADE"] == "A").sum()) if not top_edge_df.empty else 0
            n_b = int((top_edge_df["EDGE_GRADE"] == "B").sum()) if not top_edge_df.empty else 0
            n_c = int((top_edge_df["EDGE_GRADE"] == "C").sum()) if not top_edge_df.empty else 0
            print(f"Kész: {edge_path}  ({len(edge_df)} kombináció)")
            print(f"Kész: {top_edge_path}  (A={n_a} B={n_b} C={n_c})")
        if edge_validation_path:
            n_strong   = int((validation_df["EDGE_STABILITY"] == "STRONG").sum())   if not validation_df.empty else 0
            n_moderate = int((validation_df["EDGE_STABILITY"] == "MODERATE").sum()) if not validation_df.empty else 0
            n_weak     = int((validation_df["EDGE_STABILITY"] == "WEAK").sum())     if not validation_df.empty else 0
            print(f"Kész: {edge_validation_path}  (STRONG={n_strong} MODERATE={n_moderate} WEAK={n_weak})")
            print(f"Kész: {validated_edges_path}  ({len(validated_df)} sor)")
        if shadow_signals_path:
            n_shadow = len(shadow_df)
            n_promo  = len(promotion_df)
            print(f"Kész: {shadow_signals_path}  ({n_shadow} shadow jel)")
            print(f"Kész: {shadow_perf_path}")
            print(f"Kész: {live_tracking_path}  ({len(live_track_df)} edge)")
            print(f"Kész: {promotion_path}  ({n_promo} promotion jelölt)")


if __name__ == "__main__":
    main()
