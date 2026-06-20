#!/usr/bin/env python3
# replay_channel_core.py — FÁZIS 1B: multi-timeframe pivot channel engine replay
# Per-timeframe pivot strength, CLI --days / --pivot-* overrides.  No real orders.

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent / "app"))
import channel_engine as ce
import channel_trend_engine as cte
import channel_state_engine as cse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CANDLE_PATH = r"/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json"
REPORTS_DIR = "reports"

DEFAULT_REPLAY_DAYS = 30

# Per-timeframe defaults: higher TFs use smaller pivot windows (fewer bars
# available, so strict left=3/right=3 produces too few pivots on 12H / 1D).
DEFAULT_TF_CONFIG: List[dict] = [
    {"name": "1H",  "minutes": 60,   "lookback": 50, "pivot_left": 3, "pivot_right": 3},
    {"name": "4H",  "minutes": 240,  "lookback": 30, "pivot_left": 3, "pivot_right": 3},
    {"name": "12H", "minutes": 720,  "lookback": 20, "pivot_left": 2, "pivot_right": 2},
    {"name": "1D",  "minutes": 1440, "lookback": 14, "pivot_left": 2, "pivot_right": 2},
]

WEIGHTS: Dict[str, float] = {"1H": 0.10, "4H": 0.20, "12H": 0.30, "1D": 0.40}

# CPagg thresholds — mirrored from channel_trend_engine for report/config output
BUY_ZONE_THRESHOLD  = cte.BUY_ZONE_THRESHOLD
SELL_ZONE_THRESHOLD = cte.SELL_ZONE_THRESHOLD


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Uranus Channel Core — historical pivot channel replay"
    )
    p.add_argument("--days",       type=int,   default=DEFAULT_REPLAY_DAYS,
                   help="Replay window in days (default: 30)")
    p.add_argument("--pivot-1h",   type=int,   default=None, dest="pivot_1h",
                   metavar="N", help="Pivot left=right for 1H (default: 3)")
    p.add_argument("--pivot-4h",   type=int,   default=None, dest="pivot_4h",
                   metavar="N", help="Pivot left=right for 4H (default: 3)")
    p.add_argument("--pivot-12h",  type=int,   default=None, dest="pivot_12h",
                   metavar="N", help="Pivot left=right for 12H (default: 2)")
    p.add_argument("--pivot-1d",   type=int,   default=None, dest="pivot_1d",
                   metavar="N", help="Pivot left=right for 1D (default: 2)")
    return p.parse_args(argv)


def _apply_overrides(tf_config: List[dict], args: argparse.Namespace) -> List[dict]:
    """Return a new list with per-TF pivot overrides applied (if provided)."""
    override_map = {
        "1H":  args.pivot_1h,
        "4H":  args.pivot_4h,
        "12H": args.pivot_12h,
        "1D":  args.pivot_1d,
    }
    result = []
    for tfc in tf_config:
        cfg = dict(tfc)
        ov = override_map.get(cfg["name"])
        if ov is not None:
            cfg["pivot_left"]  = ov
            cfg["pivot_right"] = ov
        result.append(cfg)
    return result


# ---------------------------------------------------------------------------
# Candle loading & resampling
# ---------------------------------------------------------------------------

def _load_candles(path: str) -> List:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return sorted(data, key=lambda c: c[0])


