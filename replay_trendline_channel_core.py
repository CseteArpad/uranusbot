#!/usr/bin/env python3
# replay_trendline_channel_core.py — FÁZIS 3E: dual trendline channel engine replay.
# Uses independent lower/upper trendlines instead of a parallel channel.
# Offline only.  No live trading, no orders, no state.json access.

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
import trendline_channel_engine as tce
import channel_trend_engine     as cte
import channel_state_engine     as cse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CANDLE_PATH = r"/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json"
REPORTS_DIR = "reports"
DEFAULT_REPLAY_DAYS = 180

DEFAULT_TF_CONFIG: List[dict] = [
    {"name": "1H",  "minutes": 60,   "lookback": 50, "pivot_left": 3, "pivot_right": 3},
    {"name": "4H",  "minutes": 240,  "lookback": 30, "pivot_left": 3, "pivot_right": 3},
    {"name": "12H", "minutes": 720,  "lookback": 20, "pivot_left": 2, "pivot_right": 2},
    {"name": "1D",  "minutes": 1440, "lookback": 14, "pivot_left": 2, "pivot_right": 2},
]

WEIGHTS: Dict[str, float] = {"1H": 0.10, "4H": 0.20, "12H": 0.30, "1D": 0.40}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Uranus Trendline Channel Core — dual trendline replay"
    )
    p.add_argument("--days",       type=int, default=DEFAULT_REPLAY_DAYS)
    p.add_argument("--pivot-1h",   type=int, default=None, dest="pivot_1h")
    p.add_argument("--pivot-4h",   type=int, default=None, dest="pivot_4h")
    p.add_argument("--pivot-12h",  type=int, default=None, dest="pivot_12h")
    p.add_argument("--pivot-1d",   type=int, default=None, dest="pivot_1d")
    p.add_argument("--max-pivots", type=int, default=6, dest="max_pivots",
                   help="Max recent pivots used per trendline (default 6)")
    return p.parse_args(argv)


def _apply_overrides(tf_config: List[dict], args: argparse.Namespace) -> List[dict]:
    override_map = {"1H": args.pivot_1h, "4H": args.pivot_4h,
                    "12H": args.pivot_12h, "1D": args.pivot_1d}
    result = []
    for tfc in tf_config:
        cfg = dict(tfc)
        ov  = override_map.get(cfg["name"])
        if ov is not None:
            cfg["pivot_left"] = cfg["pivot_right"] = ov
        result.append(cfg)
    return result


# ---------------------------------------------------------------------------
# Candle helpers
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
            buckets[bar_ts] = {"ts_ms": bar_ts, "open": c[1], "high": c[2],
                               "low": c[3], "close": c[4], "volume": c[5]}
        else:
            b = buckets[bar_ts]
            if c[2] > b["high"]: b["high"] = c[2]
            if c[3] < b["low"]:  b["low"]  = c[3]
            b["close"]   = c[4]
            b["volume"] += c[5]
    return sorted(buckets.values(), key=lambda x: x["ts_ms"])


def _build_channel_cache(
    tf_bars: List[dict],
    lookback: int,
    pivot_left: int,
    pivot_right: int,
    max_pivots: int,
) -> Tuple[Dict[int, dict], dict]:
    """Build a trendline channel for each completed TF bar and collect stats."""
    cache: Dict[int, dict] = {}
    invalid_reasons: Dict[str, int] = {r: 0 for r in tce.ALL_INVALID_REASONS}
    last_valid_ts_ms: Optional[int] = None
    last_invalid_reason: Optional[str] = None
    last_pl_count = last_ph_count = 0

    for i, bar in enumerate(tf_bars):
        start  = max(0, i - lookback)
        window = tf_bars[start:i]
        highs  = [b["high"] for b in window]
        lows   = [b["low"]  for b in window]
        ch     = tce.build_trendline_channel(highs, lows, pivot_left, pivot_right, max_pivots)
        cache[bar["ts_ms"]] = ch

        last_pl_count = ch.get("pivot_low_count",  0)
        last_ph_count = ch.get("pivot_high_count", 0)

        if ch["valid"]:
            last_valid_ts_ms = bar["ts_ms"]
        else:
            reason = ch.get("reason") or tce.REASON_OTHER_EXCEPTION
            if reason not in invalid_reasons:
                reason = tce.REASON_OTHER_EXCEPTION
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

