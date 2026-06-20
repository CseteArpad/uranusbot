#!/usr/bin/env python3
# replay_channel_decision_backtest.py — FÁZIS 3B: 180-day Channel Decision PnL backtest.
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
import channel_engine       as ce
import channel_state_engine as cse
import channel_trend_engine as cte
import channel_decision_engine as cde

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

CANDLE_PATH       = r"/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json"
REPORTS_DIR       = "reports"
DEFAULT_DAYS      = 180
DEFAULT_EQUITY    = 19.25297507
DEFAULT_FEE       = 0.001        # 0.1 % per side
DEFAULT_SLIPPAGE  = 0.0

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
        description="Uranus Channel Decision — 3B PnL backtest"
    )
    p.add_argument("--days",      type=int,   default=DEFAULT_DAYS)
    p.add_argument("--equity",    type=float, default=DEFAULT_EQUITY,
                   help="Starting USDC equity")
    p.add_argument("--fee",       type=float, default=DEFAULT_FEE,
                   help="Fee per side, fraction (default 0.001 = 0.1%%)")
    p.add_argument("--slippage",  type=float, default=DEFAULT_SLIPPAGE,
                   help="Slippage per side, fraction (default 0.0)")
    p.add_argument("--verbose",   action="store_true",
                   help="Print each trade as it executes")
    p.add_argument("--smoke",     action="store_true",
                   help="Run smoke test only and exit")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Candle helpers  (reused from replay_channel_core.py pattern)
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
                "ts_ms": bar_ts, "open": c[1], "high": c[2],
                "low": c[3], "close": c[4], "volume": c[5],
            }
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
) -> Dict[int, dict]:
    cache: Dict[int, dict] = {}
    for i, bar in enumerate(tf_bars):
        start  = max(0, i - lookback)
        window = tf_bars[start:i]
        highs  = [b["high"] for b in window]
        lows   = [b["low"]  for b in window]
        ch     = ce.build_parallel_channel(highs, lows, pivot_left, pivot_right)
        cache[bar["ts_ms"]] = ch
    return cache


def _compute_cp(close: float, ch: Optional[dict]) -> Optional[float]:
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


