import json
import os
import execution_policy
import ft_endpoint
import freqtrade_ui
import tempfile
import hashlib
import subprocess
import csv
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, Response, redirect, request

from rules_loader import (
    load_rules_doc,
    save_rules_doc,
    load_lab_override_doc,
    save_lab_override_doc,
    list_lab_profiles,
)
from rules_merge import build_effective_rules

# U-0.1 biztonsági patch: bemenet-validáció (U0-SEC-001) és központi
# authentikáció/CSRF (U0-SEC-003/004/005).
import web_security
from settings_validation import (
    SettingsValidationError,
    assert_dropin_safe,
    validate_settings_payload,
)


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))

STATE_PATH = os.path.join(ROOT_DIR, "state.json")
FREQTRADE_CONFIG_PATH = os.path.join(ROOT_DIR, "freqtrade", "user_data", "config.json")
SETTINGS_ENV_FILE = "/etc/systemd/system/uranus-runner.service.d/30-canonical-env.conf"

POLL_INTERVAL_MS = 15000

MONITOR_DATA_DIR = "/opt/bots/uranus_monitor/data"
MONITOR_PRICES_CSV = os.path.join(MONITOR_DATA_DIR, "prices.csv")


def _parse_iso_ts(value: str):
    if not value:
        return None
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def load_chart_history_from_prices_csv(days: int = 10, short_days: int = 1, long_days: int = 10):
    rows = []
    try:
        with open(MONITOR_PRICES_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = _parse_iso_ts(row.get("timestamp_utc"))
                if ts is None:
                    continue
                try:
                    last = float(row.get("last"))
                except Exception:
                    continue
                rows.append((ts, last))
    except Exception as e:
        return {
            "ok": False,
            "error": "prices_csv_unavailable",
            "detail": str(e),
            "labels": [],
            "price": [],
            "ma_short": [],
            "ma_long": [],
        }

    if not rows:
        return {
            "ok": False,
            "error": "prices_csv_empty",
            "labels": [],
            "price": [],
            "ma_short": [],
            "ma_long": [],
        }

    rows.sort(key=lambda x: x[0])

    # perces gyertyásítás: adott perc utolsó ára maradjon meg
    minute_map = {}
    minute_order = []
    for ts, last in rows:
        minute_ts = ts.replace(second=0, microsecond=0)
        key = minute_ts.isoformat()
        if key not in minute_map:
            minute_order.append(key)
        minute_map[key] = (minute_ts, last)

    minute_rows = [minute_map[k] for k in minute_order]
    if not minute_rows:
        return {
            "ok": False,
            "error": "minute_rows_empty",
            "labels": [],
            "price": [],
            "ma_short": [],
            "ma_long": [],
        }

    max_ts = minute_rows[-1][0]
    cutoff = max_ts.timestamp() - (int(days) * 86400)

    filtered = [(ts, last) for ts, last in minute_rows if ts.timestamp() >= cutoff]
    if not filtered:
        filtered = minute_rows[-min(len(minute_rows), int(days) * 1440):]

    short_window = max(1, int(short_days) * 1440)
    long_window = max(1, int(long_days) * 1440)

    labels = []
    price = []
    ma_short = []
    ma_long = []

    short_buf = []
    long_buf = []

    for ts, last in filtered:
        labels.append(ts.isoformat().replace("+00:00", "Z"))
        price.append(last)

        short_buf.append(last)
        if len(short_buf) > short_window:
            short_buf.pop(0)

        long_buf.append(last)
        if len(long_buf) > long_window:
            long_buf.pop(0)

        ma_short.append(sum(short_buf) / len(short_buf) if short_buf else None)
        ma_long.append(sum(long_buf) / len(long_buf) if long_buf else None)

    return {
        "ok": True,
        "source": MONITOR_PRICES_CSV,
        "days": int(days),
        "short_days": int(short_days),
        "long_days": int(long_days),
        "points": len(labels),
        "labels": labels,
        "price": price,
        "ma_short": ma_short,
        "ma_long": ma_long,
    }

import os

def get_env(name, default=None):
    return os.environ.get(name, default)

def env_float(name, default=0.0):
    try:
        return float(get_env(name, default))
    except:
        return float(default)

def env_int(name, default=0):
    try:
        return int(float(get_env(name, default)))
    except:
        return int(default)
app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"))

# U-0.1: minden state-changing kérés központi védelem alá kerül.
# A GET/HEAD olvasási út változatlanul nyitva marad.
web_security.install_security(app)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def local_time_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path: str, obj) -> None:
    dirn = os.path.dirname(path) or "."
    base = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=dirn)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def resolve_pair(state: dict) -> str:
    candidates = [
        state.get("pair"),
        (state.get("market") or {}).get("pair"),
        (state.get("prices") or {}).get("pair"),
        (state.get("meta") or {}).get("pair"),
    ]

    ex = state.get("execution") or {}

    last_call = ex.get("last_call") or {}
    last_call_resp = last_call.get("response") or {}
    candidates.append(last_call_resp.get("pair"))

    raw = last_call_resp.get("raw")
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        candidates.append(raw[0].get("pair"))

    last_confirm = ex.get("last_confirm") or {}
    last_confirm_resp = last_confirm.get("response") or {}
    candidates.append(last_confirm_resp.get("pair"))

    raw2 = last_confirm_resp.get("raw")
    if isinstance(raw2, list) and raw2 and isinstance(raw2[0], dict):
        candidates.append(raw2[0].get("pair"))

    for v in candidates:
        if isinstance(v, str) and v.strip():
            return v.strip()

    return "XRP/USDC"


def _ensure_dict(state: dict, key: str) -> bool:
    if key not in state or not isinstance(state.get(key), dict):
        state[key] = {}
        return True
    return False


def _sync_flat_from_nested(state: dict, flat_key: str, nested: dict, nested_key: str) -> bool:
    if nested is None:
        return False
    if nested_key not in nested:
        return False
    val = nested.get(nested_key)
    if val is None:
        return False
    if state.get(flat_key) != val:
        state[flat_key] = val
        return True
    return False


