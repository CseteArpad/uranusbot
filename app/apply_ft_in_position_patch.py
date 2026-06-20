from pathlib import Path
import re

p = Path("app.py")
txt = p.read_text(encoding="utf-8")

# --- 1) Insert helper function (once) ---
marker = "# ----------------------------------------------------------------------------\n# FREQTRADE API (Token handling + auto refresh)\n# ----------------------------------------------------------------------------"
if marker not in txt:
    raise SystemExit("ERROR: marker not found. app.py structure unexpected.")

insert_block = r'''
# -----------------------------------------------------------------------------
# FREQTRADE -> in_position SYNC (OPEN TRADES)
# -----------------------------------------------------------------------------
def _sync_in_position_from_freqtrade(throttle_sec: int = 10) -> None:
    """
    Sets:
      - state["in_position"] based on open trades
      - state["freqtrade_open_trades"] count
    Throttled to reduce API load.
    """
    try:
        now = int(time.time())
        last_sync = int(state.get("_ft_pos_last_sync_epoch") or 0)
        if now - last_sync < int(throttle_sec):
            return
        state["_ft_pos_last_sync_epoch"] = now

        # Prefer /status (stable endpoint)
        r = _freqtrade_request("GET", "status")
        if r.status_code != 200:
            return
        data = r.json()

        open_count = None

        # Heuristics for status payloads
        if isinstance(data, dict):
            for k in ("open_trades", "open_trade_count", "open_trades_count", "open_trades_active"):
                if k in data and isinstance(data[k], int):
                    open_count = int(data[k])
                    break
            if open_count is None and "status" in data and isinstance(data["status"], dict):
                st = data["status"]
                for k in ("open_trades", "open_trade_count", "open_trades_count"):
                    if k in st and isinstance(st[k], int):
                        open_count = int(st[k])
                        break
            if open_count is None and "data" in data and isinstance(data["data"], dict):
                st = data["data"]
                for k in ("open_trades", "open_trade_count", "open_trades_count"):
                    if k in st and isinstance(st[k], int):
                        open_count = int(st[k])
                        break

        # Fallback: try /trades or /trades/open if available on your version
        if open_count is None:
            for ep in ("trades/open", "trades"):
                try:
                    rr = _freqtrade_request("GET", ep)
                    if rr.status_code != 200:
                        continue
                    td = rr.json()
                    if isinstance(td, list):
                        # count "open-ish" trades
                        def is_open(t):
                            if not isinstance(t, dict):
                                return False
                            if t.get("is_open") is True:
                                return True
                            if t.get("open") is True:
                                return True
                            if t.get("close_date") in (None, "", 0):
                                return True
                            if t.get("close_profit") is None and t.get("close_rate") is None:
                                # common sign of open trade records
                                return True
                            return False
                        open_count = sum(1 for t in td if is_open(t))
                        break
                    if isinstance(td, dict) and "trades" in td and isinstance(td["trades"], list):
                        open_count = sum(1 for t in td["trades"] if isinstance(t, dict) and (t.get("is_open") is True or t.get("open") is True))
                        break
                except Exception:
                    continue

        if open_count is None:
            return

        state["freqtrade_open_trades"] = int(open_count)
        state["in_position"] = bool(open_count > 0)

    except Exception:
        # no hard fail – UI must keep working
        return
'''

if "_sync_in_position_from_freqtrade(" not in txt:
    txt = txt.replace(marker, insert_block + "\n" + marker, 1)

# --- 2) Call sync from update_prices() before RULE6 evaluation ---
# We insert it right after _sync_rule_levels_from_prices() call.
upd_pat = re.compile(r'(\n\s*_sync_rule_levels_from_prices\(\)\s*\n)', re.M)
if "_sync_in_position_from_freqtrade(" not in txt:
    m = upd_pat.search(txt)
    if not m:
        raise SystemExit("ERROR: could not find _sync_rule_levels_from_prices() call in update_prices().")
    insert_call = m.group(1) + "\n        _sync_in_position_from_freqtrade(throttle_sec=10)\n"
    txt = txt[:m.start(1)] + insert_call + txt[m.end(1):]

p.write_text(txt, encoding="utf-8")
print("OK: inserted _sync_in_position_from_freqtrade() and hooked into update_prices()")