def _channel_bounds_by_tf(
    close: float,
    tf_config: List[dict],
    ch_caches: Dict[str, Dict[int, dict]],
    bar_ms: Dict[str, int],
    ts_ms: int,
) -> Dict[str, Optional[dict]]:
    """
    For each TF return {"lower": ..., "upper": ..., "width": ..., "cp": ...}
    or None when no valid channel exists for that bar.
    """
    result: Dict[str, Optional[dict]] = {}
    for tfc in tf_config:
        tf_name = tfc["name"]
        bms     = bar_ms[tf_name]
        bar_ts  = (ts_ms // bms) * bms
        ch      = ch_caches[tf_name].get(bar_ts)
        if not ch or not ch.get("valid"):
            result[tf_name] = None
            continue
        n = ch["n_bars"]
        if n == 0:
            result[tf_name] = None
            continue
        lo, up = ce.evaluate_channel_at(ch, n - 1)
        try:
            cp = ce.channel_position(close, lo, up)
        except ValueError:
            result[tf_name] = None
            continue
        result[tf_name] = {
            "lower": _r(lo, 6),
            "upper": _r(up, 6),
            "width": _r(up - lo, 6),
            "cp":    _r(cp, 4),
        }
    return result


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
# Trade accounting
# ---------------------------------------------------------------------------

def _buy_cost(usdc: float, price: float, fee: float, slippage: float) -> Tuple[float, float, float]:
    """
    Return (xrp_acquired, usdc_spent, fee_usdc).
    Effective buy price includes slippage.  Fee charged on gross cost.
    """
    eff_price   = price * (1.0 + slippage)
    gross_xrp   = usdc / eff_price
    fee_usdc    = usdc * fee          # fee on the USDC amount committed
    xrp_acquired = gross_xrp * (1.0 - fee)   # fee deducted from received XRP converted back
    # Simpler and more common: fee paid in quote; we buy slightly fewer XRP.
    # xrp = usdc_committed * (1 - fee) / eff_price
    xrp_acquired = usdc * (1.0 - fee) / eff_price
    return xrp_acquired, usdc, fee_usdc


def _sell_proceeds(xrp: float, price: float, fee: float, slippage: float) -> Tuple[float, float, float]:
    """
    Return (usdc_received, gross_usdc, fee_usdc).
    Effective sell price includes slippage.  Fee charged on gross proceeds.
    """
    eff_price    = price * (1.0 - slippage)
    gross_usdc   = xrp * eff_price
    fee_usdc     = gross_usdc * fee
    usdc_received = gross_usdc * (1.0 - fee)
    return usdc_received, gross_usdc, fee_usdc


# ---------------------------------------------------------------------------
# Smoke test (no candle file required)
# ---------------------------------------------------------------------------

def _smoke_test() -> bool:
    """
    Run a tiny synthetic replay to verify accounting and decision logic.
    Returns True if all assertions pass.
    """
    fee      = 0.001
    slip     = 0.0
    equity   = 100.0

    # Simulate: BUY at price 0.50, SELL at price 0.60
    xrp_in, spent, fee_buy = _buy_cost(equity, 0.50, fee, slip)
    proceeds, gross, fee_sell = _sell_proceeds(xrp_in, 0.60, fee, slip)

    net_pnl = proceeds - equity
    assert net_pnl > 0, f"smoke: expected profit, got {net_pnl}"
    assert abs(spent - equity) < 1e-9, "smoke: spent != equity"

    # Decision engine: FLAT + buy zone rising → BUY
    r = cde.evaluate(cde.POSITION_FLAT, cpagg=0.15, prev_cpagg=0.10)
    assert r.action == cde.ACTION_BUY, f"smoke: expected BUY, got {r.action}"

    # Decision engine: IN_POSITION + sell zone falling → SELL
    r2 = cde.evaluate(cde.POSITION_IN_POSITION, cpagg=0.85, prev_cpagg=0.90)
    assert r2.action == cde.ACTION_SELL, f"smoke: expected SELL, got {r2.action}"

    print("  [smoke] OK — accounting + decision engine verified on synthetic data.")
    return True


# ---------------------------------------------------------------------------
# Core backtest
# ---------------------------------------------------------------------------

def run(
    tf_config: List[dict],
    replay_days: int,
    start_equity: float,
    fee_rate: float,
    slippage: float,
    verbose: bool,
) -> dict:

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

    print(f"  Working: {len(working)} candles  Replay: {len(replay)} candles  ({replay_days}d)")

    # Build channel caches
    ch_caches: Dict[str, Dict[int, dict]] = {}
    bar_ms:    Dict[str, int]             = {}
    for tfc in tf_config:
        tf    = tfc["name"]
        bars  = _resample(working, tfc["minutes"])
        ch_caches[tf] = _build_channel_cache(
            bars, tfc["lookback"], tfc["pivot_left"], tfc["pivot_right"]
        )
        bar_ms[tf] = tfc["minutes"] * 60 * 1000
        print(f"  {tf:>4s}: {len(bars)} bars cached")

    # State
    ch_state    = cse.ChannelState()
    pos_state   = cde.POSITION_FLAT
    usdc_equity = start_equity
    xrp_held    = 0.0
    entry_price = 0.0
    entry_usdc  = 0.0
    entry_ts    = ""
    prev_cpagg:     Optional[float] = None
    entry_snapshot: dict           = {}

    # Counters
    trades:       List[dict] = []
    total_fees    = 0.0
    action_counts = {
        "BUY": 0, "SELL": 0,
        "STANDARD_BUY": 0, "STANDARD_SELL": 0,
        "PANIC_BUY": 0,    "PANIC_SELL": 0,
        "HOLD": 0,
    }
    TFS = [tfc["name"] for tfc in tf_config]
    boundary_counts: Dict[str, Dict[str, int]] = {
        "entry_above": {tf: 0 for tf in TFS},
        "entry_below": {tf: 0 for tf in TFS},
        "exit_above":  {tf: 0 for tf in TFS},
        "exit_below":  {tf: 0 for tf in TFS},
    }

    # Equity curve for drawdown
    equity_curve: List[float] = []

    last_tick: dict = {}

    print(f"\nRunning backtest ({len(replay)} ticks) ...")
    for i, candle in enumerate(replay):
        ts_ms  = int(candle[0])
        close  = float(candle[4])
        ts_utc = _fmt_ts(ts_ms)

        # Raw CPs
        raw_cp: Dict[str, Optional[float]] = {}
        for tfc in tf_config:
            tf_name  = tfc["name"]
            bms      = bar_ms[tf_name]
            bar_ts   = (ts_ms // bms) * bms
            ch       = ch_caches[tf_name].get(bar_ts)
            raw_cp[tf_name] = _compute_cp(close, ch)

        # Effective CPs via state engine
        cse.update_channel_state(ch_state, ts_utc, raw_cp)
        eff_cp = cse.effective_cp_by_tf(ch_state)

        # Aggregates
        agg    = _cpagg(eff_cp)
        tscore = cte.compute_trend_score(eff_cp.get("4H"), eff_cp.get("12H"))
        trend  = cte.compute_trend_state(eff_cp.get("4H"), eff_cp.get("12H"))

        # Channel bounds (lower / upper / width / cp) per TF at this tick
        bounds = _channel_bounds_by_tf(close, tf_config, ch_caches, bar_ms, ts_ms)

        # Decision
        dec = cde.evaluate(
            position_state=pos_state,
            cpagg=agg,
            prev_cpagg=prev_cpagg,
            trend_state=trend,
        )

        # Execute
        if dec.action == cde.ACTION_BUY and pos_state == cde.POSITION_FLAT:
            xrp_in, spent, fee_usdc = _buy_cost(usdc_equity, close, fee_rate, slippage)
            total_fees   += fee_usdc
            entry_price   = close
            entry_usdc    = usdc_equity
            entry_ts      = ts_utc
            xrp_held      = xrp_in
            usdc_equity   = 0.0
            pos_state     = cde.POSITION_IN_POSITION
            entry_snapshot = _snapshot(ts_utc, close, agg, tscore, trend, dec,
                                       raw_cp, eff_cp, bounds)
            # boundary counters at entry
            for tf_name, b in bounds.items():
                if b is not None:
                    if close > b["upper"]:
                        boundary_counts["entry_above"][tf_name] += 1
                    elif close < b["lower"]:
                        boundary_counts["entry_below"][tf_name] += 1
            action_counts["BUY"]    += 1
            action_counts[dec.rule] += 1
            if verbose:
                print(f"  BUY  @ {close:.6f}  xrp={xrp_in:.6f}  fee={fee_usdc:.6f}  ts={ts_utc}")

        elif dec.action == cde.ACTION_SELL and pos_state == cde.POSITION_IN_POSITION:
            usdc_out, gross, fee_usdc = _sell_proceeds(xrp_held, close, fee_rate, slippage)
            total_fees   += fee_usdc
            net_pnl_usdc  = usdc_out - entry_usdc
            pnl_pct       = net_pnl_usdc / entry_usdc * 100.0
            hold_secs     = (ts_ms - _ts_to_ms(entry_ts)) / 1000.0
            exit_snapshot = _snapshot(ts_utc, close, agg, tscore, trend, dec,
                                      raw_cp, eff_cp, bounds)
            # boundary counters at exit
            for tf_name, b in bounds.items():
                if b is not None:
                    if close > b["upper"]:
                        boundary_counts["exit_above"][tf_name] += 1
                    elif close < b["lower"]:
                        boundary_counts["exit_below"][tf_name] += 1
            trades.append({
                "trade_num":    len(trades) + 1,
                # core accounting
                "entry_ts":     entry_ts,
                "exit_ts":      ts_utc,
                "entry_price":  entry_price,
                "exit_price":   close,
                "entry_usdc":   entry_usdc,
                "exit_usdc":    usdc_out,
                "net_pnl_usdc": net_pnl_usdc,
                "pnl_pct":      pnl_pct,
                "hold_secs":    hold_secs,
                "fee_usdc":     fee_usdc,
                # split rule fields
                "entry_rule":   entry_snapshot["decision_rule"],
                "exit_rule":    exit_snapshot["decision_rule"],
                # flat audit fields for convenience
                "entry_cpagg":            entry_snapshot["cpagg"],
                "exit_cpagg":             exit_snapshot["cpagg"],
                "entry_trend_score":      entry_snapshot["trend_score"],
                "exit_trend_score":       exit_snapshot["trend_score"],
                "entry_trend_state":      entry_snapshot["trend_state"],
                "exit_trend_state":       exit_snapshot["trend_state"],
                "entry_decision_action":  entry_snapshot["decision_action"],
                "exit_decision_action":   exit_snapshot["decision_action"],
                "entry_decision_rule":    entry_snapshot["decision_rule"],
                "exit_decision_rule":     exit_snapshot["decision_rule"],
                "entry_decision_reason":  entry_snapshot["decision_reason"],
                "exit_decision_reason":   exit_snapshot["decision_reason"],
                # full CP dicts
                "entry_raw_cp_by_tf":       entry_snapshot["raw_cp_by_tf"],
                "exit_raw_cp_by_tf":        exit_snapshot["raw_cp_by_tf"],
                "entry_effective_cp_by_tf": entry_snapshot["effective_cp_by_tf"],
                "exit_effective_cp_by_tf":  exit_snapshot["effective_cp_by_tf"],
                # channel boundary dicts
                "entry_channel_bounds_by_tf": entry_snapshot["channel_bounds_by_tf"],
                "exit_channel_bounds_by_tf":  exit_snapshot["channel_bounds_by_tf"],
            })
            usdc_equity = usdc_out
            xrp_held    = 0.0
            pos_state   = cde.POSITION_FLAT
            action_counts["SELL"]   += 1
            action_counts[dec.rule] += 1
            if verbose:
                print(
                    f"  SELL @ {close:.6f}  usdc={usdc_out:.6f}  "
                    f"pnl={net_pnl_usdc:+.6f} ({pnl_pct:+.2f}%)  ts={ts_utc}"
                )
        else:
            action_counts["HOLD"] += 1

        # Mark-to-market equity for drawdown tracking
        if pos_state == cde.POSITION_IN_POSITION:
            mtm = xrp_held * close
        else:
            mtm = usdc_equity
        equity_curve.append(mtm)

        prev_cpagg = agg

        # Save last tick for report
        last_tick = {
            "ts_utc":      ts_utc,
            "close":       close,
            "raw_cp_1h":   _r(raw_cp.get("1H")),
            "raw_cp_4h":   _r(raw_cp.get("4H")),
            "raw_cp_12h":  _r(raw_cp.get("12H")),
            "raw_cp_1d":   _r(raw_cp.get("1D")),
            "eff_cp_1h":   _r(eff_cp.get("1H")),
            "eff_cp_4h":   _r(eff_cp.get("4H")),
            "eff_cp_12h":  _r(eff_cp.get("12H")),
            "eff_cp_1d":   _r(eff_cp.get("1D")),
            "cpagg":       _r(agg),
            "trend_score": _r(tscore),
            "trend_state": trend,
            "decision":    dec.action,
            "rule":        dec.rule,
            "reason":      dec.reason,
        }

        if (i + 1) % 10000 == 0:
            print(f"  ... {i + 1}/{len(replay)} ticks  equity≈{mtm:.4f}  trades={len(trades)}")

    print(f"  Done. {len(trades)} closed trades.")

    # Mark-to-market if still in position
    open_at_end = pos_state == cde.POSITION_IN_POSITION
    if open_at_end:
        final_close = float(replay[-1][4])
        usdc_proceeds, _, open_fee = _sell_proceeds(xrp_held, final_close, fee_rate, slippage)
        final_equity = usdc_proceeds
        total_fees  += open_fee
    else:
        final_equity = usdc_equity

    # PnL metrics
    net_pnl_usdc = final_equity - start_equity
    net_pnl_pct  = net_pnl_usdc / start_equity * 100.0

    closed  = [t for t in trades]
    wins    = [t for t in closed if t["net_pnl_usdc"] > 0]
    losses  = [t for t in closed if t["net_pnl_usdc"] <= 0]
    win_rate      = len(wins) / len(closed) * 100.0 if closed else 0.0
    gross_profit  = sum(t["net_pnl_usdc"] for t in wins)
    gross_loss    = sum(t["net_pnl_usdc"] for t in losses)
    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
    avg_pnl_pct   = (sum(t["pnl_pct"] for t in closed) / len(closed)) if closed else 0.0
    avg_hold_sec  = (sum(t["hold_secs"] for t in closed) / len(closed)) if closed else 0.0

    largest_win  = max((t["pnl_pct"] for t in wins),   default=0.0)
    largest_loss = min((t["pnl_pct"] for t in losses), default=0.0)

    # Max drawdown from equity curve
    max_dd_pct = _max_drawdown_pct(equity_curve)

    # Sequence validation
    buy_n  = action_counts["BUY"]
    sell_n = action_counts["SELL"]
    seq_ok = (buy_n == sell_n) or (buy_n == sell_n + 1)

    result = {
        "config": {
            "candle_path":  CANDLE_PATH,
            "replay_days":  replay_days,
            "replay_start": _fmt_ts(replay_start),
            "replay_end":   _fmt_ts(last_ts_ms),
            "start_equity": start_equity,
            "fee_rate":     fee_rate,
            "slippage":     slippage,
            "weights":      WEIGHTS,
        },
        "summary": {
            "start_equity":      round(start_equity, 8),
            "final_equity":      round(final_equity, 8),
            "net_pnl_usdc":      round(net_pnl_usdc, 8),
            "net_pnl_pct":       round(net_pnl_pct, 4),
            "trade_count":       len(closed) + (1 if open_at_end else 0),
            "closed_trade_count": len(closed),
            "open_at_end":       open_at_end,
            "win_count":         len(wins),
            "loss_count":        len(losses),
            "win_rate_pct":      round(win_rate, 2),
            "profit_factor":     round(profit_factor, 4) if profit_factor != float("inf") else None,
            "gross_profit_usdc": round(gross_profit, 8),
            "gross_loss_usdc":   round(gross_loss, 8),
            "total_fees_usdc":   round(total_fees, 8),
            "max_drawdown_pct":  round(max_dd_pct, 4),
            "largest_win_pct":   round(largest_win, 4),
            "largest_loss_pct":  round(largest_loss, 4),
            "avg_net_pnl_pct":   round(avg_pnl_pct, 4),
            "avg_hold_secs":     round(avg_hold_sec, 0),
            "avg_hold_human":    _fmt_duration(avg_hold_sec),
            "BUY_COUNT":         buy_n,
            "SELL_COUNT":        sell_n,
            "STANDARD_BUY_COUNT":  action_counts["STANDARD_BUY"],
            "STANDARD_SELL_COUNT": action_counts["STANDARD_SELL"],
            "PANIC_BUY_COUNT":     action_counts["PANIC_BUY"],
            "PANIC_SELL_COUNT":    action_counts["PANIC_SELL"],
            "HOLD_COUNT":          action_counts["HOLD"],
            "sequence_validation": "PASS" if seq_ok else "FAIL",
            # channel boundary counters per TF
            "entry_above_channel": boundary_counts["entry_above"],
            "entry_below_channel": boundary_counts["entry_below"],
            "exit_above_channel":  boundary_counts["exit_above"],
            "exit_below_channel":  boundary_counts["exit_below"],
        },
        "last_tick": last_tick,
        "trades_first_20": [_round_trade(t) for t in closed[:20]],
        "trades_last_20":  [_round_trade(t) for t in closed[-20:]],
    }
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(ts_utc: str) -> int:
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _max_drawdown_pct(curve: List[float]) -> float:
    if not curve:
        return 0.0
    peak  = curve[0]
    max_dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    return f"{h:02d}h {m:02d}m {s:02d}s"


def _snapshot(
    ts_utc: str,
    price: float,
    agg: Optional[float],
    tscore: Optional[float],
    trend: str,
    dec,
    raw_cp: Dict[str, Optional[float]],
    eff_cp: Dict[str, Optional[float]],
    channel_bounds: Dict[str, Optional[dict]],
) -> dict:
    """Capture a complete tick context for entry/exit audit."""
    return {
        "ts":              ts_utc,
        "price":           price,
        "cpagg":           _r(agg),
        "trend_score":     _r(tscore),
        "trend_state":     trend,
        "decision_action": dec.action,
        "decision_rule":   dec.rule,
        "decision_reason": dec.reason,
        "raw_cp_by_tf": {
            tf: _r(raw_cp.get(tf)) for tf in ("1H", "4H", "12H", "1D")
        },
        "effective_cp_by_tf": {
            tf: _r(eff_cp.get(tf)) for tf in ("1H", "4H", "12H", "1D")
        },
        "channel_bounds_by_tf": channel_bounds,
    }


def _round_trade(t: dict) -> dict:
    """Round floats in a trade dict; leave nested dicts intact."""
    out = {}
    for k, v in t.items():
        if isinstance(v, float):
            out[k] = round(v, 6)
        elif isinstance(v, dict):
            out[k] = {ik: (round(iv, 6) if isinstance(iv, float) else iv)
                      for ik, iv in v.items()}
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# TXT report
# ---------------------------------------------------------------------------

def _write_txt(path: str, result: dict) -> None:
    cfg  = result["config"]
    s    = result["summary"]
    lt   = result["last_tick"]
    W    = 72

    def p(v, decimals=4):
        return f"{v:.{decimals}f}" if v is not None else "N/A"

    lines = [
        "=" * W,
        "  Uranus Channel Decision — FÁZIS 3B PnL Backtest",
        "=" * W,
        "",
        f"  Candle file   : {cfg['candle_path']}",
        f"  Replay window : {cfg['replay_start']}  to  {cfg['replay_end']}",
        f"  Replay days   : {cfg['replay_days']}",
        f"  Fee per side  : {cfg['fee_rate']*100:.2f}%",
        f"  Slippage      : {cfg['slippage']*100:.3f}%",
        "",
        "-" * W,
        "  Equity",
        "-" * W,
        f"  Start equity   : {p(s['start_equity'], 8)} USDC",
        f"  Final equity   : {p(s['final_equity'], 8)} USDC",
        f"  Net PnL USDC   : {s['net_pnl_usdc']:+.8f}",
        f"  Net PnL %      : {s['net_pnl_pct']:+.4f}%",
        f"  Total fees     : {p(s['total_fees_usdc'], 8)} USDC",
        f"  Max drawdown   : {p(s['max_drawdown_pct'])}%",
        "",
        "-" * W,
        "  Trades",
        "-" * W,
        f"  Trade count    : {s['trade_count']}",
        f"  Closed trades  : {s['closed_trade_count']}",
        f"  Open at end    : {'YES' if s['open_at_end'] else 'NO'}",
        f"  Win count      : {s['win_count']}",
        f"  Loss count     : {s['loss_count']}",
        f"  Win rate       : {p(s['win_rate_pct'], 2)}%",
        f"  Profit factor  : {p(s['profit_factor'])}",
        f"  Gross profit   : {p(s['gross_profit_usdc'], 8)} USDC",
        f"  Gross loss     : {p(s['gross_loss_usdc'], 8)} USDC",
        f"  Largest win    : {p(s['largest_win_pct'])}%",
        f"  Largest loss   : {p(s['largest_loss_pct'])}%",
        f"  Avg net PnL %  : {p(s['avg_net_pnl_pct'])}%",
        f"  Avg hold time  : {s['avg_hold_human']}",
        "",
        "-" * W,
        "  Signal counts",
        "-" * W,
        f"  BUY            : {s['BUY_COUNT']}",
        f"  SELL           : {s['SELL_COUNT']}",
        f"  STANDARD_BUY   : {s['STANDARD_BUY_COUNT']}",
        f"  STANDARD_SELL  : {s['STANDARD_SELL_COUNT']}",
        f"  PANIC_BUY      : {s['PANIC_BUY_COUNT']}",
        f"  PANIC_SELL     : {s['PANIC_SELL_COUNT']}",
        f"  HOLD           : {s['HOLD_COUNT']}",
        f"  Seq validation : {s['sequence_validation']}",
        "",
        "-" * W,
        "  Last tick",
        "-" * W,
        f"  ts_utc         : {lt.get('ts_utc', 'N/A')}",
        f"  close          : {lt.get('close', 'N/A')}",
        f"  raw  CP 1H/4H/12H/1D : "
        f"{lt.get('raw_cp_1h')} / {lt.get('raw_cp_4h')} / "
        f"{lt.get('raw_cp_12h')} / {lt.get('raw_cp_1d')}",
        f"  eff  CP 1H/4H/12H/1D : "
        f"{lt.get('eff_cp_1h')} / {lt.get('eff_cp_4h')} / "
        f"{lt.get('eff_cp_12h')} / {lt.get('eff_cp_1d')}",
        f"  CPagg          : {lt.get('cpagg')}",
        f"  TrendScore     : {lt.get('trend_score')}",
        f"  TrendState     : {lt.get('trend_state')}",
        f"  Decision       : {lt.get('decision')}  ({lt.get('rule')})",
        f"  Reason         : {lt.get('reason')}",
        "",
    ]

    def _bounds_str(bounds_by_tf: Optional[dict], tf: str) -> str:
        if not bounds_by_tf:
            return "N/A"
        b = bounds_by_tf.get(tf)
        if not b:
            return "N/A"
        return f"{b['lower']:.6f}–{b['upper']:.6f} (cp={b['cp']})"

    def _trade_block(t: dict) -> List[str]:
        hold  = _fmt_duration(t.get("hold_secs", 0))
        en_b  = t.get("entry_channel_bounds_by_tf") or {}
        ex_b  = t.get("exit_channel_bounds_by_tf")  or {}
        return [
            f"  #{t['trade_num']:>3d}  entry={t['entry_ts']}  exit={t['exit_ts']}",
            f"       price  : {t['entry_price']:.6f} → {t['exit_price']:.6f}",
            f"       pnl    : {t['net_pnl_usdc']:+.6f} USDC  ({t['pnl_pct']:+.2f}%)  hold={hold}",
            f"       CPagg  : {t.get('entry_cpagg')} → {t.get('exit_cpagg')}",
            f"       trend  : {t.get('entry_trend_state')} (score={t.get('entry_trend_score')})"
            f" → {t.get('exit_trend_state')} (score={t.get('exit_trend_score')})",
            f"       rule   : {t.get('entry_rule')} → {t.get('exit_rule')}",
            f"       reason : {t.get('entry_decision_reason')}",
            f"             → {t.get('exit_decision_reason')}",
            f"       bounds entry:",
            f"         1H   : {_bounds_str(en_b, '1H')}",
            f"         4H   : {_bounds_str(en_b, '4H')}",
            f"         12H  : {_bounds_str(en_b, '12H')}",
            f"         1D   : {_bounds_str(en_b, '1D')}",
            f"       bounds exit:",
            f"         1H   : {_bounds_str(ex_b, '1H')}",
            f"         4H   : {_bounds_str(ex_b, '4H')}",
            f"         12H  : {_bounds_str(ex_b, '12H')}",
            f"         1D   : {_bounds_str(ex_b, '1D')}",
        ]

    # Channel boundary summary table
    ea = s.get("entry_above_channel", {})
    eb = s.get("entry_below_channel", {})
    xa = s.get("exit_above_channel",  {})
    xb = s.get("exit_below_channel",  {})
    tf_names = list(ea.keys()) if ea else []
    if tf_names:
        hdr = "".join(f"{tf:>6s}" for tf in tf_names)
        lines += [
            "-" * W,
            "  Channel boundary counters (trades only)",
            "-" * W,
            f"  {'':22s}{hdr}",
            f"  {'entry_above_channel':22s}" + "".join(f"{ea.get(tf, 0):>6d}" for tf in tf_names),
            f"  {'entry_below_channel':22s}" + "".join(f"{eb.get(tf, 0):>6d}" for tf in tf_names),
            f"  {'exit_above_channel':22s}"  + "".join(f"{xa.get(tf, 0):>6d}" for tf in tf_names),
            f"  {'exit_below_channel':22s}"  + "".join(f"{xb.get(tf, 0):>6d}" for tf in tf_names),
            "",
        ]

    first20 = result.get("trades_first_20", [])
    last20  = result.get("trades_last_20",  [])

    if first20:
        lines += ["-" * W, "  First 20 trades", "-" * W]
        for t in first20:
            lines += _trade_block(t)
            lines.append("")

    if last20:
        lines += ["-" * W, "  Last 20 trades", "-" * W]
        for t in last20:
            lines += _trade_block(t)
            lines.append("")

    lines += ["", "=" * W]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    args = _parse_args(argv)

    print("Running smoke test ...")
    _smoke_test()
    if args.smoke:
        print("Smoke-only mode — done.")
        return

    tf_config = [dict(tfc) for tfc in DEFAULT_TF_CONFIG]

    os.makedirs(REPORTS_DIR, exist_ok=True)
    json_path = os.path.join(REPORTS_DIR, f"channel_decision_backtest_xrp_{args.days}d.json")
    txt_path  = os.path.join(REPORTS_DIR, f"channel_decision_backtest_xrp_{args.days}d.txt")

    result = run(
        tf_config    = tf_config,
        replay_days  = args.days,
        start_equity = args.equity,
        fee_rate     = args.fee,
        slippage     = args.slippage,
        verbose      = args.verbose,
    )

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nJSON report : {json_path}")

    _write_txt(txt_path, result)
    print(f"TXT  report : {txt_path}")

    # Console summary
    s = result["summary"]
    lt = result["last_tick"]
    print("\n" + "=" * 64)
    print("  Channel Decision Backtest — Summary")
    print("=" * 64)
    print(f"  Equity   : {s['start_equity']:.6f} → {s['final_equity']:.6f} USDC")
    print(f"  Net PnL  : {s['net_pnl_usdc']:+.6f} USDC  ({s['net_pnl_pct']:+.4f}%)")
    print(f"  Trades   : {s['closed_trade_count']} closed"
          f"  {'+ 1 open' if s['open_at_end'] else ''}")
    print(f"  Win rate : {s['win_rate_pct']:.1f}%  "
          f"PF={s['profit_factor']}  MaxDD={s['max_drawdown_pct']:.2f}%")
    print(f"  BUY={s['BUY_COUNT']}  SELL={s['SELL_COUNT']}  HOLD={s['HOLD_COUNT']}")
    print(f"  Seq val  : {s['sequence_validation']}")
    print(f"  Last     : CPagg={lt.get('cpagg')}  "
          f"Trend={lt.get('trend_state')}  Dec={lt.get('decision')}")
    print("=" * 64)


if __name__ == "__main__":
    main()
