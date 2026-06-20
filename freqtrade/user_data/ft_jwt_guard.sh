#!/usr/bin/env bash
set -u

cd /opt/bots/uranus/freqtrade/user_data || { echo "HIBA: nincs user_data"; exit 2; }

FT_URL="${FT_URL:-http://127.0.0.1:8089}"
FT_USER="${FT_USER:-Freqtrader}"
FT_PASS="${FT_PASS:-SuperSecret1!}"

echo "FT_URL=$FT_URL"
echo "FT_USER=$FT_USER"

echo "== ping =="
curl -sS "$FT_URL/api/v1/ping" ; echo

echo "== token/login (Basic + body) =="
HTTP_CODE="$(curl -sS -u "$FT_USER:$FT_PASS" -o /tmp/ft_token.json -w "%{http_code}" -X POST "$FT_URL/api/v1/token/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$FT_USER\",\"password\":\"$FT_PASS\"}")"

echo "HTTP_CODE=$HTTP_CODE"
echo "--- BODY ---"
cat /tmp/ft_token.json ; echo
echo "-----------"

FT_ACCESS="$(python3 - <<'PY'
import json,sys
p="/tmp/ft_token.json"
try:
    d=json.load(open(p))
except Exception:
    print("")
    sys.exit(0)
print(d.get("access_token","") or d.get("access","") or "")
PY
)"

echo "ACCESS_LEN=${#FT_ACCESS}"

if [ "${#FT_ACCESS}" -lt 50 ]; then
  echo "HIBA: nincs token. (HTTP_CODE és BODY fent)"
  exit 3
fi

echo "== status (jwt) =="
curl -sS -H "Authorization: Bearer ${FT_ACCESS}" "$FT_URL/api/v1/status" ; echo

echo "== count (jwt) =="
curl -sS -H "Authorization: Bearer ${FT_ACCESS}" "$FT_URL/api/v1/count" ; echo

echo "== start (jwt) =="
curl -sS -X POST -H "Authorization: Bearer ${FT_ACCESS}" "$FT_URL/api/v1/start" ; echo

echo "== count after start =="
curl -sS -H "Authorization: Bearer ${FT_ACCESS}" "$FT_URL/api/v1/count" ; echo

echo "OK"
