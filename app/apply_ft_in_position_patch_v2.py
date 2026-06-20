from pathlib import Path
import re

p = Path("app.py")
txt = p.read_text(encoding="utf-8")

# ------------------------------------------------------------------
# 1) Helper function beszúrása az első _freqtrade_request után
# ------------------------------------------------------------------
helper_block = r'''
# -----------------------------------------------------------------------------
# FREQTRADE -> in_position SYNC (OPEN TRADES)
# -----------------------------------------------------------------------------
def _sync_in_position_from_freqtrade(throttle_sec: int = 10) -> None:
    try:
        now = int(time.time())
        last_sync = int(state.get("_ft_pos_last_sync_epoch") or 0)
        if now - last_sync < int(throttle_sec):
            return
        state["_ft_pos_last_sync_epoch"] = now

        r = _freqtrade_request("GET", "status")
        if r.status_code != 200:
            return

        data = r.json()
        open_count = None

        if isinstance(data, dict):
            for k in ("open_trades", "open_trade_count", "open_trades_count", "open_trades_active"):
                if k in data and isinstance(data[k], int):
                    open_count = int(data[k])
                    break

            if open_count is None:
                for v in data.values():
                    if isinstance(v, dict):
                        for k in ("open_trades", "open_trade_count", "open_trades_count"):
                            if k in v and isinstance(v[k], int):
                                open_count = int(v[k])
                                break

        if open_count is None:
            return

        state["freqtrade_open_trades"] = open_count
        state["in_position"] = open_count > 0

    except Exception:
        return
'''

if "_sync_in_position_from_freqtrade" not in txt:
    m = re.search(r"def _freqtrade_request\\(", txt)
    if not m:
        raise SystemExit("ERROR: _freqtrade_request not found")

    insert_at = txt.find("\n", m.end())
    txt = txt[:insert_at] + helper_block + txt[insert_at:]

# ------------------------------------------------------------------
# 2) Hookolás update_prices()-be RULE6 elé
# ------------------------------------------------------------------
if "_sync_in_position_from_freqtrade(" not in txt:
    m = re.search(r"_sync_rule_levels_from_prices\\(\\)", txt)
    if not m:
        raise SystemExit("ERROR: _sync_rule_levels_from_prices() call not found")

    insert_at = txt.find("\n", m.end())
    txt = txt[:insert_at] + "\n        _sync_in_position_from_freqtrade(throttle_sec=10)\n" + txt[insert_at:]

p.write_text(txt, encoding="utf-8")
print("OK: in_position sync injected (marker-free)")