def ensure_state_schema(state: dict) -> dict:
    changed = False

    if state.get("schema_version") is None:
        state["schema_version"] = 2
        changed = True

    resolved_pair = resolve_pair(state)

    defaults = {
        "active_rule": None,
        "decision": state.get("decision") or None,
        "exchange": state.get("exchange") or "binance",
        "pair": resolved_pair,
        "in_position": bool(state.get("in_position")) if state.get("in_position") is not None else False,
        "last_action": state.get("last_action") or None,
        "reason": state.get("reason") or None,
    }

    for k, v in defaults.items():
        if k not in state:
            state[k] = v
            changed = True

    if not state.get("pair"):
        state["pair"] = resolved_pair
        changed = True

    changed |= _ensure_dict(state, "bot")
    if "name" not in state["bot"]:
        state["bot"]["name"] = "Uranus"
        changed = True
    if "mode" not in state["bot"]:
        state["bot"]["mode"] = "live"
        changed = True
    if "enabled" not in state["bot"]:
        state["bot"]["enabled"] = True
        changed = True

    changed |= _ensure_dict(state, "flags")
    for k in ["had_enough_rise", "had_enough_drop", "not_enough_drop", "panic_mode", "buy_hierarchy_ok", "sell_hierarchy_ok", "ma_positive", "ma_negative", "ma_filter_enabled"]:
        if k not in state["flags"]:
            state["flags"][k] = False
            changed = True

    six_keys = [
        "std_sell",
        "recovery_sell",
        "panic_sell",
        "catastrophe_sell",
        "std_buy",
        "recovery_buy",
        "panic_buy",
        "catastrophe_buy",
        "reference_base",
    ]

    changed |= _ensure_dict(state, "levels")
    for k in six_keys:
        if k not in state["levels"]:
            state["levels"][k] = None
            changed = True

    changed |= _ensure_dict(state, "targets")
    for k in six_keys:
        if k not in state["targets"]:
            state["targets"][k] = state["levels"].get(k)
            changed = True

    changed |= _ensure_dict(state, "rules")
    for k in six_keys:
        if k not in state["rules"]:
            state["rules"][k] = state["levels"].get(k)
            changed = True

    changed |= _ensure_dict(state, "extremes")
    for k in ["high", "low", "high_timestamp_utc", "low_timestamp_utc"]:
        if k not in state["extremes"]:
            state["extremes"][k] = None
            changed = True

    changed |= _ensure_dict(state, "stats")
    for k in ["ticks", "last_tick_ts"]:
        if k not in state["stats"]:
            state["stats"][k] = 0 if k == "ticks" else None
            changed = True

    changed |= _ensure_dict(state, "meta")
    for k in ["created_at", "updated_at", "created_utc", "updated_utc"]:
        if k not in state["meta"]:
            state["meta"][k] = None
            changed = True
    if "ft" not in state["meta"] or not isinstance(state["meta"].get("ft"), dict):
        state["meta"]["ft"] = {}
        changed = True

    changed |= _ensure_dict(state, "prices")
    for k in ["base", "last", "low", "peak", "prev_last", "trough", "open", "source", "tick_ts_utc", "updated_at", "pair"]:
        if k not in state["prices"]:
            state["prices"][k] = None
            changed = True

    if not state["prices"].get("pair"):
        state["prices"]["pair"] = resolved_pair
        changed = True

    if "base_price" in state and state["prices"].get("base") is None:
        state["prices"]["base"] = state.get("base_price")
        changed = True
    if "low_price" in state and state["prices"].get("low") is None:
        state["prices"]["low"] = state.get("low_price")
        changed = True
    if "peak_price" in state and state["prices"].get("peak") is None:
        state["prices"]["peak"] = state.get("peak_price")
        changed = True

    for k in ("last", "prev_last", "base", "low", "peak", "trough", "open"):
        if k in state:
            if state["prices"].get(k) != state.get(k):
                state["prices"][k] = state.get(k)
                changed = True
        else:
            changed |= _sync_flat_from_nested(state, k, state["prices"], k)

    for k in ["had_enough_rise", "had_enough_drop", "not_enough_drop", "panic_mode", "buy_hierarchy_ok", "sell_hierarchy_ok", "ma_positive", "ma_negative", "ma_filter_enabled"]:
        changed |= _sync_flat_from_nested(state, k, state["flags"], k)

    for k in six_keys:
        changed |= _sync_flat_from_nested(state, k, state["levels"], k)

    if not state.get("started_utc"):
        state["started_utc"] = utc_now_iso()
        changed = True

    if state.get("start_last") is None:
        last = state["prices"].get("last")
        if last is not None:
            state["start_last"] = last
            changed = True

    # U-0.4 (8.3): innen ELTÁVOLÍTVA a korábbi write-once `updated_utc` backfill.
    # Az a guard (`if not state.get("updated_utc")`) egyszer beírta a UI saját
    # idejét, majd soha többé nem frissítette – így a mező hónapokra befagyott,
    # miközben a runner egy másik nevű mezőt írt. Az olvasási útvonal (UI/API)
    # nem lehet a freshness-timestamp kanonikus írója; a kanonikus írás a
    # runnerben történik (runtime_freshness.stamp_tick_timestamps).
    # A legacy megjelenítési fallback a view-ban van, állapotírás nélkül.

    if changed:
        atomic_write_json(STATE_PATH, state)

    return state