def _compute_cp(close: float, ch: Optional[dict]) -> Optional[float]:
    if not ch or not ch.get("valid"):
        return None
    lower = ch.get("lower_now")
    upper = ch.get("upper_now")
    if lower is None or upper is None:
        return None
    try:
        return tce.channel_position(close, lower, upper)
    except ValueError:
        return None


def _cpagg(cp_vals: Dict[str, Optional[float]]) -> Optional[float]:
    wsum = wtot = 0.0
    for tf, cp in cp_vals.items():
        if cp is not None:
            w = WEIGHTS[tf]
            wsum += w * cp
            wtot += w
    return wsum / wtot if wtot > 0 else None


def _fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
    last   = result.get("last_values", {})

    tf_names = list(cfg["timeframes"].keys())
    W = 80

    def _pct(v):
        return f"{v:.4f}" if v is not None else "N/A"

    hdr_cols = "".join(f"{tf:>8s}" for tf in tf_names)
    sep      = "  " + "-" * (34 + 8 * len(tf_names))

    lines = [
        "=" * W,
        "  Uranus Trendline Channel Core — FÁZIS 3E Replay",
        "=" * W,
        "",
        f"  Candle file   : {cfg['candle_path']}",
        f"  Replay window : {cfg['replay_start_utc']}  to  {cfg['replay_end_utc']}",
        f"  Replay days   : {cfg['replay_days']}",
        f"  Max pivots    : {cfg['max_pivots']}",
        "",
        "  Timeframe settings:",
    ]
    for tf in tf_names:
        tc = cfg["timeframes"][tf]
        lines.append(
            f"    {tf:<5s} minutes={tc['minutes']:<6d} lookback={tc['lookback']:<4d}"
            f" pivot_left={tc['pivot_left']}  pivot_right={tc['pivot_right']}"
        )

    # Validity overview
    lines += ["", "-" * W, "  Channel validity per timeframe", "-" * W]
    for tf in tf_names:
        valid = summ[f"cp_valid_ticks_{tf}"]
        total = summ["candles_1m_processed"]
        pct   = 100.0 * valid / total if total else 0.0
        fail  = sum(inv_by.get(tf, {}).values())
        lines.append(
            f"  {tf:>4s}  cp_valid={valid:>7d}/{total}  ({pct:5.1f}%)  build_invalid={fail}"
        )

    # Invalid reason table
    lines += [
        "", "-" * W, "  Invalid trendline reasons per timeframe", "-" * W,
        f"  {'Reason':<34s}{hdr_cols}", sep,
    ]
    for reason in tce.ALL_INVALID_REASONS:
        counts = [inv_by.get(tf, {}).get(reason, 0) for tf in tf_names]
        lines.append(f"  {reason:<34s}" + "".join(f"{c:>8d}" for c in counts))

    # Debug counts
    lines += [
        "", "-" * W, "  Debug counts per timeframe", "-" * W,
        f"  {'Metric':<34s}{hdr_cols}", sep,
    ]
    for label, fn in [
        ("pivot_left",            lambda tf: dbg_by.get(tf, {}).get("pivot_left",  0)),
        ("pivot_right",           lambda tf: dbg_by.get(tf, {}).get("pivot_right", 0)),
        ("total_bars",            lambda tf: dbg_by.get(tf, {}).get("total_bars",  0)),
        ("last_pivot_low_count",  lambda tf: dbg_by.get(tf, {}).get("last_pivot_low_count",  0)),
        ("last_pivot_high_count", lambda tf: dbg_by.get(tf, {}).get("last_pivot_high_count", 0)),
        ("cp_valid_ticks",        lambda tf: dbg_by.get(tf, {}).get("cp_valid_ticks",        0)),
    ]:
        vals = [fn(tf) for tf in tf_names]
        lines.append(f"  {label:<34s}" + "".join(f"{v:>8d}" for v in vals))

    pct_vals = [dbg_by.get(tf, {}).get("cp_valid_pct", 0.0) for tf in tf_names]
    lines.append(f"  {'cp_valid_pct (%)':34s}" + "".join(f"{v:>8.1f}" for v in pct_vals))

    lines += [""]
    for tf in tf_names:
        d = dbg_by.get(tf, {})
        lines.append(f"  {tf}  last_valid_ts       = {d.get('last_valid_ts_utc') or 'N/A'}")
        lines.append(f"  {tf}  last_invalid_reason = {d.get('last_invalid_reason') or 'none'}")
    lines.append("")

    # Last values
    lines += [
        "-" * W, "  Last computed values", "-" * W,
        f"  {'TF':>4s}  {'lower_now':>12s}  {'upper_now':>12s}  {'width':>10s}  {'raw_cp':>8s}  {'eff_cp':>8s}",
    ]
    for tf in tf_names:
        lv = last.get(tf, {})
        lines.append(
            f"  {tf:>4s}  {_pct(lv.get('lower_now')):>12s}  "
            f"{_pct(lv.get('upper_now')):>12s}  "
            f"{_pct(lv.get('width_now')):>10s}  "
            f"{_pct(lv.get('raw_cp')):>8s}  "
            f"{_pct(lv.get('eff_cp')):>8s}"
        )
    lines += [
        "",
        f"  CPagg        = {_pct(summ.get('last_cpagg'))}",
        f"  TrendScore   = {_pct(summ.get('last_trend_score'))}   (0.4*CP_4H + 0.6*CP_12H)",
        f"  TrendState   = {summ.get('last_trend_state') or 'N/A'}",
        "",
        "=" * W,
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run(tf_config: List[dict], replay_days: int, max_pivots: int) -> dict:
    print(f"Loading candles from {CANDLE_PATH} ...")
    all_candles = _load_candles(CANDLE_PATH)
    print(f"  Total 1m candles: {len(all_candles)}")

    replay_ms    = replay_days * 24 * 60 * 60 * 1000
    last_ts_ms   = int(all_candles[-1][0])
    replay_start = last_ts_ms - replay_ms
    warmup_ms    = max(tfc["minutes"] * tfc["lookback"] for tfc in tf_config) * 60 * 1000
    data_start   = replay_start - warmup_ms

    working = [c for c in all_candles if int(c[0]) >= data_start]
    replay  = [c for c in working    if int(c[0]) >= replay_start]
    warmup_days = warmup_ms // (60 * 60 * 24 * 1000)
    print(f"  Working: {len(working)} candles (incl. {warmup_days}d warmup)")
    print(f"  Replay : {len(replay)} candles ({replay_days}d)")

    # Build channel caches
    ch_caches: Dict[str, Dict[int, dict]] = {}
    tf_stats:  Dict[str, dict]            = {}
    bar_ms:    Dict[str, int]             = {}

    for tfc in tf_config:
        tf   = tfc["name"]
        bars = _resample(working, tfc["minutes"])
        print(f"  {tf:>4s}: {len(bars)} bars  "
              f"(pivot {tfc['pivot_left']}/{tfc['pivot_right']}  max_pivots={max_pivots})")
        cache, stats = _build_channel_cache(
            bars, tfc["lookback"], tfc["pivot_left"], tfc["pivot_right"], max_pivots
        )
        ch_caches[tf] = cache
        tf_stats[tf]  = stats
        bar_ms[tf]    = tfc["minutes"] * 60 * 1000

    # Replay loop
    ch_state        = cse.ChannelState()
    cp_valid_ticks: Dict[str, int] = {tfc["name"]: 0 for tfc in tf_config}
    last_bounds:    Dict[str, dict] = {tfc["name"]: {} for tfc in tf_config}
    last_row: Optional[dict] = None

    print(f"\nReplaying {len(replay)} candles ...")
    for i, candle in enumerate(replay):
        ts_ms  = int(candle[0])
        close  = float(candle[4])
        ts_utc = _fmt_ts(ts_ms)

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
                last_bounds[tf_name] = {
                    "lower_now": ch.get("lower_now"),
                    "upper_now": ch.get("upper_now"),
                    "width_now": ch.get("width_now"),
                }

        cse.update_channel_state(ch_state, ts_utc, raw_cp)
        eff_cp = cse.effective_cp_by_tf(ch_state)

        agg    = _cpagg(eff_cp)
        tscore = cte.compute_trend_score(eff_cp.get("4H"), eff_cp.get("12H"))
        trend  = cte.compute_trend_state(eff_cp.get("4H"), eff_cp.get("12H"))

        last_row = {
            "ts_ms": ts_ms, "ts_utc": ts_utc, "close": close,
            "raw_cp": {tf: _r(raw_cp.get(tf)) for tf in ("1H", "4H", "12H", "1D")},
            "eff_cp": {tf: _r(eff_cp.get(tf)) for tf in ("1H", "4H", "12H", "1D")},
            "cpagg": _r(agg), "trend_score": _r(tscore), "trend_state": trend,
        }

        if (i + 1) % 10000 == 0:
            print(f"  ... {i + 1}/{len(replay)} ticks  CPagg={_r(agg)}  {trend}")

    print(f"  Replay complete.")

    n_rows = len(replay)
    debug_by: Dict[str, dict] = {}
    for tfc in tf_config:
        tf  = tfc["name"]
        st  = tf_stats[tf]
        vt  = cp_valid_ticks[tf]
        debug_by[tf] = {
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

    # Last values per TF (bounds + raw/eff CP)
    last_values: Dict[str, dict] = {}
    for tfc in tf_config:
        tf = tfc["name"]
        last_values[tf] = {
            **last_bounds.get(tf, {}),
            "raw_cp": last_row["raw_cp"].get(tf) if last_row else None,
            "eff_cp": last_row["eff_cp"].get(tf) if last_row else None,
        }

    result = {
        "config": {
            "candle_path":     CANDLE_PATH,
            "replay_days":     replay_days,
            "replay_start_utc": _fmt_ts(replay_start),
            "replay_end_utc":   _fmt_ts(last_ts_ms),
            "max_pivots":      max_pivots,
            "weights":         WEIGHTS,
            "timeframes": {
                tfc["name"]: {k: tfc[k] for k in
                              ("minutes", "lookback", "pivot_left", "pivot_right")}
                for tfc in tf_config
            },
        },
        "summary": {
            "candles_1m_processed": n_rows,
            **{f"cp_valid_ticks_{tfc['name']}": cp_valid_ticks[tfc["name"]] for tfc in tf_config},
            **{f"channel_build_invalid_{tfc['name']}":
               sum(tf_stats[tfc["name"]]["invalid_reasons"].values()) for tfc in tf_config},
            "last_cpagg":        last_row["cpagg"]       if last_row else None,
            "last_trend_score":  last_row["trend_score"] if last_row else None,
            "last_trend_state":  last_row["trend_state"] if last_row else None,
        },
        "invalid_reasons_by_timeframe": {
            tfc["name"]: dict(tf_stats[tfc["name"]]["invalid_reasons"]) for tfc in tf_config
        },
        "debug_counts_by_timeframe": debug_by,
        "last_values": last_values,
    }
    return result


def main(argv=None) -> None:
    args      = _parse_args(argv)
    tf_config = _apply_overrides([dict(tfc) for tfc in DEFAULT_TF_CONFIG], args)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    json_path = os.path.join(REPORTS_DIR, f"trendline_channel_core_xrp_{args.days}d.json")
    txt_path  = os.path.join(REPORTS_DIR, f"trendline_channel_core_xrp_{args.days}d.txt")

    result = run(tf_config=tf_config, replay_days=args.days, max_pivots=args.max_pivots)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nJSON report: {json_path}")

    _write_txt(txt_path, result)
    print(f"TXT  report: {txt_path}")

    s      = result["summary"]
    dbg_by = result["debug_counts_by_timeframe"]
    inv_by = result["invalid_reasons_by_timeframe"]

    print("\n" + "=" * 64)
    print("  Trendline Channel Core — Summary")
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

    non_zero = {
        r: {tf: inv_by[tf].get(r, 0) for tf in dbg_by}
        for r in tce.ALL_INVALID_REASONS
        if any(inv_by[tf].get(r, 0) > 0 for tf in dbg_by)
    }
    if non_zero:
        print("\n  Invalid reasons (non-zero):")
        for reason, counts in non_zero.items():
            parts = "  ".join(f"{tf}={c}" for tf, c in counts.items() if c > 0)
            print(f"    {reason:<34s} {parts}")
    print("=" * 64)


if __name__ == "__main__":
    main()
