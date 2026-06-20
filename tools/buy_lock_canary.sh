#!/usr/bin/env bash
set -euo pipefail

SERVICE="uranus-runner"
STATE="/opt/bots/uranus/state.json"

echo "=== BUY_LOCK CANARY ==="
echo "ts_utc: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo

echo "== systemd status (short) =="
sudo systemctl is-active "$SERVICE" --quiet && echo "active: yes" || echo "active: no"
echo "unit: $(systemctl show -p FragmentPath --value "$SERVICE" 2>/dev/null || true)"
echo "dropins:"
systemctl show -p DropInPaths --value "$SERVICE" 2>/dev/null | tr ' ' '\n' | sed '/^$/d' || true
echo

PID="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo 0)"
echo "== PID =="
echo "PID=$PID"
if [[ "$PID" == "0" || -z "$PID" ]]; then
  echo "ERROR: MainPID is 0. Service not running?"
  exit 1
fi
echo

echo "== process cmd/cwd =="
sudo ps -p "$PID" -o pid,etimes,cmd --no-headers || true
echo -n "cwd: "
sudo readlink -f "/proc/$PID/cwd" || true
echo

echo "== ENV (runner process) =="
sudo tr '\0' '\n' < "/proc/$PID/environ" \
  | egrep '^(BUY_LOCK_TTL_SEC|EXECUTION_ENABLED|EXECUTION_LOG_ONLY|TICK_SECONDS|PAIR|TIMEFRAME|LIMIT|STATE_PATH|FT_URL)=' \
  | sort || true
echo

echo "== unit env lines (effective) =="
sudo systemctl cat "$SERVICE" --no-pager \
  | egrep -n '^\s*Environment=|^\s*EnvironmentFile=|^\s*\[Service\]|\.(conf)' || true
echo

echo "== state.json (decision + buy_pending) =="
if [[ -f "$STATE" ]]; then
  python3 - <<'PY'
import json, time
p="/opt/bots/uranus/state.json"
try:
    s=json.load(open(p,"r",encoding="utf-8"))
except Exception as e:
    print("ERROR reading state.json:", type(e).__name__, str(e))
    raise SystemExit(0)

d=s.get("decision",{}) if isinstance(s,dict) else {}
locks=s.get("locks",{}) if isinstance(s,dict) else {}
bp=locks.get("buy_pending") if isinstance(locks,dict) else None

print("decision.action:", d.get("action"))
print("decision.rule:", d.get("rule"))
print("decision.reason:", d.get("reason"))
print("locks.keys:", list(locks.keys()) if isinstance(locks,dict) else locks)

if isinstance(bp,dict):
    now=int(time.time())
    ts=bp.get("ts")
    age=(now-ts) if isinstance(ts,int) else None
    print("buy_pending.ts:", ts)
    print("buy_pending.age_sec:", age)
    print("buy_pending.pair:", bp.get("pair"))
    print("buy_pending.timeframe:", bp.get("timeframe"))
    print("buy_pending.last:", bp.get("last"))
    print("buy_pending.prev_last:", bp.get("prev_last"))
    print("buy_pending.rule:", bp.get("rule"))
    print("buy_pending.reason:", bp.get("reason"))
else:
    print("buy_pending: None")
PY
else
  echo "ERROR: state.json not found at $STATE"
fi
echo

echo "== journald (last 120 lines, filtered) =="
sudo journalctl -u "$SERVICE" -n 120 --no-pager \
  | egrep -i 'decision=BUY|BUY_LOCK_ACTIVE|EXEC_LOG_ONLY|EXEC_DISABLED|ERROR|EXC' || true
echo

echo "=== END CANARY ==="