def load_state_view() -> dict:
    state = read_json(STATE_PATH, default={})
    state = ensure_state_schema(state)

    cfg = read_json(FREQTRADE_CONFIG_PATH, default={})
    stake = {
        "stake_currency": cfg.get("stake_currency"),
        "stake_amount": cfg.get("stake_amount"),
        "dry_run": cfg.get("dry_run"),
    }

    try:
        dry = bool(cfg.get("dry_run"))
        mode = "DRY_RUN" if dry else "LIVE"
        view_freqtrade = {
            "dry_run": dry,
            "mode": mode,
            "trading_mode": cfg.get("trading_mode"),
        }
    except Exception:
        view_freqtrade = {}

    view = dict(state)
    view["pair"] = resolve_pair(state)

    # U-0.4 (8.3/8.4): megjelenítési fallback, NEM állapotírás. Régi state-ben
    # hiányozhat az `updated_utc`; ilyenkor a runner által írt `updated_at`
    # aliast mutatjuk, hogy az X-Updated-Utc header se adjon hamis értéket.
    if not view.get("updated_utc"):
        view["updated_utc"] = state.get("updated_at") or state.get("time")

    if not isinstance(view.get("freqtrade"), dict):
        view["freqtrade"] = view_freqtrade
    else:
        for k, v in view_freqtrade.items():
            view["freqtrade"].setdefault(k, v)

    view["stake"] = stake
    view["stake_currency"] = stake.get("stake_currency")
    view["stake_amount"] = stake.get("stake_amount")
    view["stake_dry_run"] = stake.get("dry_run")

    ft = freqtrade_ui.snapshot()
    view["ft"] = ft

    ft_ping = ft.get("ping") if isinstance(ft, dict) else None
    ft_ping_ok = bool(isinstance(ft_ping, dict) and ft_ping.get("status_code") == 200)

    ft_show = ft.get("show_config") if isinstance(ft, dict) else None
    ft_show_ok = bool(isinstance(ft_show, dict) and ft_show.get("status_code") == 200)
    ft_show_body = ft_show.get("body") if isinstance(ft_show, dict) else None
    ft_state = ft_show_body.get("state") if isinstance(ft_show_body, dict) else None

    view["ft_api_ok"] = ft_ping_ok
    view["ft_auth_ok"] = ft_show_ok
    view["ft_engine_state"] = (ft_state.upper() if isinstance(ft_state, str) else None)
    view["ft_trader_running"] = (str(ft_state).lower() == "running") if ft_state is not None else False

    ft_cnt = ft.get("count") if isinstance(ft, dict) else None
    ft_cnt_code = ft_cnt.get("status_code") if isinstance(ft_cnt, dict) else None
    ft_cnt_body = ft_cnt.get("body") if isinstance(ft_cnt, dict) else None
    ft_cnt_err = ft_cnt_body.get("error") if isinstance(ft_cnt_body, dict) else None
    view["ft_count_status_code"] = ft_cnt_code
    view["ft_count_error"] = ft_cnt_err

    st_engine = view.get("ft_engine_state")
    if st_engine == "STOPPED" and isinstance(ft_cnt_err, str) and "trader is not running" in ft_cnt_err.lower():
        view["ui_count_ok"] = True
        view["ui_count_reason"] = "trader_stopped"
    else:
        view["ui_count_ok"] = bool(ft_cnt_code == 200 and isinstance(ft_cnt_body, dict))
        view["ui_count_reason"] = None if view["ui_count_ok"] else (ft_cnt_err or "count_unavailable")

    sc_body = None
    pf_body = None
    st_body = None

    if isinstance(ft, dict):
        sc = ft.get("show_config")
        pf = ft.get("profit")
        stx = ft.get("status")
        if isinstance(sc, dict):
            sc_body = sc.get("body")
        if isinstance(pf, dict):
            pf_body = pf.get("body")
        if isinstance(stx, dict):
            st_body = stx.get("body")

    if isinstance(sc_body, dict):
        view["ft_exchange"] = sc_body.get("exchange")
        view["ft_timeframe"] = sc_body.get("timeframe")
        view["ft_strategy"] = sc_body.get("strategy")
        view["ft_runmode"] = sc_body.get("runmode")
        view["ft_dry_run"] = sc_body.get("dry_run")
        view["ft_trading_mode"] = sc_body.get("trading_mode")
        view["ft_stake_currency"] = sc_body.get("stake_currency")
        view["ft_stake_amount"] = sc_body.get("stake_amount")
        view["ft_max_open_trades"] = sc_body.get("max_open_trades")

    if isinstance(st_body, list):
        view["ft_open_trades_count"] = len(st_body)
    else:
        view["ft_open_trades_count"] = None

    if isinstance(pf_body, dict):
        view["ft_profit"] = dict(pf_body)
        view["ft_profit_all_percent"] = None
        view["ft_profit_all_coin"] = None
        view["ft_profit_closed_coin"] = pf_body.get("profit_closed_coin")
        view["ft_profit_closed_percent"] = pf_body.get("profit_closed_percent")
        view["ft_trade_count"] = pf_body.get("trade_count")
        view["ft_closed_trade_count"] = pf_body.get("closed_trade_count")
        view["ft_winning_trades"] = pf_body.get("winning_trades")
        view["ft_losing_trades"] = pf_body.get("losing_trades")
        view["ft_profit_factor"] = pf_body.get("profit_factor")
        view["ft_winrate"] = pf_body.get("winrate")
        view["ft_bot_start_date"] = pf_body.get("bot_start_date")
    else:
        view["ft_profit"] = {}

    pair = view.get("pair") or "XRP/USDC"
    base_symbol = pair.split("/")[0] if "/" in pair else None

    # U-2B (Patch B): egyetlen kanonikus Freqtrade-authority.
    #
    # A korábbi lánc a state.json-ból (`state["ft"]["url"]`,
    # `state["meta"]["ft"]["base_url"]`) is elfogadott végpontot, majd négy
    # különböző fallbackon át jutott el a 8090-hez. Ez két bajt okozott:
    # a state konfigurációs authorityvé vált, és ugyanabban a processzben
    # eltérhetett attól a címtől, amit a `freqtrade_ui` használt (U-2A: 8017).
    # Innentől mindkét fogyasztó ugyanazt a feloldót hívja.
    ft_url = ft_endpoint.canonical_ft_url()

    view["ft_base_url"] = ft_url

    balance_ok = False
    balance_error = None
    balance_usdc = None
    balance_base = None
    wallet_balances = {}

    auth = None
    try:
        import requests

        cfg = read_json(FREQTRADE_CONFIG_PATH, default={})
        api = (cfg.get("api_server") or {})
        ft_user = api.get("username")
        ft_pass = api.get("password")
        auth = (ft_user, ft_pass) if ft_user and ft_pass else None

        r = requests.get(f"{ft_url}/api/v1/balance", auth=auth, timeout=5)

        if r.status_code == 200:
            j = r.json()
            curmap = {}

            if isinstance(j, dict) and "currencies" in j:
                c = j.get("currencies")
                if isinstance(c, dict):
                    curmap = c
                elif isinstance(c, list):
                    for it in c:
                        if isinstance(it, dict) and it.get("currency"):
                            curmap[it["currency"]] = it
            elif isinstance(j, list):
                for it in j:
                    if isinstance(it, dict) and it.get("currency"):
                        curmap[it["currency"]] = it

            def pick_amount(obj, keys):
                if not isinstance(obj, dict):
                    return 0.0
                for k in keys:
                    v = obj.get(k)
                    if isinstance(v, (int, float)):
                        return float(v)
                    try:
                        if isinstance(v, str) and v.strip():
                            return float(v)
                    except Exception:
                        pass
                return 0.0

            for sym, obj in curmap.items():
                total = pick_amount(obj, ("balance", "total", "free"))
                free = pick_amount(obj, ("free", "balance", "total"))
                used = pick_amount(obj, ("used", "locked"))
                wallet_balances[sym] = {
                    "free": free,
                    "locked": used,
                    "total": total,
                }

            if "USDC" in curmap:
                balance_usdc = pick_amount(curmap.get("USDC"), ("balance", "total", "free"))
            if base_symbol and base_symbol in curmap:
                balance_base = pick_amount(curmap.get(base_symbol), ("balance", "total", "free"))

            balance_ok = True
        else:
            balance_error = f"HTTP_{r.status_code}"
    except Exception as e:
        balance_error = str(e)

    view["balance_usdc"] = balance_usdc
    view["balance_base"] = balance_base
    view["balance_base_symbol"] = base_symbol
    view["balance_ok"] = balance_ok
    view["balance_error"] = balance_error

    profit_lifetime_coin = view.get("ft_profit_all_coin")
    profit_lifetime_pct = view.get("ft_profit_all_percent")
    profit_today_coin = None
    profit_today_pct = None
    profit_current_coin = None
    profit_current_pct = None

    try:
        import requests
        import datetime as dtmod

        r = requests.get(f"{ft_url}/api/v1/status", auth=auth, timeout=5)
        if r.status_code == 200:
            trades = r.json()
            if isinstance(trades, list) and trades:
                t = trades[0]
                profit_current_coin = t.get("profit_abs")
                profit_current_pct = t.get("profit_ratio")

        r = requests.get(f"{ft_url}/api/v1/trades", auth=auth, timeout=5)
        if r.status_code == 200:
            trades = r.json()
            if isinstance(trades, list):
                today = dtmod.datetime.utcnow().date()
                sum_coin = 0.0
                for t in trades:
                    c = t.get("close_date")
                    if c:
                        d = dtmod.datetime.fromisoformat(c.replace("Z", "")).date()
                        if d == today:
                            sum_coin += float(t.get("profit_abs", 0))
                profit_today_coin = sum_coin
    except Exception:
        pass

    view["profit_current_coin"] = profit_current_coin
    view["profit_current_pct"] = profit_current_pct
    view["profit_today_coin"] = profit_today_coin
    view["profit_today_pct"] = profit_today_pct
    view["profit_lifetime_coin"] = profit_lifetime_coin
    view["profit_lifetime_pct"] = profit_lifetime_pct

    view["wallet"] = {
        "binance_spot": {
            "status": "OK" if balance_ok else ("ERR" if balance_error else "N/A"),
            "balances": wallet_balances,
        }
    }

    prev_in_pos = bool(state.get("in_position"))
    now_in_pos = bool(view.get("in_position", state.get("in_position")))

    cyc = state.get("cycle") or {}
    last_closed = (cyc.get("last_closed") or {})

    prices = state.get("prices") or {}
    base_raw = state.get("base")
    if base_raw is None:
        base_raw = state.get("base_price")
    if base_raw is None:
        base_raw = prices.get("base")
    base = float(base_raw or 0.0)
    last = float(prices.get("last") or state.get("last") or 0.0)

    if (not prev_in_pos) and now_in_pos:
        entry_price = base if base > 0 else (last if last > 0 else None)
        cyc["in_cycle"] = True
        cyc["entry_price"] = entry_price
        cyc["entry_utc"] = cyc.get("entry_utc") or __import__("state_schema").utc_now()

    if prev_in_pos and (not now_in_pos):
        entry_price = cyc.get("entry_price") or (base if base > 0 else None)
        exit_price = last if last > 0 else None

        profit_pc = None
        try:
            if entry_price and exit_price and float(entry_price) != 0.0:
                profit_pc = (float(exit_price) - float(entry_price)) / float(entry_price) * 100.0
        except Exception:
            profit_pc = None

        last_closed["profit_percent"] = profit_pc
        last_closed["entry_price"] = entry_price
        last_closed["exit_price"] = exit_price
        last_closed["closed_utc"] = __import__("state_schema").utc_now()

        cyc["last_closed"] = last_closed
        try:
            cyc["count"] = int(cyc.get("count") or 0) + 1
        except Exception:
            cyc["count"] = 1

        cyc["in_cycle"] = False
        cyc["entry_price"] = None
        cyc["entry_utc"] = None

    state["cycle"] = cyc
    view["cycle_profit_percent"] = (state.get("cycle") or {}).get("last_closed", {}).get("profit_percent")
    view["cycle_last_closed"] = last_closed
    view["cycle_count"] = cyc.get("count")

    pf = ft.get("profit") if isinstance(ft, dict) else None
    pf_ok = bool(isinstance(pf, dict) and pf.get("status_code") == 200 and isinstance(pf.get("body"), dict))

    profit_lifetime_coin = None
    profit_lifetime_pct = None
    profit_today_coin = 0.0
    profit_today_pct = None
    profit_current_coin = None
    profit_current_pct = None

    session_start_total = state.get("ui_session_start_total")
    session_start_utc = state.get("ui_session_start_utc")

    try:
        import requests

        current_total = None

        r_balance2 = requests.get(f"{ft_url}/api/v1/balance", auth=auth, timeout=10)
        if r_balance2.status_code == 200:
            j_balance2 = r_balance2.json()
            if isinstance(j_balance2, dict):
                total_val = j_balance2.get("total")
                try:
                    if total_val is not None:
                        current_total = float(total_val)
                except Exception:
                    current_total = None

        if session_start_total is None and current_total is not None:
            session_start_total = float(current_total)
            session_start_utc = utc_now_iso()
            state["ui_session_start_total"] = session_start_total
            state["ui_session_start_utc"] = session_start_utc
            atomic_write_json(STATE_PATH, state)

        if current_total is not None and session_start_total is not None:
            profit_lifetime_coin = float(current_total) - float(session_start_total)
            if float(session_start_total) != 0:
                profit_lifetime_pct = (profit_lifetime_coin / float(session_start_total)) * 100.0

        r_status2 = requests.get(f"{ft_url}/api/v1/status", auth=auth, timeout=10)
        if r_status2.status_code == 200:
            st_body2 = r_status2.json()
            if isinstance(st_body2, list) and st_body2:
                t = st_body2[0]
                try:
                    if t.get("profit_abs") is not None:
                        profit_current_coin = float(t.get("profit_abs"))
                except Exception:
                    profit_current_coin = None

                try:
                    if t.get("profit_pct") is not None:
                        profit_current_pct = float(t.get("profit_pct"))
                    elif t.get("profit_ratio") is not None:
                        profit_current_pct = float(t.get("profit_ratio")) * 100.0
                except Exception:
                    profit_current_pct = None

        r_trades2 = requests.get(f"{ft_url}/api/v1/trades", auth=auth, timeout=15)
        if r_trades2.status_code == 200:
            tr_body2 = r_trades2.json()

            if isinstance(tr_body2, dict):
                trades2 = tr_body2.get("trades") or tr_body2.get("data") or []
            elif isinstance(tr_body2, list):
                trades2 = tr_body2
            else:
                trades2 = []

            today_utc = datetime.now(timezone.utc).date()
            today_sum = 0.0

            for t in trades2:
                if not isinstance(t, dict):
                    continue

                close_date = t.get("close_date")
                if not close_date:
                    continue

                dt = None
                raw = str(close_date).strip()

                try:
                    raw2 = raw.replace(" ", "T")
                    if raw2.endswith("Z"):
                        dt = datetime.fromisoformat(raw2.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(raw2)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None

                if dt is None:
                    continue

                if dt.astimezone(timezone.utc).date() == today_utc:
                    try:
                        today_sum += float(t.get("profit_abs") or 0.0)
                    except Exception:
                        pass

            profit_today_coin = today_sum

    except Exception:
        pass

    view["profit_current_coin"] = profit_current_coin
    view["profit_current_pct"] = profit_current_pct
    view["profit_today_coin"] = profit_today_coin
    view["profit_today_pct"] = profit_today_pct
    view["profit_lifetime_coin"] = profit_lifetime_coin
    view["profit_lifetime_pct"] = profit_lifetime_pct
    view["ui_session_start_total"] = session_start_total
    view["ui_session_start_utc"] = session_start_utc

    view["ui_profit_source"] = "freqtrade"
    view["ui_profit_ok"] = pf_ok
    view["ui_profit_reason"] = None if pf_ok else "profit_endpoint_unavailable"

    otc = view.get("ft_open_trades_count")
    if isinstance(otc, int):
        view["ui_in_position"] = (otc > 0)
        view["ui_in_position_source"] = "freqtrade"
    else:
        view["ui_in_position"] = False
        view["ui_in_position_source"] = "freqtrade"

    view["ui_now"] = local_time_str()
    return view


def state_hash(view: dict) -> str:
    v = dict(view or {})
    v.pop("ui_now", None)
    v.pop("time_local", None)
    v.pop("now_local", None)
    v.pop("ft", None)
    raw = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def env_decimal_to_percent(value) -> float:
    return round(float(value) * 100.0, 6)


def percent_to_env_decimal(value) -> float:
    return round(float(value) / 100.0, 8)


def env_to_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def bool_to_env(value: bool) -> str:
    return "1" if value else "0"


def read_trading_settings():
    # U-2B (Patch A): a végrehajtási alapértelmezés fail-safe.
    #
    # Korábban itt `execution_enabled=True` és `execution_log_only=False`
    # állt. Mivel a production drop-in EGYETLEN execution-flaget sem tartalmaz
    # (U-2A lelet), ezek az alapértelmezések túlélték a lenti felülírást, és a
    # UI egy tetszőleges „Mentés”-sel `EXECUTION_ENABLED=1`-et írt volna a
    # runner drop-inbe. A kanonikus értékek innentől egy helyen élnek:
    # `execution_policy` – ugyanabból olvas a runner is.
    cfg = {
        "tick_seconds": 2.0,
        "pair": "XRP/USDC",
        "timeframe": "1m",
        "limit": 5,
        **execution_policy.safe_settings_defaults(),
        "buy_lock_ttl_sec": 180,
        "kill_switch": False,
        "max_trades_per_day": 20,
        "daily_loss_cap_pct": 5.0,
        "ft_url": ft_endpoint.CANONICAL_FT_URL,

        "std_sell_enabled": True,
        "std_sell_pct": 1.0,
        "recovery_sell_retrace_pct": 0.3,
        "recovery_profit_target_pct": 0.1,
        "panic_sell_enabled": True,
        "panic_sell_pct": 1.0,
        "sell_reversal_min_pct": 0.0,
        "catastrophe_sell_pct": 10.0,

        "std_buy_enabled": True,
        "std_buy_pct": 1.0,
        "recovery_buy_rebound_pct": 0.3,
        "panic_buy_enabled": True,
        "panic_buy_pct": 1.0,
        "panic_buy_confirm_ticks": 2,
        "catastrophe_buy_pct": 10.0,

        "ma_filter_enabled": True,
        "ma_period": 20,
        "ma_sideways_band_pct": 0.05,

        "shadow_enabled": False,
        "shadow_start_equity_usdc": 0.0,
    }

    try:
        with open(SETTINGS_ENV_FILE, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()

                if line.startswith("Environment=TICK_SECONDS="):
                    cfg["tick_seconds"] = float(line.split("=", 2)[2])
                elif line.startswith("Environment=PAIR="):
                    cfg["pair"] = line.split("=", 2)[2]
                elif line.startswith("Environment=TIMEFRAME="):
                    cfg["timeframe"] = line.split("=", 2)[2]
                elif line.startswith("Environment=LIMIT="):
                    cfg["limit"] = int(float(line.split("=", 2)[2]))

                elif line.startswith("Environment=EXECUTION_ENABLED="):
                    cfg["execution_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=EXECUTION_LOG_ONLY="):
                    cfg["execution_log_only"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=BUY_LOCK_TTL_SEC="):
                    cfg["buy_lock_ttl_sec"] = int(float(line.split("=", 2)[2]))

                elif line.startswith("Environment=STD_SELL_ENABLED="):
                    cfg["std_sell_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=STD_SELL_PCT="):
                    cfg["std_sell_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=RECOVERY_SELL_RETRACE_PCT="):
                    cfg["recovery_sell_retrace_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=RECOVERY_PROFIT_TARGET_PCT="):
                    cfg["recovery_profit_target_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=PANIC_SELL_ENABLED="):
                    cfg["panic_sell_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=PANIC_SELL_PCT="):
                    cfg["panic_sell_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=SELL_REVERSAL_MIN_PCT="):
                    cfg["sell_reversal_min_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=CATASTROPHE_SELL_PCT="):
                    cfg["catastrophe_sell_pct"] = env_decimal_to_percent(line.split("=", 2)[2])

                elif line.startswith("Environment=STD_BUY_ENABLED="):
                    cfg["std_buy_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=STD_BUY_PCT="):
                    cfg["std_buy_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=RECOVERY_BUY_REBOUND_PCT="):
                    cfg["recovery_buy_rebound_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=PANIC_BUY_ENABLED="):
                    cfg["panic_buy_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=PANIC_BUY_PCT="):
                    cfg["panic_buy_pct"] = env_decimal_to_percent(line.split("=", 2)[2])
                elif line.startswith("Environment=PANIC_BUY_CONFIRM_TICKS="):
                    cfg["panic_buy_confirm_ticks"] = int(float(line.split("=", 2)[2]))
                elif line.startswith("Environment=CATASTROPHE_BUY_PCT="):
                    cfg["catastrophe_buy_pct"] = env_decimal_to_percent(line.split("=", 2)[2])

                elif line.startswith("Environment=MA_FILTER_ENABLED="):
                    cfg["ma_filter_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=MA_PERIOD="):
                    cfg["ma_period"] = int(float(line.split("=", 2)[2]))
                elif line.startswith("Environment=MA_SIDEWAYS_BAND_PCT="):
                    cfg["ma_sideways_band_pct"] = env_decimal_to_percent(line.split("=", 2)[2])

                elif line.startswith("Environment=KILL_SWITCH="):
                    cfg["kill_switch"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=MAX_TRADES_PER_DAY="):
                    cfg["max_trades_per_day"] = int(float(line.split("=", 2)[2]))
                elif line.startswith("Environment=DAILY_LOSS_CAP_PCT="):
                    cfg["daily_loss_cap_pct"] = float(line.split("=", 2)[2])
                elif line.startswith("Environment=FT_URL="):
                    cfg["ft_url"] = line.split("=", 2)[2]
                elif line.startswith("Environment=SHADOW_ENABLED="):
                    cfg["shadow_enabled"] = env_to_bool(line.split("=", 2)[2])
                elif line.startswith("Environment=SHADOW_START_EQUITY_USDC="):
                    cfg["shadow_start_equity_usdc"] = float(line.split("=", 2)[2])
    except Exception:
        pass

    return cfg


def build_settings_dropin(data) -> str:
    """
    A systemd drop-in szövegének előállítása (tiszta függvény, nincs I/O).

    A visszaadott szöveg átment az ``assert_dropin_safe`` strukturális
    ellenőrzésen: minden sora vagy üres, vagy ``[Service]``, vagy egy
    engedélyezett ``Environment=KULCS=ERTEK`` sor.
    """
    tpl = f"""[Service]

Environment=TICK_SECONDS={data['tick_seconds']}
Environment=PAIR={data['pair']}
Environment=TIMEFRAME={data['timeframe']}
Environment=LIMIT={data['limit']}

Environment=EXECUTION_ENABLED={bool_to_env(data['execution_enabled'])}
Environment=EXECUTION_LOG_ONLY={bool_to_env(data['execution_log_only'])}
Environment=BUY_LOCK_TTL_SEC={data['buy_lock_ttl_sec']}

Environment=STD_SELL_ENABLED={bool_to_env(data.get('std_sell_enabled', True))}
Environment=STD_SELL_PCT={percent_to_env_decimal(data['std_sell_pct'])}
Environment=RECOVERY_SELL_RETRACE_PCT={percent_to_env_decimal(data['recovery_sell_retrace_pct'])}
Environment=RECOVERY_PROFIT_TARGET_PCT={percent_to_env_decimal(data.get('recovery_profit_target_pct', 0.1))}
Environment=PANIC_SELL_ENABLED={bool_to_env(data.get('panic_sell_enabled', True))}
Environment=PANIC_SELL_PCT={percent_to_env_decimal(data['panic_sell_pct'])}
Environment=SELL_REVERSAL_MIN_PCT={percent_to_env_decimal(data.get('sell_reversal_min_pct', 0))}
Environment=CATASTROPHE_SELL_PCT={percent_to_env_decimal(data['catastrophe_sell_pct'])}

Environment=STD_BUY_ENABLED={bool_to_env(data.get('std_buy_enabled', True))}
Environment=STD_BUY_PCT={percent_to_env_decimal(data['std_buy_pct'])}
Environment=RECOVERY_BUY_REBOUND_PCT={percent_to_env_decimal(data['recovery_buy_rebound_pct'])}
Environment=PANIC_BUY_ENABLED={bool_to_env(data.get('panic_buy_enabled', True))}
Environment=PANIC_BUY_PCT={percent_to_env_decimal(data['panic_buy_pct'])}
Environment=PANIC_BUY_CONFIRM_TICKS={int(data.get('panic_buy_confirm_ticks', 2))}
Environment=CATASTROPHE_BUY_PCT={percent_to_env_decimal(data['catastrophe_buy_pct'])}

Environment=MA_FILTER_ENABLED={bool_to_env(data.get('ma_filter_enabled', True))}
Environment=MA_PERIOD={int(data.get('ma_period', 20))}
Environment=MA_SIDEWAYS_BAND_PCT={percent_to_env_decimal(data.get('ma_sideways_band_pct', 0.05))}

Environment=KILL_SWITCH={bool_to_env(data['kill_switch'])}
Environment=MAX_TRADES_PER_DAY={data['max_trades_per_day']}
Environment=DAILY_LOSS_CAP_PCT={data['daily_loss_cap_pct']}
Environment=FT_URL={data['ft_url']}

Environment=SHADOW_ENABLED={bool_to_env(data.get('shadow_enabled', False))}
Environment=SHADOW_START_EQUITY_USDC={float(data.get('shadow_start_equity_usdc', 0.0))}
"""

    return assert_dropin_safe(tpl)


def _atomic_write_text(path: str, text: str) -> None:
    """Atomikus fájlírás: félbeszakadás esetén sem marad csonka drop-in."""
    dirn = os.path.dirname(path) or "."
    base = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=dirn)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_settings_dropin(text: str) -> None:
    """
    A drop-in élesítése: ellenőrzés -> atomikus írás -> daemon-reload -> restart.

    Ha a ``daemon-reload`` vagy a ``restart`` hibázik, a korábbi tartalmat
    visszaállítjuk, hogy soha ne maradjon félig alkalmazott konfiguráció.
    A hiba nem nyelődik el: a hívó 500-as választ ad, nincs csendes siker.
    """
    assert_dropin_safe(text)

    existed = os.path.exists(SETTINGS_ENV_FILE)
    previous = None
    if existed:
        try:
            with open(SETTINGS_ENV_FILE, "r", encoding="utf-8") as f:
                previous = f.read()
        except OSError:
            previous = None

    _atomic_write_text(SETTINGS_ENV_FILE, text)

    try:
        subprocess.run(["/usr/bin/sudo", "/usr/bin/systemctl", "daemon-reload"], check=True)
        subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "uranus-runner.service"],
            check=True,
        )
    except Exception:
        try:
            if previous is not None:
                _atomic_write_text(SETTINGS_ENV_FILE, previous)
            elif not existed:
                os.unlink(SETTINGS_ENV_FILE)
            subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/systemctl", "daemon-reload"], check=False
            )
        except Exception:
            pass
        raise