def _resample(candles_1m: List, tf_minutes: int) -> List[dict]:
    ms_per_bar = tf_minutes * 60 * 1000
    buckets: Dict[int, dict] = {}
    for c in candles_1m:
        bar_ts = (int(c[0]) // ms_per_bar) * ms_per_bar
        if bar_ts not in buckets:
            buckets[bar_ts] = {
                "ts_ms":  bar_ts,
                "open":   c[1],
                "high":   c[2],
                "low":    c[3],
                "close":  c[4],
                "volume": c[5],
            }
        else:
            b = buckets[bar_ts]
            if c[2] > b["high"]:
                b["high"] = c[2]
            if c[3] < b["low"]:
                b["low"] = c[3]
            b["close"]   = c[4]
            b["volume"] += c[5]
    return sorted(buckets.values(), key=lambda x: x["ts_ms"])


# ---------------------------------------------------------------------------
# Channel cache with per-TF pivot params
# ---------------------------------------------------------------------------

def _empty_reason_counts() -> Dict[str, int]:
    return {r: 0 for r in ce.ALL_INVALID_REASONS}


def _build_channel_cache(
    tf_bars: List[dict],
    lookback: int,
    pivot_left: int,
    pivot_right: int,
) -> Tuple[Dict[int, dict], dict]:
    """
    Build a channel for each completed TF bar and collect diagnostics.
    Returns (cache, stats).  cache is keyed by bar ts_ms.
    """
    cache: Dict[int, dict] = {}
    invalid_reasons  = _empty_reason_counts()
    last_valid_ts_ms: Optional[int]    = None
    last_invalid_reason: Optional[str] = None
    last_pl_count = 0
    last_ph_count = 0

    for i, bar in enumerate(tf_bars):
        start  = max(0, i - lookback)
        window = tf_bars[start:i]

        highs = [b["high"] for b in window]
        lows  = [b["low"]  for b in window]
        ch = ce.build_parallel_channel(highs, lows, pivot_left, pivot_right)

        cache[bar["ts_ms"]] = ch

        last_pl_count = ch.get("pivot_low_count",  0)
        last_ph_count = ch.get("pivot_high_count", 0)

        if ch["valid"]:
            last_valid_ts_ms = bar["ts_ms"]
        else:
            reason = ch.get("reason") or ce.REASON_OTHER_EXCEPTION
            if reason not in invalid_reasons:
                reason = ce.REASON_OTHER_EXCEPTION
            invalid_reasons[reason] += 1
            last_invalid_reason = reason

    return cache, {
        "total_bars":            len(tf_bars),
        "invalid_reasons":       invalid_reasons,
        "last_valid_ts_ms":      last_valid_ts_ms,
        "last_valid_ts_utc":     _fmt_ts(last_valid_ts_ms) if last_valid_ts_ms else None,
        "last_invalid_reason":   last_invalid_reason,
        "last_pivot_low_count":  last_pl_count,
        "last_pivot_high_count": last_ph_count,
    }


# ---------------------------------------------------------------------------
# Per-candle helpers
# ---------------------------------------------------------------------------

def _compute_cp(close: float, ch: dict) -> Optional[float]:
    if not ch or not ch.get("valid"):
        return None
    n = ch["n_bars"]
    if n == 0:
        return None
    lo, up = ce.evaluate_channel_at(ch, n - 1)
    try:
        return ce.channel_position(close, lo, up)
    except ValueError:
        return None


def _cpagg(cp_vals: Dict[str, Optional[float]]) -> Optional[float]:
    wsum = 0.0
    wtot = 0.0
    for tf, cp in cp_vals.items():
        if cp is not None:
            w = WEIGHTS[tf]
            wsum += w * cp
            wtot += w
    return wsum / wtot if wtot > 0 else None


def _fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(v: Optional[float], places: int = 4) -> Optional[float]:
    return round(v, places) if v is not None else None


# ---------------------------------------------------------------------------
# TXT report
# ---------------------------------------------------------------------------

def _write_txt(path: str, result: dict) -> None:
    cfg    = result["config"]
    summ   = result["summary"]
    inv_by = result.get("invalid_reasons_by_timeframe", {})
    dbg_by = result.get("debug_counts_by_timeframe", {})

    tf_names = list(cfg["timeframes"].keys())

    def _pct(v):
        return f"{v:.4f}" if v is not None else "N/A"

    def _col4(vals, width=7, fmt="{:>{w}d}"):
        return "".join(fmt.format(v, w=width) for v in vals)

    def _col4f(vals, width=7, decimals=1):
        return "".join(f"{v:>{width}.{decimals}f}" for v in vals)

    W = 76
    lines = [
        "=" * W,
        "  Uranus Channel Core — FÁZIS 2 Replay",
        "=" * W,
        "",
        f"  Candle file   : {cfg['candle_path']}",
        f"  Replay window : {cfg['replay_start_utc']}  to  {cfg['replay_end_utc']}",
        f"  Replay days   : {cfg['replay_days']}",
        "",
        "  Timeframe settings:",
    ]
    for tf in tf_names:
        tc = cfg["timeframes"][tf]
        lines.append(
            f"    {tf:<5s} minutes={tc['minutes']:<6d} lookback={tc['lookback']:<4d}"
            f" pivot_left={tc['pivot_left']}  pivot_right={tc['pivot_right']}"
        )

    # TrendScore thresholds
    lines += [
        "",
        "-" * W,
        "  TrendScore thresholds  (TrendScore = 0.4*CP_4H + 0.6*CP_12H)",
        "-" * W,
        f"  LONG    >= {cfg.get('long_threshold',  0.65):.2f}",
        f"  SHORT   <= {cfg.get('short_threshold', 0.35):.2f}",
        "  SIDEWAYS   otherwise",
        "",
    ]

    # Validity overview
    lines += ["-" * W, "  Channel validity per timeframe", "-" * W]
    for tf in tf_names:
        valid = summ[f"cp_valid_ticks_{tf}"]
        total = summ["candles_1m_processed"]
        pct   = 100.0 * valid / total if total else 0.0
        fail  = sum(inv_by.get(tf, {}).values())
        lines.append(
            f"  {tf:>4s}  cp_valid={valid:>7d}/{total}  ({pct:5.1f}%)  build_invalid={fail}"
        )

    # Invalid reason breakdown table
    hdr_cols = "".join(f"{tf:>7s}" for tf in tf_names)
    sep = "  " + "-" * (34 + 7 * len(tf_names))
    lines += [
        "",
        "-" * W,
        "  Invalid channel reasons per timeframe",
        "-" * W,
        f"  {'Reason':<34s}{hdr_cols}",
        sep,
    ]
    for reason in ce.ALL_INVALID_REASONS:
        counts = [inv_by.get(tf, {}).get(reason, 0) for tf in tf_names]
        lines.append(f"  {reason:<34s}" + _col4(counts))

    # Debug counts table
    lines += [
        "",
        "-" * W,
        "  Debug counts per timeframe",
        "-" * W,
        f"  {'Metric':<34s}{hdr_cols}",
        sep,
    ]
    int_metrics = [
        ("pivot_left",            lambda tf: dbg_by.get(tf, {}).get("pivot_left",            0)),
        ("pivot_right",           lambda tf: dbg_by.get(tf, {}).get("pivot_right",           0)),
        ("total_bars",            lambda tf: dbg_by.get(tf, {}).get("total_bars",            0)),
        ("last_pivot_low_count",  lambda tf: dbg_by.get(tf, {}).get("last_pivot_low_count",  0)),
        ("last_pivot_high_count", lambda tf: dbg_by.get(tf, {}).get("last_pivot_high_count", 0)),
        ("cp_valid_ticks",        lambda tf: dbg_by.get(tf, {}).get("cp_valid_ticks",        0)),
    ]
    for label, fn in int_metrics:
        vals = [fn(tf) for tf in tf_names]
        lines.append(f"  {label:<34s}" + _col4(vals))

    # cp_valid_pct (float row)
    pct_vals = [dbg_by.get(tf, {}).get("cp_valid_pct", 0.0) for tf in tf_names]
    lines.append(f"  {'cp_valid_pct':<34s}" + _col4f(pct_vals, decimals=1))

    # Per-TF last ts / reason
    lines += [""]
    for tf in tf_names:
        d = dbg_by.get(tf, {})
        lines.append(f"  {tf}  last_valid_ts       = {d.get('last_valid_ts_utc') or 'N/A'}")
        lines.append(f"  {tf}  last_invalid_reason = {d.get('last_invalid_reason') or 'none'}")
    lines.append("")

    # Last computed values
    lines += [
        "-" * W,
        "  Last computed values (effective = last-valid fallback)",
        "-" * W,
        f"  {'':8s}{'Raw CP':>10s}  {'Eff CP':>10s}  {'Fallback ticks':>14s}  {'Final stale':>11s}",
    ]
    for tf in tf_names:
        raw_key  = f"last_raw_cp_{tf.lower()}"
        eff_key  = f"last_cp_{tf.lower()}"
        fb_key   = f"fallback_count_{tf}"
        st_key   = f"final_stale_ticks_{tf}"
        raw_v    = _pct(summ.get(raw_key))
        eff_v    = _pct(summ.get(eff_key))
        fb_v     = summ.get(fb_key, 0)
        st_v     = summ.get(st_key, 0)
        lines.append(
            f"  {tf:<6s}  {raw_v:>10s}  {eff_v:>10s}  {fb_v:>14d}  {st_v:>11d}"
        )
    lines += [
        f"  CPagg        = {_pct(summ['last_cpagg'])}",
        f"  TrendScore   = {_pct(summ['last_trend_score'])}   (0.4*CP_4H + 0.6*CP_12H)",
        f"  TrendState   = {summ['last_trend_state'] or 'N/A'}",
        f"  Action       = {summ['last_candidate_action'] or 'N/A'}",
        "",
    ]

    # CP row tables
    def _row_line(r: dict) -> str:
        def _fv(v):
            return f"{v:+.4f}" if v is not None else "  N/A "
        return (
            f"  {r['ts_utc']}  "
            f"{_fv(r['cp_1h'])}  {_fv(r['cp_4h'])}  "
            f"{_fv(r['cp_12h'])}  {_fv(r['cp_1d'])}  "
            f"{_fv(r['cpagg'])}  {_fv(r['trend_score'])}  "
            f"{r['trend_state']:>9s}  "
            f"{r['candidate_action']:>9s}"
        )

    header = (
        "  ts_utc                  CP_1H    CP_4H   CP_12H    CP_1D"
        "    CPagg   TSCORE  TREND_ST  CAND_ACT"
    )

    first20 = result.get("cp_rows_first_20", [])
    last20  = result.get("cp_rows_last_20",  [])

    if first20:
        lines += ["-" * W, "  First 20 rows", "-" * W, header]
        lines += [_row_line(r) for r in first20]

    if last20:
        lines += ["", "-" * W, "  Last 20 rows", "-" * W, header]
        lines += [_row_line(r) for r in last20]

    lines += ["", "=" * W]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run(tf_config: List[dict] = None, replay_days: int = DEFAULT_REPLAY_DAYS) -> dict:
    """
    Run the channel core replay.

    Args:
        tf_config:   list of TF config dicts (name, minutes, lookback,
                     pivot_left, pivot_right).  Defaults to DEFAULT_TF_CONFIG.
        replay_days: number of trailing calendar days to replay.
    """
    if tf_config is None:
        tf_config = [dict(tfc) for tfc in DEFAULT_TF_CONFIG]

    print(f"Loading candles from {CANDLE_PATH} ...")
    all_candles = _load_candles(CANDLE_PATH)
    print(f"  Total 1m candles in file: {len(all_candles)}")

    replay_ms_span  = replay_days * 24 * 60 * 60 * 1000
    last_ts_ms      = int(all_candles[-1][0])
    replay_start_ms = last_ts_ms - replay_ms_span

    # Warmup: longest lookback in minutes across all TFs
    warmup_ms = max(tfc["minutes"] * tfc["lookback"] for tfc in tf_config) * 60 * 1000
    data_start_ms = replay_start_ms - warmup_ms

    working = [c for c in all_candles if int(c[0]) >= data_start_ms]
    replay  = [c for c in working    if int(c[0]) >= replay_start_ms]

    warmup_days = warmup_ms // (60 * 60 * 24 * 1000)
    print(f"  Working set : {len(working)} candles  (incl. {warmup_days}d warmup)")
    print(f"  Replay set  : {len(replay)} candles  ({replay_days}d)")

    # Resample & build channel caches
    tf_bars:   Dict[str, List[dict]]      = {}
    ch_caches: Dict[str, Dict[int, dict]] = {}
    tf_stats:  Dict[str, dict]            = {}

    for tfc in tf_config:
        tf_name = tfc["name"]
        bars = _resample(working, tfc["minutes"])
        tf_bars[tf_name] = bars
        print(f"  {tf_name:>4s}: {len(bars)} bars  (lookback={tfc['lookback']}  "
              f"pivot_left={tfc['pivot_left']}  pivot_right={tfc['pivot_right']})")
        cache, stats = _build_channel_cache(
            bars, tfc["lookback"], tfc["pivot_left"], tfc["pivot_right"]
        )
        ch_caches[tf_name] = cache
        tf_stats[tf_name]  = stats

    bar_ms: Dict[str, int] = {tfc["name"]: tfc["minutes"] * 60 * 1000 for tfc in tf_config}

    # Replay
    cp_valid_ticks:  Dict[str, int] = {tfc["name"]: 0 for tfc in tf_config}
    fallback_counts: Dict[str, int] = {tfc["name"]: 0 for tfc in tf_config}
    rows:     List[dict] = []
    last_row: Optional[dict] = None

    ch_state = cse.ChannelState()   # last-valid CP state across ticks

    print(f"\nReplaying {len(replay)} candles ...")
    for i, candle in enumerate(replay):
        ts_ms  = int(candle[0])
        close  = float(candle[4])
        ts_utc = _fmt_ts(ts_ms)

        # 1. Raw CP from the channel cache (may be None if no valid channel)
        raw_cp: Dict[str, Optional[float]] = {}
        for tfc in tf_config:
            tf_name = tfc["name"]
            bms     = bar_ms[tf_name]
            bar_ts  = (ts_ms // bms) * bms
            ch      = ch_caches[tf_name].get(bar_ts)
            cp      = _compute_cp(close, ch)
            raw_cp[tf_name] = cp
            if cp is not None:
                cp_valid_ticks[tf_name] += 1

        # 2. Update last-valid state; get effective (last-valid fallback) CPs
        cse.update_channel_state(ch_state, ts_utc, raw_cp)
        eff_cp = cse.effective_cp_by_tf(ch_state)

        # 3. Count ticks that used a last-valid fallback (raw None, eff not None)
        for tfc in tf_config:
            tf_name = tfc["name"]
            if raw_cp[tf_name] is None and eff_cp.get(tf_name) is not None:
                fallback_counts[tf_name] += 1

        # 4. Derive trend / action from EFFECTIVE CPs (avoids UNKNOWN during gaps)
        agg    = _cpagg(eff_cp)
        tscore = cte.compute_trend_score(eff_cp.get("4H"), eff_cp.get("12H"))
        trend  = cte.compute_trend_state(eff_cp.get("4H"), eff_cp.get("12H"))
        action = cte.compute_candidate_action(agg)

        row = {
            "ts_ms":            ts_ms,
            "ts_utc":           ts_utc,
            # raw CP (direct channel engine output; None when no valid channel)
            "raw_cp_1h":        _r(raw_cp.get("1H")),
            "raw_cp_4h":        _r(raw_cp.get("4H")),
            "raw_cp_12h":       _r(raw_cp.get("12H")),
            "raw_cp_1d":        _r(raw_cp.get("1D")),
            # effective CP (last-valid fallback applied)
            "cp_1h":            _r(eff_cp.get("1H")),
            "cp_4h":            _r(eff_cp.get("4H")),
            "cp_12h":           _r(eff_cp.get("12H")),
            "cp_1d":            _r(eff_cp.get("1D")),
            "cpagg":            _r(agg),
            "trend_score":      _r(tscore),
            "trend_state":      trend,
            "candidate_action": action,
        }
        rows.append(row)
        last_row = row

        if (i + 1) % 5000 == 0:
            print(f"  ... {i + 1}/{len(replay)} ticks  CPagg={_r(agg)}  {action}")

    print(f"  Replay complete. {len(rows)} rows.")
    final_stale = cse.staleness_by_tf(ch_state)

    n_rows = len(rows)

    # debug_counts per TF
    debug_by: Dict[str, dict] = {}
    for tfc in tf_config:
        tf_name = tfc["name"]
        st = tf_stats[tf_name]
        vt = cp_valid_ticks[tf_name]
        debug_by[tf_name] = {
            "pivot_left":            tfc["pivot_left"],
            "pivot_right":           tfc["pivot_right"],
            "total_bars":            st["total_bars"],
            "last_pivot_low_count":  st["last_pivot_low_count"],
            "last_pivot_high_count": st["last_pivot_high_count"],
            "cp_valid_ticks":        vt,
            "cp_valid_pct":          round(100.0 * vt / n_rows, 2) if n_rows else 0.0,
            "last_valid_ts_utc":     st["last_valid_ts_utc"],
            "last_invalid_reason":   st["last_invalid_reason"],
        }

    result = {
        "config": {
            "candle_path":         CANDLE_PATH,
            "replay_days":         replay_days,
            "replay_start_utc":    _fmt_ts(replay_start_ms),
            "replay_end_utc":      _fmt_ts(last_ts_ms),
            "weights":             WEIGHTS,
            "timeframes": {
                tfc["name"]: {
                    "minutes":     tfc["minutes"],
                    "lookback":    tfc["lookback"],
                    "pivot_left":  tfc["pivot_left"],
                    "pivot_right": tfc["pivot_right"],
                }
                for tfc in tf_config
            },
            "trend_score_formula": "0.4*CP_4H + 0.6*CP_12H",
            "long_threshold":      cte.LONG_THRESHOLD,
            "short_threshold":     cte.SHORT_THRESHOLD,
            "buy_zone_threshold":  BUY_ZONE_THRESHOLD,
            "sell_zone_threshold": SELL_ZONE_THRESHOLD,
        },
        "summary": {
            "candles_1m_processed": n_rows,
            **{f"cp_valid_ticks_{tfc['name']}": cp_valid_ticks[tfc["name"]] for tfc in tf_config},
            **{f"channel_build_invalid_{tfc['name']}":
               sum(tf_stats[tfc["name"]]["invalid_reasons"].values()) for tfc in tf_config},
            # effective CP (last-valid fallback applied)
            "last_cp_1h":            last_row["cp_1h"]            if last_row else None,
            "last_cp_4h":            last_row["cp_4h"]            if last_row else None,
            "last_cp_12h":           last_row["cp_12h"]           if last_row else None,
            "last_cp_1d":            last_row["cp_1d"]            if last_row else None,
            # raw CP at last tick
            "last_raw_cp_1h":        last_row["raw_cp_1h"]        if last_row else None,
            "last_raw_cp_4h":        last_row["raw_cp_4h"]        if last_row else None,
            "last_raw_cp_12h":       last_row["raw_cp_12h"]       if last_row else None,
            "last_raw_cp_1d":        last_row["raw_cp_1d"]        if last_row else None,
            "last_cpagg":            last_row["cpagg"]            if last_row else None,
            "last_trend_score":      last_row["trend_score"]      if last_row else None,
            "last_trend_state":      last_row["trend_state"]      if last_row else None,
            "last_candidate_action": last_row["candidate_action"] if last_row else None,
            # fallback stats
            **{f"fallback_count_{tfc['name']}": fallback_counts[tfc["name"]] for tfc in tf_config},
            **{f"final_stale_ticks_{tfc['name']}": final_stale[tfc["name"]] for tfc in tf_config},
        },
        "invalid_reasons_by_timeframe": {
            tfc["name"]: dict(tf_stats[tfc["name"]]["invalid_reasons"]) for tfc in tf_config
        },
        "debug_counts_by_timeframe": debug_by,
        "cp_rows_first_20": rows[:20],
        "cp_rows_last_20":  rows[-20:],
    }

    return result


def main(argv=None) -> None:
    args      = _parse_args(argv)
    tf_config = _apply_overrides([dict(tfc) for tfc in DEFAULT_TF_CONFIG], args)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    json_path = os.path.join(REPORTS_DIR, f"channel_core_xrp_{args.days}d.json")
    txt_path  = os.path.join(REPORTS_DIR, f"channel_core_xrp_{args.days}d.txt")

    result = run(tf_config=tf_config, replay_days=args.days)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nJSON report: {json_path}")

    _write_txt(txt_path, result)
    print(f"TXT  report: {txt_path}")

    # Console summary
    s      = result["summary"]
    dbg_by = result["debug_counts_by_timeframe"]
    inv_by = result["invalid_reasons_by_timeframe"]

    print("\n" + "=" * 64)
    print("  Channel Core — Summary")
    print("=" * 64)
    for tfc in tf_config:
        tf = tfc["name"]
        d  = dbg_by[tf]
        print(
            f"  {tf:>4s}  cp_valid={d['cp_valid_pct']:5.1f}%  "
            f"pivot={tfc['pivot_left']}/{tfc['pivot_right']}  "
            f"bars={d['total_bars']:<5d}  "
            f"pl={d['last_pivot_low_count']}  ph={d['last_pivot_high_count']}"
        )

    print(f"\n  Last CPagg      = {s['last_cpagg']}")
    print(f"  Last TrendScore = {s['last_trend_score']}  (0.4*CP_4H + 0.6*CP_12H)")
    print(f"  Last TrendState = {s['last_trend_state']}")
    print(f"  Last Action     = {s['last_candidate_action']}")

    print("\n  Fallback statistics (last-valid CP used when raw CP is None):")
    for tfc in tf_config:
        tf  = tfc["name"]
        fb  = s.get(f"fallback_count_{tf}", 0)
        stl = s.get(f"final_stale_ticks_{tf}", 0)
        total = s["candles_1m_processed"]
        pct   = 100.0 * fb / total if total else 0.0
        print(f"    {tf:>4s}  fallback_ticks={fb:>7d} ({pct:5.1f}%)  final_stale={stl}")

    non_zero = {
        reason: {tf: inv_by[tf].get(reason, 0) for tf in dbg_by}
        for reason in ce.ALL_INVALID_REASONS
        if any(inv_by[tf].get(reason, 0) > 0 for tf in dbg_by)
    }
    if non_zero:
        print("\n  Invalid reasons (non-zero):")
        for reason, counts in non_zero.items():
            parts = "  ".join(f"{tf}={c}" for tf, c in counts.items() if c > 0)
            print(f"    {reason:<34s} {parts}")

    print("=" * 64)


if __name__ == "__main__":
    main()
