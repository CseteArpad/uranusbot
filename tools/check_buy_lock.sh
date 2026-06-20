#!/usr/bin/env bash
set -euo pipefail

STATE="/opt/bots/uranus/state.json"

echo "== service env =="
systemctl show uranus-runner.service -p Environment --no-pager | sed 's/^Environment=//'

echo
echo "== drop-ins loaded =="
systemctl status uranus-runner.service --no-pager -l | sed -n '1,35p'

echo
echo "== state locks =="
python3 - <<'PY'
import json
p="/opt/bots/uranus/state.json"
s=json.load(open(p,"r",encoding="utf-8"))
locks=s.get("locks")
print("locks:", locks)
print("has_locks_dict:", isinstance(locks, dict))
print("buy_pending_in_locks:", isinstance(locks, dict) and ("buy_pending" in locks))
print("buy_in_locks:", isinstance(locks, dict) and ("buy" in locks))
PY

echo
echo "== state.json contains 'buy_pending'? =="
grep -n "buy_pending" /opt/bots/uranus/state.json || true

echo
echo "== log grep (last 400) =="
journalctl -u uranus-runner.service -n 400 --no-pager \
  | grep -E "BUY_LOCK_ACTIVE|BUY_LOCK|buy_pending|EXEC_LOG_ONLY|decision=BUY" \
  | tail -n 120 || true