def write_trading_settings(data):
    apply_settings_dropin(build_settings_dropin(data))




SERVICE_MAP = {
    "runner": "uranus-runner.service",
    "freqtrade": "uranus-freqtrade.service",
}


def _run_cmd(cmd, timeout=8):
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        msg = (r.stderr or r.stdout or "").strip()
        return r.returncode == 0, msg
    except Exception as e:
        return False, str(e)


def _systemctl_status(service: str) -> dict:
    active_ok, active_out = _run_cmd(["/usr/bin/systemctl", "is-active", service], timeout=5)
    enabled_ok, enabled_out = _run_cmd(["/usr/bin/systemctl", "is-enabled", service], timeout=5)
    show_ok, show_out = _run_cmd(
        ["/usr/bin/systemctl", "show", service, "--property=SubState,ActiveState,UnitFileState", "--no-pager"],
        timeout=5,
    )


    substate = None
    active_state = None
    unit_file_state = None

    if show_ok and show_out:
        for line in show_out.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "SubState":
                substate = v
            elif k == "ActiveState":
                active_state = v
            elif k == "UnitFileState":
                unit_file_state = v

    return {
        "service": service,
        "active": active_out.strip() if active_out else ("active" if active_ok else "unknown"),
        "enabled": enabled_out.strip() if enabled_out else ("enabled" if enabled_ok else "unknown"),
        "substate": substate,
        "active_state": active_state,
        "unit_file_state": unit_file_state,
        "checked_at_utc": utc_now_iso(),
    }


