#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Uranus tick_runner V2
- fetch last candles via Freqtrade REST API
- sync position from Freqtrade
- compute V2 levels (STANDARD / RECOVERY / PANIC + catastrophe zones)
- persist recovery arming state
- call rule_engine.decide(ctx)
- optional executor wiring
"""

from __future__ import annotations

import os
import json
import time
import base64
from datetime import datetime, timezone
from typing import Any, Tuple
from urllib.request import Request, urlopen

# U-0.4: startup execution gate + kanonikus tick-időbélyeg.
import runtime_freshness

# U-2B: a végrehajtási flagek kanonikus alapértelmezései. Az értékek azonosak
# az eddigiekkel (enabled=False, log_only=True, confirm=True); a modul célja,
# hogy a runner és a UI ne tudjon szétcsúszni ezekben.
import execution_policy

# U-3: háromértékű pozíció-authority (FLAT / OPEN / UNKNOWN). A Freqtrade
# kommunikációs hibája soha nem jelenthet FLAT-et.
import position_authority


APP_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_PATH = os.getenv("STATE_PATH", "/opt/bots/uranus/state.json")
FT_URL = os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")
FT_TIMEOUT = float(os.getenv("FT_TIMEOUT", "8"))

TICK_SECONDS = float(os.getenv("TICK_SECONDS", os.getenv("URANUS_TICK_SECONDS", "10")))
PAIR = os.getenv("PAIR", "XRP/USDC")
TIMEFRAME = os.getenv("TIMEFRAME", "1m")
LIMIT = int(os.getenv("LIMIT", "2"))
MA_SHORT_PERIOD = int(os.getenv("MA_SHORT_PERIOD", "5"))
MA_LONG_PERIOD = int(os.getenv("MA_LONG_PERIOD", "20"))

FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "0.0"))

FT_USERNAME = os.getenv("FT_USERNAME", "").strip()
FT_PASSWORD = os.getenv("FT_PASSWORD", "").strip()

EXECUTION_ENABLED = os.getenv("EXECUTION_ENABLED", "0") == "1"
SHADOW_ENABLED = os.getenv("SHADOW_ENABLED", "0") == "1"
EXECUTION_CONFIRM = os.getenv("EXECUTION_CONFIRM", "1") == "1"
EXECUTION_LOG_ONLY = os.getenv("EXECUTION_LOG_ONLY", "1") == "1"

STD_SELL_PCT = float(os.getenv("STD_SELL_PCT", "0.01"))
RECOVERY_SELL_RETRACE_PCT = float(os.getenv("RECOVERY_SELL_RETRACE_PCT", "0.006"))
PANIC_SELL_PCT = float(os.getenv("PANIC_SELL_PCT", "0.01"))
CATASTROPHE_SELL_PCT = float(os.getenv("CATASTROPHE_SELL_PCT", "0.10"))

STD_BUY_PCT = float(os.getenv("STD_BUY_PCT", "0.01"))
RECOVERY_BUY_REBOUND_PCT = float(os.getenv("RECOVERY_BUY_REBOUND_PCT", "0.006"))
PANIC_BUY_PCT = float(os.getenv("PANIC_BUY_PCT", "0.01"))
CATASTROPHE_BUY_PCT = float(os.getenv("CATASTROPHE_BUY_PCT", "0.10"))

RECOVERY_ARM_TIMEOUT_BARS = int(os.getenv("RECOVERY_ARM_TIMEOUT_BARS", "240"))
MIN_REBOUND_CONFIRM_PCT = float(os.getenv("MIN_REBOUND_CONFIRM_PCT", "0.003"))
SPREAD_MAX_PCT = float(os.getenv("SPREAD_MAX_PCT", "0.002"))
STALENESS_MAX_MS = int(os.getenv("STALENESS_MAX_MS", "500"))
PROFIT_BUFFER_PCT = float(os.getenv("PROFIT_BUFFER_PCT", "0.001"))
COST_GUARD_ENABLED = os.getenv("COST_GUARD_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")


def _runtime_guardrail_flags() -> tuple[bool, int, float]:
    kill_switch = str(os.getenv("KILL_SWITCH", "0")).strip().lower() in ("1", "true", "yes", "on")
    max_trades = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
    daily_loss_cap_pct = float(os.getenv("DAILY_LOSS_CAP_PCT", "2.0"))
    return kill_switch, max_trades, daily_loss_cap_pct


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    ts = now_utc_iso()
    print(f"{ts} [tick_runner] {msg}", flush=True)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)
def _env_float(name: str, default: float = 0.0) -> float:
    try:
        val = os.getenv(name)
        if val is None or str(val).strip() == "":
            return float(default)
        return float(val)
    except Exception:
        return float(default)

def _as_float(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _runtime_exec_flags() -> tuple[bool, bool, bool]:
    # U-2B: egyetlen forrás a kanonikus alapértelmezésekre. A viselkedés
    # változatlan – ugyanaz a három érték, csak már nem két helyen definiálva.
    return execution_policy.runtime_execution_flags()


def _runtime_shadow_enabled() -> bool:
    return _env_bool("SHADOW_ENABLED", False)


def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def _auth_headers() -> dict:
    if FT_USERNAME and FT_PASSWORD:
        token = base64.b64encode(f"{FT_USERNAME}:{FT_PASSWORD}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}


def _http_get_json(url: str) -> Any:
    headers = _auth_headers()
    req = Request(url, headers=headers)
    with urlopen(req, timeout=FT_TIMEOUT) as r:
        data = r.read().decode("utf-8", "replace")
    return json.loads(data)


def fetch_candles(pair: str, timeframe: str, *, limit: int = 2) -> list:
    pair_enc = pair.replace("/", "%2F")
    candidates = [
        f"{FT_URL}/api/v1/pair_candles?pair={pair_enc}&timeframe={timeframe}&limit={int(limit)}",
        f"{FT_URL}/api/v1/pair_candles?pair={pair}&timeframe={timeframe}&limit={int(limit)}",
        f"{FT_URL}/api/v1/ohlcv?pair={pair_enc}&timeframe={timeframe}&limit={int(limit)}",
        f"{FT_URL}/api/v1/ohlcv?pair={pair}&timeframe={timeframe}&limit={int(limit)}",
    ]

    last_err = None
    for u in candidates:
        try:
            j = _http_get_json(u)
            if isinstance(j, dict):
                d = j.get("data")
                if isinstance(d, dict) and isinstance(d.get("candles"), list):
                    return d["candles"]
                if isinstance(j.get("candles"), list):
                    return j["candles"]
                if isinstance(d, list):
                    return d
            last_err = f"BAD_JSON_SHAPE url={u}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

    raise RuntimeError(f"FT_CANDLES_FETCH_FAIL: {last_err}")


def _candle_close(c) -> float:
    if isinstance(c, dict):
        if "close" in c:
            return float(c["close"])
        if "c" in c:
            return float(c["c"])
    if isinstance(c, (list, tuple)) and len(c) >= 5:
        return float(c[4])
    raise ValueError("BAD_CANDLE_FORMAT")


def _candle_ohlcv_ts(c) -> tuple[float | None, float | None, float | None, float | None, float | None, Any]:
    if isinstance(c, dict):
        o = c.get("open", c.get("o"))
        h = c.get("high", c.get("h"))
        l = c.get("low", c.get("l"))
        cc = c.get("close", c.get("c"))
        v = c.get("volume", c.get("v"))
        ts = c.get("date", c.get("ts", c.get("time", c.get("timestamp"))))

        def f(x):
            try:
                return float(x) if x is not None else None
            except Exception:
                return None

        return f(o), f(h), f(l), f(cc), f(v), ts

    if isinstance(c, (list, tuple)):
        ts = c[0] if len(c) >= 1 else None

        def fidx(i):
            try:
                return float(c[i])
            except Exception:
                return None

        o = fidx(1) if len(c) >= 2 else None
        h = fidx(2) if len(c) >= 3 else None
        l = fidx(3) if len(c) >= 4 else None
        cc = fidx(4) if len(c) >= 5 else None
        v = fidx(5) if len(c) >= 6 else None
        return o, h, l, cc, v, ts

    return None, None, None, None, None, None


def update_state_from_candles(state: dict, pair: str, timeframe: str, candles: list) -> tuple[dict, float | None, float | None]:
    prev_close = None
    last_close = None

    if isinstance(candles, list) and len(candles) >= 1:
        last_close = _candle_close(candles[-1])
    if isinstance(candles, list) and len(candles) >= 2:
        prev_close = _candle_close(candles[-2])

    market = state.get("market")
    if not isinstance(market, dict):
        market = {}
        state["market"] = market

    if prev_close is not None:
        market["prev_last"] = prev_close
        state["prev_last"] = prev_close
    if last_close is not None:
        market["last"] = last_close
        state["last"] = last_close

    if isinstance(candles, list) and len(candles) >= 1:
        o, h, l, cc, v, ts_raw = _candle_ohlcv_ts(candles[-1])

        if ts_raw is not None:
            market["last_candle_time"] = ts_raw
        if o is not None:
            market["open"] = o
        if h is not None:
            market["high"] = h
        if l is not None:
            market["low"] = l
        if cc is not None:
            market["close"] = cc
            state["close"] = cc
        if v is not None:
            market["volume"] = v
            state["volume"] = v

        try:
            if isinstance(ts_raw, (int, float)):
                market["ts"] = int(ts_raw)
            elif isinstance(ts_raw, str) and ts_raw.endswith("Z") and "T" in ts_raw:
                dt = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                market["ts"] = int(dt.timestamp())
        except Exception:
            pass

    ma_short = _sma_from_candles(candles, MA_SHORT_PERIOD)
    ma_long = _sma_from_candles(candles, MA_LONG_PERIOD)

    if ma_short is not None:
        market["ma_short"] = ma_short
        state["ma_short"] = ma_short
    else:
        market.pop("ma_short", None)
        state.pop("ma_short", None)

    if ma_long is not None:
        market["ma_long"] = ma_long
        state["ma_long"] = ma_long
    else:
        market.pop("ma_long", None)
        state.pop("ma_long", None)

    flags = state.get("flags")
    if not isinstance(flags, dict):
        flags = {}
        state["flags"] = flags

    if ma_short is not None and ma_long is not None:
        if ma_short > ma_long:
            state["ma_positive"] = True
            state["ma_negative"] = False
            flags["ma_positive"] = True
            flags["ma_negative"] = False
        elif ma_short < ma_long:
            state["ma_positive"] = False
            state["ma_negative"] = True
            flags["ma_positive"] = False
            flags["ma_negative"] = True
        else:
            state["ma_positive"] = False
            state["ma_negative"] = False
            flags["ma_positive"] = False
            flags["ma_negative"] = False
    else:
        state["ma_positive"] = False
        state["ma_negative"] = False
        flags["ma_positive"] = False
        flags["ma_negative"] = False

    market["pair"] = pair
    market["timeframe"] = timeframe

    # U-0.4 (8.2): a market blokk a PIACI adat frissülésének idejét hordozza.
    # A tick-szintű kanonikus időbélyegzés a sikeres tick végén történik
    # (runtime_freshness.stamp_tick_timestamps), hogy egyetlen `now` értékből
    # származzon minden mező, és hibaágon ne hazudjon frissességet.
    market["updated_at"] = now_utc_iso()

    # U-0.4 (7.1): a friss piaci adat bizonyítéka a JELENLEGI processzben.
    # Enélkül a startup execution gate zárva marad.
    if last_close is not None:
        runtime_freshness.mark_market_fetch_ok(
            market.get("last_candle_time", market.get("ts")), timeframe
        )
    else:
        runtime_freshness.mark_market_fetch_failed()

    return state, prev_close, last_close


def _sma_from_candles(candles: list, period: int) -> float | None:
    try:
        period = int(period)
    except Exception:
        return None

    if period <= 0 or not isinstance(candles, list) or len(candles) < period:
        return None

    closes = []
    for c in candles[-period:]:
        try:
            closes.append(_candle_close(c))
        except Exception:
            return None

    if len(closes) != period:
        return None

    return sum(closes) / float(period)


def _as_list_trades(j: dict) -> list:
    if not isinstance(j, dict):
        return []
    if isinstance(j.get("trades"), list):
        return j["trades"]
    d = j.get("data")
    if isinstance(d, dict) and isinstance(d.get("trades"), list):
        return d["trades"]
    if isinstance(d, list):
        return d
    return []


def fetch_open_trade(pair: str) -> dict | None:
    pair_enc = pair.replace("/", "%2F")
    candidates = [
        f"{FT_URL}/api/v1/status",
        f"{FT_URL}/api/v1/trades?is_open=true",
        f"{FT_URL}/api/v1/trades?is_open=true&limit=50",
        f"{FT_URL}/api/v1/trades?pair={pair_enc}&is_open=true",
        f"{FT_URL}/api/v1/open_trades",
        f"{FT_URL}/api/v1/open_trades?pair={pair_enc}",
    ]

    for u in candidates:
        try:
            j = _http_get_json(u)

            if u.endswith("/api/v1/status") and isinstance(j, list):
                if len(j) == 0:
                    return None
                trades = j
            else:
                trades = _as_list_trades(j) if isinstance(j, dict) else []

            if not isinstance(trades, list):
                continue

            for t in trades:
                if not isinstance(t, dict):
                    continue

                if not u.endswith("/api/v1/status"):
                    is_open = t.get("is_open")
                    if is_open is None:
                        is_open = t.get("open", t.get("isOpen"))
                    if bool(is_open) is not True:
                        continue

                tp = t.get("pair")
                if tp is None or str(tp) == str(pair):
                    return t
        except Exception:
            continue

    return None


def _set_buy_cooldown(state: dict, reason: str, trade_id=None) -> None:
    ttl = _env_int("SELL_REBUY_COOLDOWN_SEC", 180)
    if ttl <= 0:
        return

    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, dict):
        cooldowns = {}
        state["cooldowns"] = cooldowns

    now_ts = int(time.time())
    cooldowns["buy_until"] = now_ts + ttl
    cooldowns["buy_set_ts"] = now_ts
    cooldowns["buy_reason"] = reason
    if trade_id is not None:
        cooldowns["sell_trade_id"] = trade_id


def sync_position_from_freqtrade(state: dict, pair: str) -> None:
    try:
        prev_in_position = bool(state.get("in_position"))
        prev_trade_id = state.get("active_trade_id")

        trade = fetch_open_trade(pair)
        now_in_position = bool(trade)

        state["in_position"] = now_in_position
        state["ui_in_position"] = now_in_position

        locks = state.get("locks")
        if not isinstance(locks, dict):
            locks = {}
            state["locks"] = locks

        flags = state.get("flags")
        if not isinstance(flags, dict):
            flags = {}
            state["flags"] = flags

        def _norm_buy_rule_name(x):
            s = str(x or "").strip().upper()
            mapping = {
                "BUY_PANIC": "BUY_PANIC",
                "PANIC_BUY": "BUY_PANIC",
                "BUY_RECOVERY": "BUY_RECOVERY",
                "RECOVERY_BUY": "BUY_RECOVERY",
                "BUY_STANDARD": "BUY_STANDARD",
                "STANDARD_BUY": "BUY_STANDARD",
                "STD_BUY": "BUY_STANDARD",
                "BUY_STD": "BUY_STANDARD",
            }
            return mapping.get(s, "")

        def _extract_latest_buy_rule():
            candidates = [
                state.get("decision"),
                state.get("last_decision"),
                state.get("last_result"),
                state.get("_last_buy_decision"),
            ]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                action = str(c.get("action") or c.get("decision_action") or "").strip().upper()
                rule = _norm_buy_rule_name(c.get("rule") or c.get("decision_rule_id"))
                if action == "BUY" and rule:
                    return rule
            return ""

        if now_in_position and isinstance(trade, dict):
            trade_id = trade.get("trade_id")
            state["active_trade_id"] = trade_id

            # Store live trade stake for shadow engine (so it mirrors real position size)
            live_stake = _as_float(trade.get("stake_amount"))
            if live_stake is None or live_stake <= 0:
                open_r = _as_float(trade.get("open_rate") or trade.get("open_rate_requested"))
                amt = _as_float(trade.get("amount"))
                if open_r is not None and amt is not None and open_r > 0 and amt > 0:
                    live_stake = open_r * amt
            state["live_trade_stake"] = live_stake if (live_stake is not None and live_stake > 0) else None

            open_rate = _as_float(trade.get("open_rate"))
            if open_rate is None:
                open_rate = _as_float(trade.get("open_rate_requested"))

            if open_rate is not None:
                old_base = _as_float(state.get("base"))
                if old_base is not None and abs(old_base - open_rate) > 1e-12:
                    state["previous_base"] = old_base
                elif state.get("previous_base") is None and old_base is not None:
                    state["previous_base"] = old_base

                state["base"] = open_rate
                state["base_price"] = open_rate
                state["entry_price"] = open_rate
                state["buy_price"] = open_rate

            max_rate = _as_float(trade.get("max_rate"))
            cur_peak = _as_float(state.get("peak"))
            cand_peak = max_rate if max_rate is not None and max_rate > 0 else open_rate
            if cand_peak is not None:
                if cur_peak is None:
                    cur_peak = cand_peak
                else:
                    cur_peak = max(cur_peak, cand_peak)
                state["peak"] = cur_peak
                state["high"] = cur_peak

            state["trough"] = None
            state["low"] = None
            state["_flat_anchor"] = None

            locks.pop("buy_pending", None)

            if not prev_in_position:
                entry_rule = _extract_latest_buy_rule()
                if entry_rule:
                    flags["position_entry_rule"] = entry_rule
                    state["position_entry_rule"] = entry_rule

        else:
            state["active_trade_id"] = None
            state["live_trade_stake"] = None

            if prev_in_position:
                _set_buy_cooldown(
                    state,
                    reason="POST_SELL_COOLDOWN: position transitioned open -> flat",
                    trade_id=prev_trade_id,
                )

                locks.pop("buy_pending", None)
                state["peak"] = None
                state["high"] = None

                flags.pop("position_entry_rule", None)
                state["position_entry_rule"] = None

                market = state.get("market") if isinstance(state.get("market"), dict) else {}
                last = _as_float(market.get("last"))
                if last is None:
                    last = _as_float(state.get("last"))

                old_base = _as_float(state.get("base"))
                if old_base is not None:
                    state["previous_base"] = old_base

                if last is not None:
                    state["trough"] = last
                    state["low"] = last
                    state["base"] = last
                    state["_flat_anchor"] = last

    except Exception:
        return


def reconcile_startup_anchor(
    state: dict, last: float | None, authority_state: str | None = None
) -> None:
    """
    U-0.4 (7.3): egyszeri, processzenkénti állapot-újrahorgonyzás indulás után.

    A perzisztált ``base`` egy *kereskedési ciklushoz* tartozó horgony. Újra-
    indítás után, ha a Freqtrade szerint nincs nyitott pozíció, ez a horgony
    hónapokkal korábbi lehet, és vakon nem használható új BUY döntéshez – ez
    okozta az azonnali ``BUY_CATASTROPHE``-t (``base * 1.10`` küszöb).

    Két eset, szándékosan eltérően kezelve:

    * **Nyitott LIVE pozíció** – a horgonyt és a cycle/recovery kontextust
      MEGŐRIZZÜK. A ``base`` amúgy is a Freqtrade ``open_rate``-ből frissül
      minden tickben (``sync_position_from_freqtrade``), és a recovery/panic
      folyamat a nyitott ügylethez tartozik.
    * **LIVE flat** – a flat horgonyt az aktuális árra állítjuk, pontosan úgy,
      ahogy a meglévő nyitott→flat átmenet is teszi. A cycle/panic kontextust
      itt sem töröljük; a döntés-oldali védelmet a startup execution gate adja.

    Amíg ez le nem futott, a ``runtime_freshness`` gate tiltja a végrehajtást.
    """
    if runtime_freshness.is_reconciled():
        return

    # U-3: az UNKNOWN pozícióállapot NEM horgonyoz újra és NEM jelöl
    # reconciled-nek. Korábban ez a függvény a (hibás esetben hamis) legacy
    # booleant olvasta, és mindkét ágán `mark_reconciled()`-et hívott – így egy
    # meghiúsult pozíció-lekérdezés a flat-ágra vitte a horgonyt ÉS kinyitotta a
    # U-0.4 execution gate-et egy bizonyítatlan állapot mellett.
    if authority_state == position_authority.STATE_UNKNOWN:
        state["startup_reconcile"] = {
            "ts": int(time.time()),
            "mode": "blocked_authority_unknown",
            "base": _as_float(state.get("base")),
        }
        log("STARTUP_RECONCILE mode=blocked_authority_unknown (no reanchor, gate stays closed)")
        return

    if authority_state in (position_authority.STATE_OPEN, position_authority.STATE_FLAT):
        in_position = authority_state == position_authority.STATE_OPEN
    else:
        in_position = bool(state.get("in_position", False))

    if in_position:
        state["startup_reconcile"] = {
            "ts": int(time.time()),
            "mode": "in_position_preserved",
            "base": _as_float(state.get("base")),
        }
        runtime_freshness.mark_reconciled()
        log("STARTUP_RECONCILE mode=in_position_preserved (anchor from Freqtrade open_rate)")
        return

    if last is None:
        # Nincs használható friss ár -> nem horgonyzunk, a gate zárva marad.
        return

    old_base = _as_float(state.get("base"))
    if old_base is not None:
        state["previous_base"] = old_base

    state["base"] = last
    state["base_price"] = last
    state["_flat_anchor"] = last
    state["trough"] = last
    state["low"] = last

    state["startup_reconcile"] = {
        "ts": int(time.time()),
        "mode": "flat_reanchored",
        "previous_base": old_base,
        "base": last,
    }
    runtime_freshness.mark_reconciled()
    log(f"STARTUP_RECONCILE mode=flat_reanchored previous_base={old_base} new_base={last}")


def update_peak_trough(state: dict, market: dict) -> None:
    if not isinstance(state, dict):
        return
    if not isinstance(market, dict):
        market = {}

    in_pos = bool(state.get("in_position", False))

    last = _as_float(market.get("last", state.get("last")))
    low = _as_float(market.get("low"))
    high = _as_float(market.get("high"))
    base = _as_float(state.get("base"))

    if in_pos:
        cur_peak = _as_float(state.get("peak"))
        cand = high if high is not None else last
        if cand is None:
            return
        if cur_peak is None:
            cur_peak = cand
        else:
            cur_peak = max(cur_peak, cand)
        state["peak"] = cur_peak
        state["high"] = cur_peak
        state["trough"] = None
        state["low"] = None
        state["_flat_anchor"] = None
        return

    anchor = state.get("_flat_anchor", None)
    anchor_f = _as_float(anchor)

    if base is not None and anchor_f is not None and abs(base - anchor_f) > 1e-12:
        if last is not None:
            state["trough"] = last
            state["low"] = last
        state["_flat_anchor"] = base

    if base is not None and state.get("_flat_anchor") is None:
        state["_flat_anchor"] = base

    cur_trough = _as_float(state.get("trough"))
    cand = low if low is not None else last
    if cand is None:
        return

    if cur_trough is None:
        cur_trough = cand
    else:
        cur_trough = min(cur_trough, cand)

    state["trough"] = cur_trough
    state["low"] = cur_trough
    state["peak"] = None
    state["high"] = None


def ensure_levels(state: dict) -> None:
    from rule_engine import compute_levels as shared_compute_levels

    levels = state.get("levels")
    if not isinstance(levels, dict):
        levels = {}
        state["levels"] = levels

    cycle = state.get("cycle")
    if not isinstance(cycle, dict):
        cycle = {}
        state["cycle"] = cycle

    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    state["flags"] = flags

    last = market.get("last", state.get("last"))
    prev_last = market.get("prev_last", state.get("prev_last"))

    base = None
    for k in ("base", "base_price", "entry_price", "buy_price"):
        v = state.get(k)
        if v is not None:
            try:
                base = float(v)
                break
            except Exception:
                pass

    peak = None
    for k in ("peak", "high"):
        v = state.get(k)
        if v is not None:
            try:
                peak = float(v)
                break
            except Exception:
                pass

    trough = None
    for k in ("trough", "low"):
        v = state.get(k)
        if v is not None:
            try:
                trough = float(v)
                break
            except Exception:
                pass

    try:
        last_f = float(last) if last is not None else None
    except Exception:
        last_f = None

    try:
        prev_last_f = float(prev_last) if prev_last is not None else last_f
    except Exception:
        prev_last_f = last_f

    if base is None:
        base = last_f
        if (not bool(state.get("in_position", False))) and base is not None:
            state["base"] = base
            state["base_price"] = base
            state["_flat_anchor"] = base

    if peak is None and base is not None and bool(state.get("in_position", False)):
        peak = base

    in_position = bool(state.get("in_position", False))
    if in_position:
        trough = None

    ma_short = _as_float(market.get("ma_short", state.get("ma_short")))
    ma_long = _as_float(market.get("ma_long", state.get("ma_long")))

    if ma_short is None or ma_long is None:
        ma_short = 0.0
        ma_long = 0.0

    ctx = {
        "base": base,
        "last": last_f if last_f is not None else base,
        "prev_last": prev_last_f if prev_last_f is not None else (last_f if last_f is not None else base),
        "peak": peak if peak is not None else base,
        "trough": trough if trough is not None else base,
        "in_position": in_position,
        "ma_short": ma_short,
        "ma_long": ma_long,
        "std_sell_pct": STD_SELL_PCT,
        "recovery_sell_retrace_pct": RECOVERY_SELL_RETRACE_PCT,
        "panic_sell_pct": PANIC_SELL_PCT,
        "catastrophe_sell_pct": CATASTROPHE_SELL_PCT,
        "std_buy_pct": STD_BUY_PCT,
        "recovery_buy_rebound_pct": RECOVERY_BUY_REBOUND_PCT,
        "panic_buy_pct": PANIC_BUY_PCT,
        "catastrophe_buy_pct": CATASTROPHE_BUY_PCT,
        "fee_pct_per_side": FEE_PCT / 100.0,
        "fee_total_pct": (FEE_PCT / 100.0) * 2.0,
        "fee_buffer_pct": max(PROFIT_BUFFER_PCT, ((FEE_PCT / 100.0) * 2.0) + (SLIPPAGE_PCT / 100.0)),
        "recovery_context": bool(state.get("recovery_context", False) or cycle.get("recovery_mode") or cycle.get("required_next_buy_mode") or cycle.get("required_next_sell_mode")),
        "panic_context": str(state.get("panic_context") or "NONE"),
        "last_panic_loss": float(state.get("last_panic_loss", 0.0) or 0.0),
        "required_next_buy_mode": "RECOVERY" if cycle.get("required_next_buy_mode") else "",
        "required_next_sell_mode": "RECOVERY" if cycle.get("required_next_sell_mode") else "",
        "symbol": PAIR,
        "timeframe": TIMEFRAME,
    }

    try:
        shared_levels = shared_compute_levels(ctx) or {}
    except Exception:
        shared_levels = {}

    reference_base = base

    levels["reference_base"] = reference_base
    levels["std_sell"] = shared_levels.get("std_sell_level")
    levels["recovery_sell"] = shared_levels.get("recovery_sell_level")
    levels["panic_sell"] = shared_levels.get("panic_sell_level")
    levels["catastrophe_sell"] = shared_levels.get("catastrophe_sell_level")
    levels["std_buy"] = shared_levels.get("std_buy_level")
    levels["recovery_buy"] = shared_levels.get("recovery_buy_level")
    levels["panic_buy"] = shared_levels.get("panic_buy_level")
    levels["catastrophe_buy"] = shared_levels.get("catastrophe_buy_level")
    levels["computed_at"] = now_utc_iso()

    std_buy = levels.get("std_buy")
    recovery_buy = levels.get("recovery_buy")
    panic_buy = levels.get("panic_buy")
    catastrophe_buy = levels.get("catastrophe_buy")

    std_sell = levels.get("std_sell")
    recovery_sell = levels.get("recovery_sell")
    panic_sell = levels.get("panic_sell")
    catastrophe_sell = levels.get("catastrophe_sell")

    flags["buy_hierarchy_ok"] = bool(
        (std_buy is not None) and (recovery_buy is not None) and (panic_buy is not None) and (catastrophe_buy is not None)
        and std_buy < recovery_buy < panic_buy < catastrophe_buy
    )
    flags["sell_hierarchy_ok"] = bool(
        (std_sell is not None) and (recovery_sell is not None) and (panic_sell is not None) and (catastrophe_sell is not None)
        and std_sell > recovery_sell > panic_sell > catastrophe_sell
    )

    for k in (
        "reference_base",
        "std_sell",
        "recovery_sell",
        "panic_sell",
        "catastrophe_sell",
        "std_buy",
        "recovery_buy",
        "panic_buy",
        "catastrophe_buy",
    ):
        state[k] = levels.get(k)


def update_recovery_state(state: dict) -> None:

    cycle = state.get("cycle")
    if not isinstance(cycle, dict):
        cycle = {}
        state["cycle"] = cycle

    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}
    levels = state.get("levels") if isinstance(state.get("levels"), dict) else {}
    market = state.get("market") if isinstance(state.get("market"), dict) else {}

    last = _as_float(market.get("last", state.get("last")))
    if last is None:
        return

    trend_up = bool(flags.get("ma_positive"))
    trend_down = bool(flags.get("ma_negative"))

    recovery_buy_armed = bool(cycle.get("recovery_buy_armed", False))
    recovery_sell_armed = bool(cycle.get("recovery_sell_armed", False))
    recovery_buy_arm_age = int(cycle.get("recovery_buy_arm_age", 0) or 0)
    recovery_sell_arm_age = int(cycle.get("recovery_sell_arm_age", 0) or 0)

    panic_sell = _as_float(levels.get("panic_sell"))
    catastrophe_sell = _as_float(levels.get("catastrophe_sell"))
    panic_buy = _as_float(levels.get("panic_buy"))
    catastrophe_buy = _as_float(levels.get("catastrophe_buy"))

    if trend_up and ((panic_buy is not None and last >= panic_buy) or (catastrophe_buy is not None and last >= catastrophe_buy)):
        recovery_sell_armed = True
        recovery_sell_arm_age = 0

    if trend_down and ((panic_sell is not None and last <= panic_sell) or (catastrophe_sell is not None and last <= catastrophe_sell)):
        recovery_buy_armed = True
        recovery_buy_arm_age = 0

    if recovery_sell_armed:
        recovery_sell_arm_age += 1
        if recovery_sell_arm_age > RECOVERY_ARM_TIMEOUT_BARS:
            recovery_sell_armed = False
            recovery_sell_arm_age = 0

    if recovery_buy_armed:
        recovery_buy_arm_age += 1
        if recovery_buy_arm_age > RECOVERY_ARM_TIMEOUT_BARS:
            recovery_buy_armed = False
            recovery_buy_arm_age = 0

    cycle["recovery_buy_armed"] = recovery_buy_armed
    cycle["recovery_sell_armed"] = recovery_sell_armed
    cycle["recovery_buy_arm_age"] = recovery_buy_arm_age
    cycle["recovery_sell_arm_age"] = recovery_sell_arm_age

    levels["recovery_buy_armed"] = recovery_buy_armed
    levels["recovery_sell_armed"] = recovery_sell_armed


def _ensure_cycle_section(state: dict) -> dict:
    cycle = state.get("cycle")
    if not isinstance(cycle, dict):
        cycle = {}
        state["cycle"] = cycle
    return cycle


def _mirror_cycle_to_state(state: dict) -> None:
    cycle = _ensure_cycle_section(state)

    recovery_mode = bool(cycle.get("recovery_mode", False))
    recovery_loss_pct = float(cycle.get("recovery_loss_pct", 0.0) or 0.0)
    recovery_anchor_price = cycle.get("recovery_anchor_price")
    required_next_buy_mode = cycle.get("required_next_buy_mode")
    required_next_sell_mode = cycle.get("required_next_sell_mode")
    recovery_target_entry_cap = cycle.get("recovery_target_entry_cap")

    # top-level = exact mirror of detailed cycle workflow state
    state["recovery_mode"] = recovery_mode
    state["recovery_loss_pct"] = recovery_loss_pct
    state["recovery_anchor_price"] = recovery_anchor_price
    state["required_next_buy_mode"] = required_next_buy_mode
    state["required_next_sell_mode"] = required_next_sell_mode
    state["recovery_target_entry_cap"] = recovery_target_entry_cap

    state["recovery_context"] = bool(recovery_mode or required_next_buy_mode or required_next_sell_mode)

    if required_next_buy_mode:
        state["panic_context"] = "AFTER_SELL_PANIC"
    elif required_next_sell_mode:
        state["panic_context"] = "AFTER_BUY_PANIC"
    else:
        state["panic_context"] = "NONE"

    try:
        anchor_f = float(recovery_anchor_price) if recovery_anchor_price is not None else None
    except Exception:
        anchor_f = None

    state["last_panic_loss"] = (anchor_f * recovery_loss_pct) if (anchor_f is not None and recovery_loss_pct > 0) else 0.0

def _clear_recovery_cycle(state: dict) -> None:
    cycle = _ensure_cycle_section(state)

    cycle["recovery_mode"] = False
    cycle["recovery_loss_pct"] = 0.0
    cycle["recovery_anchor_price"] = None
    cycle["required_next_buy_mode"] = None
    cycle["required_next_sell_mode"] = None
    cycle["recovery_target_entry_cap"] = None

    _mirror_cycle_to_state(state)


def _set_next_buy_recovery_requirement(state: dict, entry_price: float, exit_price: float) -> None:
    cycle = _ensure_cycle_section(state)

    loss_pct = 0.0
    if entry_price is not None and entry_price > 0 and exit_price is not None:
        loss_pct = max(0.0, (float(entry_price) - float(exit_price)) / float(entry_price))

    cycle["recovery_mode"] = True
    cycle["recovery_loss_pct"] = float(loss_pct)
    cycle["recovery_anchor_price"] = float(entry_price) if entry_price is not None else None
    cycle["required_next_buy_mode"] = "RECOVERY_OR_LOWER_STANDARD"
    cycle["required_next_sell_mode"] = None
    cycle["recovery_target_entry_cap"] = float(exit_price) if exit_price is not None else None

    _mirror_cycle_to_state(state)


def _set_next_sell_recovery_requirement(state: dict, entry_price: float | None) -> None:
    cycle = _ensure_cycle_section(state)

    cycle["recovery_mode"] = True
    cycle["required_next_buy_mode"] = None
    cycle["required_next_sell_mode"] = "RECOVERY_OR_HIGHER_STANDARD"
    cycle["recovery_target_entry_cap"] = None

    if entry_price is not None:
        cycle["recovery_anchor_price"] = float(entry_price)

    if cycle.get("recovery_loss_pct") is None:
        cycle["recovery_loss_pct"] = 0.0

    _mirror_cycle_to_state(state)


def _mark_recovery_buy_consumed(state: dict) -> None:
    cycle = _ensure_cycle_section(state)
    cycle["required_next_buy_mode"] = None
    cycle["recovery_target_entry_cap"] = None
    _mirror_cycle_to_state(state)


def _apply_cycle_rules_after_execution(state: dict, decision: dict) -> None:
    if not isinstance(state, dict):
        return

    action = str(decision.get("action") or "").strip().upper()
    rule = str(decision.get("rule") or decision.get("decision_rule_id") or "").strip().upper()

    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    last_price = _as_float(market.get("last", state.get("last")))
    entry_price = _as_float(state.get("entry_price"))
    cycle = _ensure_cycle_section(state)

    data = decision.get("data") if isinstance(decision.get("data"), dict) else {}
    min_profitable_exit_level = _as_float(data.get("min_profitable_exit_level"))
    min_recovery_exit_level = _as_float(data.get("min_recovery_exit_level"))

    if action == "SELL":
        profitable_exit = False
        recovered_exit = False

        if last_price is not None and min_profitable_exit_level is not None:
            profitable_exit = last_price >= min_profitable_exit_level
        elif entry_price is not None and last_price is not None:
            profitable_exit = last_price >= entry_price

        if last_price is not None and min_recovery_exit_level is not None:
            recovered_exit = last_price >= min_recovery_exit_level

        if rule in ("SELL_PANIC", "SELL_CATASTROPHE") and entry_price is not None and last_price is not None and not profitable_exit:
            _set_next_buy_recovery_requirement(state, entry_price, last_price)
            return

        if bool(cycle.get("recovery_mode", False)):
            if rule in ("SELL_RECOVERY", "SELL_STANDARD") and (recovered_exit or profitable_exit):
                _clear_recovery_cycle(state)
                return

        if entry_price is not None and last_price is not None and not profitable_exit:
            _set_next_buy_recovery_requirement(state, entry_price, last_price)
            return

        if not bool(cycle.get("recovery_mode", False)):
            cycle["required_next_buy_mode"] = None
            cycle["required_next_sell_mode"] = None
            cycle["recovery_target_entry_cap"] = None
            _mirror_cycle_to_state(state)
            return

        _mirror_cycle_to_state(state)
        return

    if action == "BUY":
        buy_entry_price = last_price if last_price is not None else entry_price

        if rule in ("BUY_PANIC", "BUY_CATASTROPHE"):
            _set_next_sell_recovery_requirement(state, buy_entry_price)
            return

        if rule in ("BUY_STANDARD", "BUY_RECOVERY") and cycle.get("required_next_buy_mode") == "RECOVERY_OR_LOWER_STANDARD":
            _mark_recovery_buy_consumed(state)
            return

        _mirror_cycle_to_state(state)
        return


def normalize_decision(*, action: str, rule: str | None, reason: str, level: str = "none", raw: dict | None = None) -> dict:
    a = (action or "HOLD").upper()
    return {
        "level": level or "none",
        "action": a,
        "rule": rule,
        "reason": reason or "NO_REASON",
        "ts": int(time.time()),
        "raw": raw if isinstance(raw, dict) else {"action": a, "rule": rule, "reason": reason, "level": level},
        "decision_action": a,
        "decision_rule_id": rule,
        "decision_reason": reason or "NO_REASON",
        "decision_level": level or "none",
    }


def apply_decision_contract(state: dict, decision: dict) -> None:
    if not isinstance(state, dict):
        return
    if not isinstance(decision, dict):
        decision = normalize_decision(action="HOLD", rule="BAD_DECISION", reason="BAD_DECISION_TYPE", level="none")

    state["decision"] = decision
    state["decision_action"] = decision.get("action", "HOLD")
    state["decision_reason"] = decision.get("reason")
    state["decision_rule"] = decision.get("rule") or decision.get("decision_rule_id")
    state["decision_level"] = decision.get("level", decision.get("decision_level", "none"))

    state["last_price"] = state.get("last")
    state["last_decision"] = decision
    state["last_reason"] = state["decision_reason"]

    state["ui_last_action"] = state["decision_action"]
    state["ui_last_rule"] = state["decision_rule"]
    state["ui_last_reason"] = state["decision_reason"]

    # top-level workflow state must come from cycle mirror only
    # do NOT overwrite panic/recovery state from the raw engine decision here
    if "panic_context" in decision:
        state["engine_panic_context"] = decision.get("panic_context")
    if "last_panic_loss" in decision:
        state["engine_last_panic_loss"] = decision.get("last_panic_loss")
    if "recovery_context" in decision:
        state["engine_recovery_context"] = decision.get("recovery_context")

    # keep detailed workflow modes sourced from cycle only
    if "required_next_buy_mode" in decision:
        state["engine_required_next_buy_mode"] = decision.get("required_next_buy_mode")
    if "required_next_sell_mode" in decision:
        state["engine_required_next_sell_mode"] = decision.get("required_next_sell_mode")

    thresholds = decision.get("thresholds")
    if isinstance(thresholds, dict):
        data = decision.get("data") if isinstance(decision.get("data"), dict) else {}
        state["engine"] = {
            "engine_version": decision.get("engine_version"),
            "trend_state": decision.get("trend_state"),
            "thresholds": thresholds,
            "debug": data.get("debug"),
            "candidates": data.get("candidates"),
        }

def apply_buy_cooldown_guard(state: dict, decision: dict) -> dict:
    if not isinstance(state, dict):
        return decision

    act = (decision.get("action") or "HOLD").upper()
    if act != "BUY":
        return decision

    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, dict):
        return decision

    now_ts = int(time.time())
    buy_until = cooldowns.get("buy_until")
    try:
        buy_until = int(buy_until) if buy_until is not None else 0
    except Exception:
        buy_until = 0

    if buy_until <= 0:
        return decision

    if now_ts >= buy_until:
        cooldowns.pop("buy_until", None)
        cooldowns.pop("buy_set_ts", None)
        cooldowns.pop("buy_reason", None)
        cooldowns.pop("sell_trade_id", None)
        return decision

    return normalize_decision(
        action="HOLD",
        rule="BUY_COOLDOWN",
        reason=f"BUY_COOLDOWN_ACTIVE: BUY blocked until {buy_until} (unix_ts) after SELL/exit transition",
        level="none",
        raw={"original": decision, "cooldowns": cooldowns},
    )


def apply_buy_lock(state: dict, decision: dict) -> dict:
    if not isinstance(state, dict):
        return decision

    locks = state.get("locks")
    if not isinstance(locks, dict):
        locks = {}
        state["locks"] = locks

    buy_lock = locks.get("buy_pending")
    in_pos = bool(state.get("in_position", False))

    ttl = _env_int("BUY_LOCK_TTL_SEC", 180)
    now = int(time.time())

    if isinstance(buy_lock, dict):
        ts = buy_lock.get("ts")
        if isinstance(ts, int) and ttl > 0 and (now - ts) >= ttl:
            locks.pop("buy_pending", None)
            buy_lock = None

    if in_pos:
        if buy_lock is not None:
            locks.pop("buy_pending", None)
        return decision

    act = (decision.get("action") or "HOLD").upper()

    if act != "BUY":
        if buy_lock is not None:
            locks.pop("buy_pending", None)
        return decision

    enabled_now, log_only_now, _confirm_now = _runtime_exec_flags()
    if (not enabled_now) or log_only_now:
        if buy_lock is not None:
            locks.pop("buy_pending", None)
        return decision

    if buy_lock is None:
        market = state.get("market") if isinstance(state.get("market"), dict) else {}
        locks["buy_pending"] = {
            "ts": now,
            "pair": market.get("pair", state.get("pair")),
            "timeframe": market.get("timeframe", state.get("timeframe")),
            "last": market.get("last", state.get("last")),
            "prev_last": market.get("prev_last", state.get("prev_last")),
            "rule": decision.get("rule") or decision.get("decision_rule_id"),
            "reason": decision.get("reason"),
        }
        return decision

    return normalize_decision(
        action="HOLD",
        rule="BUY_LOCK",
        reason="BUY_LOCK_ACTIVE: BUY already signaled while flat; waiting for in_position, condition to clear, or TTL expiry",
        level="none",
        raw={"original": decision, "lock": buy_lock, "ttl_sec": ttl},
    )


def decision_from_rule_engine(state: dict) -> dict:
    import rule_engine

    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    cycle = state.get("cycle") if isinstance(state.get("cycle"), dict) else {}
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}

    base = _as_float(state.get("base"))
    last = _as_float(market.get("last", state.get("last")))
    prev_last = _as_float(market.get("prev_last", state.get("prev_last")))
    peak = _as_float(state.get("peak"))
    trough = _as_float(state.get("trough"))

    in_position = bool(state.get("in_position", False))

    if last is None:
        last = base
    if prev_last is None:
        prev_last = last

    if in_position and peak is None:
        vals = [v for v in (base, last) if v is not None]
        peak = max(vals) if vals else None

    if (not in_position) and trough is None:
        vals = [v for v in (base, last) if v is not None]
        trough = min(vals) if vals else None

    fee_pct_per_side = FEE_PCT / 100.0
    slippage_pct = SLIPPAGE_PCT / 100.0
    fee_total_pct = fee_pct_per_side * 2.0
    fee_buffer_pct = max(PROFIT_BUFFER_PCT, fee_total_pct + slippage_pct)

    required_next_buy_mode = cycle.get("required_next_buy_mode") or ""
    required_next_sell_mode = cycle.get("required_next_sell_mode") or ""
    recovery_mode = bool(cycle.get("recovery_mode", False))

    recovery_loss_pct = _as_float(cycle.get("recovery_loss_pct", state.get("recovery_loss_pct"))) or 0.0
    recovery_anchor_price = _as_float(cycle.get("recovery_anchor_price", state.get("recovery_anchor_price")))
    recovery_target_entry_cap = _as_float(cycle.get("recovery_target_entry_cap", state.get("recovery_target_entry_cap")))
    last_panic_sell_price = _as_float(cycle.get("last_panic_sell_price"))
    last_panic_buy_price = _as_float(cycle.get("last_panic_buy_price"))

    panic_context = "NONE"
    if required_next_buy_mode:
        panic_context = "AFTER_SELL_PANIC"
    elif required_next_sell_mode:
        panic_context = "AFTER_BUY_PANIC"

    last_panic_loss = 0.0
    if recovery_anchor_price is not None and recovery_loss_pct > 0:
        last_panic_loss = recovery_anchor_price * recovery_loss_pct

    ma_short = _as_float(market.get("ma_short", state.get("ma_short")))
    ma_long = _as_float(market.get("ma_long", state.get("ma_long")))

    ma_values_ready = (ma_short is not None and ma_long is not None)
    if not ma_values_ready:
        ma_short = None
        ma_long = None

    ctx = {
        "base": base,
        "last": last,
        "prev_last": prev_last,
        "peak": peak,
        "trough": trough,
        "in_position": in_position,
        "ma_short": ma_short,
        "ma_long": ma_long,
        "fee_pct_per_side": fee_pct_per_side,
        "fee_total_pct": fee_total_pct,
        "fee_buffer_pct": fee_buffer_pct,
        "std_sell_pct": STD_SELL_PCT,
        "recovery_sell_retrace_pct": RECOVERY_SELL_RETRACE_PCT,
        "panic_sell_pct": PANIC_SELL_PCT,
        "catastrophe_sell_pct": CATASTROPHE_SELL_PCT,
        "std_buy_pct": STD_BUY_PCT,
        "recovery_buy_rebound_pct": RECOVERY_BUY_REBOUND_PCT,
        "panic_buy_pct": PANIC_BUY_PCT,
        "catastrophe_buy_pct": CATASTROPHE_BUY_PCT,
        "sell_reversal_min_pct": _env_float("SELL_REVERSAL_MIN_PCT", 0.0),
        "ma_sideways_band_pct": _env_float("MA_SIDEWAYS_BAND_PCT", 0.0005),
        "recovery_context": recovery_mode or bool(required_next_buy_mode) or bool(required_next_sell_mode),
        "panic_context": panic_context,
        "last_panic_loss": last_panic_loss,
        "last_panic_sell_price": last_panic_sell_price,
        "last_panic_buy_price": last_panic_buy_price,
        "recovery_anchor_price": recovery_anchor_price,
        "required_next_buy_mode": "RECOVERY" if required_next_buy_mode else "",
        "required_next_sell_mode": "RECOVERY" if required_next_sell_mode else "",
        "symbol": PAIR,
        "timeframe": TIMEFRAME,
        "expected_recovery_sell_override": None,
    }

    cycle_ctx = {
        "recovery_mode": recovery_mode,
        "recovery_loss_pct": recovery_loss_pct,
        "recovery_anchor_price": recovery_anchor_price,
        "required_next_buy_mode": required_next_buy_mode,
        "required_next_sell_mode": required_next_sell_mode,
        "recovery_target_entry_cap": recovery_target_entry_cap,
        "panic_context": panic_context,
        "last_panic_loss": last_panic_loss,
    }

    raw = rule_engine.decide(ctx, cycle_ctx)
    if not isinstance(raw, dict):
        return normalize_decision(
            action="HOLD",
            rule="BAD_ENGINE_RESULT",
            reason="rule_engine returned non-dict result",
            level="none",
            raw={"engine_raw": raw},
        )

    engine_decision = str(
        raw.get("decision")
        or raw.get("action")
        or raw.get("last_result")
        or "HOLD"
    ).strip().upper()

    if engine_decision.startswith("BUY_"):
        action = "BUY"
        rule = engine_decision
    elif engine_decision.startswith("SELL_"):
        action = "SELL"
        rule = engine_decision
    elif engine_decision == "HOLD":
        action = "HOLD"
        rule = "HOLD"
    else:
        action = "HOLD"
        rule = engine_decision or "UNKNOWN_ENGINE_DECISION"

    selected_level_name = raw.get("selected_level_name")
    selected_level = raw.get("selected_level")
    if selected_level_name:
        level = str(selected_level_name)
    elif selected_level is not None:
        level = str(selected_level)
    else:
        level = "none"

    decision = normalize_decision(
        action=action,
        rule=rule,
        reason=str(raw.get("reason") or "NO_REASON"),
        level=level,
        raw=raw,
    )

    decision["panic_context"] = raw.get("panic_context")
    decision["last_panic_loss"] = raw.get("last_panic_loss")
    decision["recovery_context"] = raw.get("recovery_context")
    decision["required_next_buy_mode"] = raw.get("required_next_buy_mode")
    decision["required_next_sell_mode"] = raw.get("required_next_sell_mode")
    decision["thresholds"] = raw.get("thresholds")
    decision["trend_state"] = raw.get("trend_state")
    decision["engine_version"] = raw.get("engine_version")
    decision["data"] = {
        "selected_level_name": raw.get("selected_level_name"),
        "selected_level": raw.get("selected_level"),
        "candidates": raw.get("candidates"),
        "debug": raw.get("debug"),
        "engine_raw": raw,
    }

    return decision

def _ensure_guardrail_section(state: dict) -> dict:
    g = state.get("guardrail")
    if not isinstance(g, dict):
        g = {}
        state["guardrail"] = g
    return g


def _utc_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _guardrail_counters(state: dict) -> dict:
    g = _ensure_guardrail_section(state)
    day = _utc_day_key()
    if g.get("day") != day:
        g["day"] = day
        g["trades_today"] = 0
        g["blocked"] = False
        g["block_reason"] = None
    g.setdefault("trades_today", 0)
    g.setdefault("blocked", False)
    g.setdefault("block_reason", None)
    return g


def _estimate_open_trade_loss_pct(state: dict) -> float:
    try:
        ex = state.get("execution") if isinstance(state.get("execution"), dict) else {}
        last_confirm = ex.get("last_confirm") if isinstance(ex.get("last_confirm"), dict) else {}
        resp = last_confirm.get("response") if isinstance(last_confirm.get("response"), dict) else {}
        raw = resp.get("raw") if isinstance(resp.get("raw"), list) else []
        if raw:
            t0 = raw[0] if isinstance(raw[0], dict) else {}
            ratio = t0.get("total_profit_ratio")
            if ratio is not None:
                ratio_f = float(ratio)
                return abs(ratio_f * 100.0) if ratio_f < 0 else 0.0
    except Exception:
        pass
    return 0.0


def guardrail_allows_execution(state: dict, decision: dict) -> tuple[bool, str]:
    g = _guardrail_counters(state)
    act = (decision.get("action") or "HOLD").upper()
    kill_switch, max_trades_per_day, daily_loss_cap_pct = _runtime_guardrail_flags()

    if act not in ("BUY", "SELL"):
        g["blocked"] = False
        g["block_reason"] = None
        return True, "NO_ACTION"

    if kill_switch:
        g["blocked"] = True
        g["block_reason"] = "KILL_SWITCH"
        return False, "KILL_SWITCH"

    if int(g.get("trades_today", 0)) >= int(max_trades_per_day):
        g["blocked"] = True
        g["block_reason"] = "MAX_TRADES_PER_DAY"
        return False, "MAX_TRADES_PER_DAY"

    loss_pct = _estimate_open_trade_loss_pct(state)
    if loss_pct >= float(daily_loss_cap_pct):
        g["blocked"] = True
        g["block_reason"] = f"DAILY_LOSS_CAP_PCT:{loss_pct:.4f}"
        return False, g["block_reason"]

    g["blocked"] = False
    g["block_reason"] = None
    return True, "OK"

def _ensure_exec_section(state: dict) -> dict:
    execs = state.get("execution")
    if not isinstance(execs, dict):
        execs = {}
        state["execution"] = execs
    return execs


def refresh_exec_flags_every_tick(state: dict) -> None:
    if not isinstance(state, dict):
        return

    execs = _ensure_exec_section(state)

    enabled, log_only, _confirm = _runtime_exec_flags()
    active = bool(enabled and (not log_only))

    if not enabled:
        mode = "disabled"
    elif log_only:
        mode = "log_only"
    else:
        mode = "active"

    execs["enabled"] = enabled
    execs["log_only"] = log_only
    execs["active"] = active
    execs["mode"] = mode
    execs["flags_ts"] = int(time.time())
    execs["ft_url"] = FT_URL
    execs["pair"] = os.getenv("PAIR", PAIR)
    execs["shadow_enabled"] = _runtime_shadow_enabled()

def sell_execution_profit_guard(state: dict, decision: dict) -> tuple[bool, str]:
    """
    Final execution safety gate before Freqtrade force_exit.
    Blocks non-panic SELL if expected exit would be net negative after fees/slippage.
    PANIC/CATASTROPHE exits are allowed because their purpose is loss limitation.
    """
    rule = str(decision.get("rule") or decision.get("decision_rule_id") or "").strip().upper()

    if rule in ("SELL_PANIC", "SELL_CATASTROPHE"):
        return True, f"panic_allowed:{rule}"

    market = state.get("market") if isinstance(state.get("market"), dict) else {}

    last = _as_float(market.get("last", state.get("last")))
    entry = _as_float(state.get("entry_price"))
    if entry is None:
        entry = _as_float(state.get("base"))

    if last is None or entry is None or entry <= 0:
        return False, "SELL_BLOCKED_PROFIT_GUARD: missing last/entry price"

    fee_side = FEE_PCT / 100.0
    slippage = SLIPPAGE_PCT / 100.0
    min_exit = entry * (1.0 + (fee_side * 2.0) + slippage + PROFIT_BUFFER_PCT)

    net_pct = (last / entry) - 1.0 - (fee_side * 2.0) - slippage

    if last < min_exit:
        return False, (
            "SELL_BLOCKED_PROFIT_GUARD: "
            f"rule={rule} last={last:.8f} entry={entry:.8f} "
            f"min_exit={min_exit:.8f} net_pct={net_pct:.8f}"
        )

    return True, (
        "SELL_PROFIT_GUARD_OK: "
        f"rule={rule} last={last:.8f} entry={entry:.8f} "
        f"min_exit={min_exit:.8f} net_pct={net_pct:.8f}"
    )

def maybe_execute_via_api(state: dict, decision: dict) -> Tuple[dict, bool]:
    executed = False
    act = (decision.get("action") or "HOLD").upper()

    execs = _ensure_exec_section(state)

    if act not in ("BUY", "SELL"):
        g = _guardrail_counters(state)
        g["blocked"] = False
        g["block_reason"] = None

        last_result = execs.get("last_result") if isinstance(execs.get("last_result"), dict) else {}
        detail = str(last_result.get("detail") or "")
        if detail.startswith("guardrail_block:"):
            execs["last_result"] = {
                "ts": int(time.time()),
                "ok": True,
                "detail": "guardrail_cleared_on_hold",
            }

        return decision, executed

    enabled_now, log_only_now, confirm_now = _runtime_exec_flags()

    guard_ok, guard_reason = guardrail_allows_execution(state, decision)
    execs["last_intent"] = {
        "ts": int(time.time()),
        "action": act,
        "rule": decision.get("rule") or decision.get("decision_rule_id"),
        "reason": decision.get("reason"),
    }
    execs["ft_url"] = FT_URL
    execs["pair"] = os.getenv("PAIR", PAIR)

    if not enabled_now:
        execs["last_result"] = {"ts": int(time.time()), "ok": False, "detail": "execution_disabled"}
        log(f"EXEC_DISABLED: act={act} rule={decision.get('rule')} reason={decision.get('reason')}")
        return decision, executed

    # U-0.4 (7.4): hard freshness gate KÖZVETLENÜL a végrehajtás előtt.
    # Döntés keletkezhet, de order nem mehet ki, amíg a jelenlegi processz nem
    # bizonyított friss piaci adatot ÉS nem horgonyozta újra az állapotot.
    fresh_ok, fresh_reason = runtime_freshness.execution_gate_status()
    execs["freshness_gate"] = runtime_freshness.gate_snapshot()
    if not fresh_ok:
        execs["last_result"] = {
            "ts": int(time.time()),
            "ok": False,
            "detail": f"freshness_block:{fresh_reason}",
        }
        log(
            f"EXEC_FRESHNESS_BLOCK: act={act} rule={decision.get('rule')} "
            f"reason={decision.get('reason')} block={fresh_reason}"
        )
        return decision, executed

    # U-3: Position Authority hard gate. BUY csak bizonyított FLAT-ben, SELL
    # csak bizonyított OPEN-ben; UNKNOWN alatt egyik sem. Csak `live` módban
    # blokkol – `off`/`shadow` alatt nincs olyan verdikt, amire támaszkodhatnánk.
    if position_authority.get_mode() == "live":
        pos_ok, pos_reason = position_authority.execution_allowed(state, act)
        execs["position_authority"] = {
            "state": position_authority.authority_state(state),
            "trade_id": position_authority.authority_trade_id(state),
            "gate": pos_reason,
        }
        if not pos_ok:
            execs["last_result"] = {
                "ts": int(time.time()),
                "ok": False,
                "detail": f"position_block:{pos_reason}",
            }
            log(
                f"EXEC_POSITION_BLOCK: act={act} rule={decision.get('rule')} "
                f"reason={decision.get('reason')} block={pos_reason}"
            )
            return decision, executed

    if not guard_ok:
        execs["last_result"] = {"ts": int(time.time()), "ok": False, "detail": f"guardrail_block:{guard_reason}"}
        log(f"EXEC_GUARDRAIL_BLOCK: act={act} rule={decision.get('rule')} reason={decision.get('reason')} block={guard_reason}")
        return decision, executed

    if log_only_now:
        execs["last_result"] = {"ts": int(time.time()), "ok": False, "detail": "log_only_no_call"}
        log(f"EXEC_LOG_ONLY: act={act} rule={decision.get('rule')} reason={decision.get('reason')} (NO API CALL)")
        return decision, executed

    try:
        import freqtrade_executor
        ex = freqtrade_executor.FreqtradeExecutor()

        if act == "BUY":
            r = ex.force_enter()
        else:
            sell_ok, sell_guard_reason = sell_execution_profit_guard(state, decision)
            if not sell_ok:
                execs["last_result"] = {
                    "ts": int(time.time()),
                    "ok": False,
                    "detail": sell_guard_reason,
                }
                execs["last_call"] = {
                    "ts": int(time.time()),
                    "action": act,
                    "ok": False,
                    "detail": sell_guard_reason,
                    "blocked_before_api": True,
                }
                log(f"EXEC_SELL_PROFIT_GUARD_BLOCK: {sell_guard_reason}")
                return decision, executed

            log(f"EXEC_SELL_PROFIT_GUARD_OK: {sell_guard_reason}")
            r = ex.force_exit()

        executed = True
        _apply_cycle_rules_after_execution(state, decision)
        g = _guardrail_counters(state)
        g["trades_today"] = int(g.get("trades_today", 0)) + 1
        execs["last_call"] = {
            "ts": int(time.time()),
            "action": act,
            "http": r.http_status,
            "ok": r.ok,
            "detail": r.detail,
            "response": r.response,
        }
        execs["last_result"] = {"ts": int(time.time()), "ok": bool(r.ok), "detail": r.detail}

        if confirm_now:
            c = ex.confirm_open_trades()
            execs["last_confirm"] = {
                "ts": int(time.time()),
                "ok": c.ok,
                "detail": c.detail,
                "http": c.http_status,
                "response": c.response,
            }

        return decision, executed

    except Exception as e:
        execs["last_result"] = {"ts": int(time.time()), "ok": False, "detail": f"exec_exception: {type(e).__name__}: {e}"}
        return decision, executed


def run_once() -> tuple[bool, float | None, dict]:
    state = _read_state()
    if not isinstance(state, dict):
        state = {}

    refresh_exec_flags_every_tick(state)
    _guardrail_counters(state)

    pair = os.getenv("PAIR", PAIR)
    timeframe = os.getenv("TIMEFRAME", TIMEFRAME)
    limit = int(os.getenv("LIMIT", str(LIMIT)))

    last_close = None
    try:
        market0 = state.get("market") if isinstance(state.get("market"), dict) else {}
        last_close = _as_float(market0.get("last", state.get("last")))

        # U-3: a pozícióállapot forrása módfüggő.
        #   off    -> a legacy sync (változatlan viselkedés)
        #   shadow -> legacy sync + megfigyelő authority (state["position"] only)
        #   live   -> KIZÁRÓLAG az authority; a legacy sync nem fut (egy író)
        pa_mode = position_authority.get_mode()
        authority_state_now: str | None = None
        if pa_mode == "live":
            snapshot = position_authority.authority_tick(state, pair, mode="live")
            authority_state_now = (
                snapshot.authority_state if snapshot is not None
                else position_authority.STATE_UNKNOWN
            )
        else:
            sync_position_from_freqtrade(state, pair)
            if pa_mode == "shadow":
                position_authority.authority_tick(state, pair, mode="shadow")

        position_frozen = authority_state_now == position_authority.STATE_UNKNOWN

        effective_limit = max(int(limit), int(MA_LONG_PERIOD))
        candles = fetch_candles(pair, timeframe, limit=effective_limit)
        state, _prev_close, last_close = update_state_from_candles(state, pair, timeframe, candles)

        # U-0.4: a perzisztált ciklus-horgony újraértékelése MIELŐTT az
        # árszintek kiszámolódnának – különben egy hónapokkal régi `base`
        # azonnali CATASTROPHE döntést szülne.
        reconcile_startup_anchor(state, last_close, authority_state_now)

        market = state.get("market") if isinstance(state.get("market"), dict) else {}
        if position_frozen:
            # U-3: UNKNOWN alatt a POZÍCIÓFÜGGŐ állapot fagyasztva. A piaci
            # mezők (market.*, last, close/open/high/low, volume, MA) az előző
            # lépésben már frissültek – azok forrása a gyertya, nem a pozíció.
            log("POSITION_FROZEN: authority_state=UNKNOWN -> peak/trough/levels/cycle unchanged")
        else:
            update_peak_trough(state, market)
            ensure_levels(state)
            update_recovery_state(state)
            _mirror_cycle_to_state(state)

        state["fee_buy_pct"] = FEE_PCT / 100.0
        state["fee_sell_pct"] = FEE_PCT / 100.0
        state["slippage_est_pct"] = SLIPPAGE_PCT / 100.0

        decision = decision_from_rule_engine(state)
        decision = apply_buy_cooldown_guard(state, decision)
        decision = apply_buy_lock(state, decision)
        apply_decision_contract(state, decision)

        decision, executed = maybe_execute_via_api(state, decision)
        if executed:
            apply_decision_contract(state, decision)

        execs = _ensure_exec_section(state)
        execs["ft_url"] = FT_URL
        execs["freshness_gate"] = runtime_freshness.gate_snapshot()

        if _runtime_shadow_enabled():
            from shadow_position import maybe_run_shadow_tick
            maybe_run_shadow_tick(state)

        # U-0.4 (8.1): kanonikus időbélyegzés a SIKERES tick végén, egyetlen
        # `now` értékből. Hibaágon szándékosan nem fut le.
        runtime_freshness.stamp_tick_timestamps(state)

        _write_state(state)
        return True, last_close, decision

    except Exception as e:
        reason = f"API_UNAVAILABLE: {type(e).__name__}: {e}"

        decision = normalize_decision(
            action="HOLD",
            rule="API_UNAVAILABLE",
            reason=reason,
            level="none",
        )
        apply_decision_contract(state, decision)

        # U-0.4 (8.1): hibaágon a frissesség-időbélyeget NEM frissítjük –
        # különben a state "frissnek" hazudná magát egy sikertelen tick után.
        # A hiba ideje külön mezőbe kerül, és a gate fail-closed módon zár.
        state["last_error_at"] = now_utc_iso()
        runtime_freshness.mark_market_fetch_failed()

        execs = _ensure_exec_section(state)
        execs["ft_url"] = FT_URL
        execs["freshness_gate"] = runtime_freshness.gate_snapshot()
        execs["last_result"] = {
            "ts": int(time.time()),
            "ok": False,
            "detail": reason,
        }

        _write_state(state)
        return False, last_close, decision


def main_loop() -> None:
    enabled0, log_only0, _confirm0 = _runtime_exec_flags()
    log(
        f"started ({TICK_SECONDS:.1f}s tick, Freqtrade -> state.json, auth={'on' if (FT_USERNAME and FT_PASSWORD) else 'off'}, "
        f"exec_enabled={'on' if enabled0 else 'off'}, exec_log_only={'on' if log_only0 else 'off'})"
    )

    while True:
        t0 = time.time()
        ok = False
        last_price = None
        decision = {"action": "HOLD", "reason": "INIT", "rule": "INIT", "level": "none"}
        reason = "INIT"

        try:
            ok, last_price, decision = run_once()
            reason = decision.get("reason", "NO_REASON")
            log(f"price={last_price} pair={PAIR} ok={ok} decision={decision.get('action')} reason={reason}")
        except Exception as e:
            reason = f"EXC: {type(e).__name__}: {e}"
            log(f"ERROR {reason}")

        elapsed = time.time() - t0
        sleep_for = max(0.0, float(TICK_SECONDS) - elapsed)
        log(f"loop_end ok={ok} elapsed={elapsed:.3f}s sleep_for={sleep_for:.3f} reason={reason}")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main_loop()