def service_statuses() -> dict:
    return {
        "runner": _systemctl_status(SERVICE_MAP["runner"]),
        "freqtrade": _systemctl_status(SERVICE_MAP["freqtrade"]),
    }


def _svc(action: str, service: str):
    return _run_cmd(["/usr/bin/sudo", "/usr/bin/systemctl", action, service], timeout=10)


def _svc_many(action: str, targets: list[str]) -> dict:
    results = {}
    ok_all = True
    for target in targets:
        service = SERVICE_MAP[target]
        ok, msg = _svc(action, service)
        results[target] = {
            "ok": ok,
            "service": service,
            "message": msg or "ok",
        }
        if not ok:
            ok_all = False
    return {"ok": ok_all, "results": results}


@app.get("/api/services")
def api_services():
    return jsonify({
        "ok": True,
        "sell_reversal_min_pct": env_decimal_to_percent(get_env("SELL_REVERSAL_MIN_PCT", 0.0)),
        "services": service_statuses(),
    })


@app.post("/ui/control/<target>/<action>")
def ui_control(target: str, action: str):
    target = (target or "").strip().lower()
    action = (action or "").strip().lower()

    if action not in {"start", "stop"}:
        return jsonify({"ok": False, "error": "invalid_action"}), 400

    if target == "all":
        result = _svc_many(action, ["runner", "freqtrade"])
    elif target in SERVICE_MAP:
        ok, msg = _svc(action, SERVICE_MAP[target])
        result = {
            "ok": ok,
            "results": {
                target: {
                    "ok": ok,
                    "service": SERVICE_MAP[target],
                    "message": msg or "ok",
                }
            }
        }
    else:
        return jsonify({"ok": False, "error": "invalid_target"}), 400

    payload = {
        "ok": result["ok"],
        "action": action,
        "target": target,
        "result": result,
        "services": service_statuses(),
    }
    return jsonify(payload), (200 if result["ok"] else 500)


@app.get("/")
def home():
    return redirect("/exchange/binance", code=302)



@app.get("/exchange/<exchange>")
def exchange_page(exchange: str):
    data = load_state_view()
    data["exchange"] = exchange.lower() if exchange else (data.get("exchange") or "binance")

    ui = {
        "time_local": local_time_str(),
        "time_utc": utc_now_iso(),
        "state_error": None,
        "poll_interval_ms": POLL_INTERVAL_MS,
    }
    return render_template("exchange_detail.html", ui=ui, data=data)


@app.get("/exchange_detail/<exchange>")
def exchange_detail_alias(exchange: str):
    return exchange_page(exchange)


@app.get("/api/state")
def api_state():
    view = load_state_view()
    return jsonify(view)


@app.get("/api/chart_history")
def api_chart_history():
    try:
        days = int(request.args.get("days", 10))
    except Exception:
        days = 10

    try:
        short_days = int(request.args.get("short_days", 1))
    except Exception:
        short_days = 1

    try:
        long_days = int(request.args.get("long_days", 10))
    except Exception:
        long_days = 10

    payload = load_chart_history_from_prices_csv(
        days=days,
        short_days=short_days,
        long_days=long_days,
    )
    status = 200 if payload.get("ok") else 500
    return jsonify(payload), status


@app.get("/api/targets")
def api_targets():
    view = load_state_view()
    levels = (view or {}).get("levels") or {}
    if not isinstance(levels, dict) or not levels:
        levels = (view.get("targets") if isinstance(view.get("targets"), dict) else {}) or levels

    return jsonify({
        "reference_base": levels.get("reference_base"),
        "std_sell": levels.get("std_sell"),
        "recovery_sell": levels.get("recovery_sell"),
        "panic_sell": levels.get("panic_sell"),
        "catastrophe_sell": levels.get("catastrophe_sell"),
        "std_buy": levels.get("std_buy"),
        "recovery_buy": levels.get("recovery_buy"),
        "panic_buy": levels.get("panic_buy"),
        "catastrophe_buy": levels.get("catastrophe_buy"),
    })


@app.route("/api/state/head", methods=["HEAD"])
def api_state_head():
    view = load_state_view()
    h = state_hash(view)
    resp = Response(status=200)
    resp.headers["ETag"] = h
    resp.headers["X-State-Hash"] = h
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Updated-Utc"] = str(view.get("updated_utc") or "")
    return resp


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(read_trading_settings())


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    try:
        data = request.get_json(force=True)

        cleaned = {
            "tick_seconds": float(data.get("tick_seconds")),
            "pair": str(data.get("pair")).strip(),
            "timeframe": str(data.get("timeframe")).strip(),
            "limit": int(float(data.get("limit"))),
            # U-2B (Patch A): fail-safe bool-értelmezés. A puszta `bool()` a
            # `"false"` STRINGET is igaznak látta volna; a `coerce_bool`
            # kizárólag explicit igaz tokenre ad True-t, és hiányzó értéknél a
            # tiltó irányú alapértelmezésre esik vissza.
            "execution_enabled": execution_policy.coerce_bool(
                data.get("execution_enabled"),
                execution_policy.EXECUTION_ENABLED_DEFAULT,
            ),
            "execution_log_only": execution_policy.coerce_bool(
                data.get("execution_log_only"),
                execution_policy.EXECUTION_LOG_ONLY_DEFAULT,
            ),
            "buy_lock_ttl_sec": int(float(data.get("buy_lock_ttl_sec"))),
            "kill_switch": bool(data.get("kill_switch")),
            "max_trades_per_day": int(float(data.get("max_trades_per_day"))),
            "daily_loss_cap_pct": float(data.get("daily_loss_cap_pct")),
            "ft_url": str(data.get("ft_url")).strip(),

            "std_sell_enabled": bool(data.get("std_sell_enabled")),
            "std_sell_pct": float(data.get("std_sell_pct")),

            "recovery_sell_retrace_pct": float(data.get("recovery_sell_retrace_pct")),
            "recovery_profit_target_pct": float(data.get("recovery_profit_target_pct")),

            "panic_sell_enabled": bool(data.get("panic_sell_enabled")),
            "panic_sell_pct": float(data.get("panic_sell_pct")),
            "sell_reversal_min_pct": float(data.get("sell_reversal_min_pct")),
            "catastrophe_sell_pct": float(data.get("catastrophe_sell_pct")),

            "std_buy_enabled": bool(data.get("std_buy_enabled")),
            "std_buy_pct": float(data.get("std_buy_pct")),

            "recovery_buy_rebound_pct": float(data.get("recovery_buy_rebound_pct")),

            "panic_buy_enabled": bool(data.get("panic_buy_enabled")),
            "panic_buy_pct": float(data.get("panic_buy_pct")),
            "panic_buy_confirm_ticks": int(float(data.get("panic_buy_confirm_ticks"))),
            "catastrophe_buy_pct": float(data.get("catastrophe_buy_pct")),

            "ma_filter_enabled": bool(data.get("ma_filter_enabled")),
            "ma_period": int(float(data.get("ma_period"))),
            "ma_sideways_band_pct": float(data.get("ma_sideways_band_pct")),
        }

        if not cleaned["pair"]:
            raise ValueError("A PAIR mező nem lehet üres.")
        if not cleaned["timeframe"]:
            raise ValueError("A TIMEFRAME mező nem lehet üres.")
        if not cleaned["ft_url"]:
            raise ValueError("Az FT_URL mező nem lehet üres.")
        if cleaned["limit"] < 1:
            raise ValueError("A LIMIT legalább 1 legyen.")
        if cleaned["tick_seconds"] <= 0:
            raise ValueError("A TICK_SECONDS legyen pozitív.")
        if cleaned["max_trades_per_day"] < 0:
            raise ValueError("A MAX_TRADES_PER_DAY nem lehet negatív.")
        if cleaned["buy_lock_ttl_sec"] < 0:
            raise ValueError("A BUY_LOCK_TTL_SEC nem lehet negatív.")

        if cleaned["panic_buy_confirm_ticks"] < 1:
            raise ValueError("A PANIC_BUY_CONFIRM_TICKS legalább 1 legyen.")
        if cleaned["ma_period"] < 1:
            raise ValueError("Az MA_PERIOD legalább 1 legyen.")
        if cleaned["ma_sideways_band_pct"] < 0:
            raise ValueError("Az MA_SIDEWAYS_BAND_PCT nem lehet negatív.")

        # U-0.1 (U0-SEC-001): fail-closed bemenet-validáció. Érvénytelen input
        # esetén innen SettingsValidationError száll, tehát nincs fájlírás,
        # nincs daemon-reload és nincs service restart.
        cleaned = validate_settings_payload(cleaned)

        write_trading_settings(cleaned)
        return jsonify({"ok": True})
    except SettingsValidationError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route("/api/shadow", methods=["GET"])
def api_get_shadow():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

    shadow = state.get("shadow") if isinstance(state.get("shadow"), dict) else {}

    # Truncate ledger to 20 newest for the API response (full ledger stays in state.json)
    ledger = shadow.get("ledger", [])
    if isinstance(ledger, list) and len(ledger) > 20:
        shadow = dict(shadow)
        shadow["ledger"] = ledger[-20:]

    return jsonify({"ok": True, "shadow": shadow})


@app.route("/api/rules", methods=["GET"])
def api_get_rules():
    return jsonify(load_rules_doc())


@app.route("/api/rules", methods=["POST"])
def api_save_rules():
    try:
        data = request.get_json(force=True)
        saved = save_rules_doc(data)
        return jsonify({"ok": True, "rules": saved})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/rules/effective", methods=["GET"])
def api_get_effective_rules():
    try:
        canonical = load_rules_doc()
        mode = (request.args.get("mode", "bot") or "bot").strip().lower()

        if mode == "lab":
            lab = load_lab_override_doc()
            effective = build_effective_rules(canonical, lab, mode="lab")
        else:
            effective = build_effective_rules(canonical, mode="bot")

        return jsonify({"ok": True, "rules": effective})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab-rules", methods=["GET"])
def api_get_lab_rules():
    return jsonify(load_lab_override_doc())


@app.route("/api/lab-rules", methods=["POST"])
def api_save_lab_rules():
    try:
        data = request.get_json(force=True)
        saved = save_lab_override_doc(data)
        return jsonify({"ok": True, "rules": saved})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lab-rules/profiles", methods=["GET"])
def api_get_lab_rule_profiles():
    try:
        return jsonify({"ok": True, "profiles": list_lab_profiles()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "profiles": []}), 500


@app.route("/rules")
@app.route("/ui/rules")
def rules_page():
    ui = {
        "time_local": local_time_str(),
        "time_utc": utc_now_iso(),
        "state_error": None,
        "poll_interval_ms": POLL_INTERVAL_MS,
    }
    return render_template("rules.html", ui=ui)


@app.route("/settings")
@app.route("/ui/settings")
def settings_page():
    ui = {
        "time_local": local_time_str(),
        "time_utc": utc_now_iso(),
        "state_error": None,
        "poll_interval_ms": POLL_INTERVAL_MS,
    }
    return render_template("settings.html", ui=ui)


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_local": local_time_str()})
