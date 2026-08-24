#!/usr/bin/env bash
set -u

cd /opt/bots/uranus/freqtrade/user_data || { echo "HIBA: nincs user_data"; exit 2; }

FT_URL="${FT_URL:-http://127.0.0.1:8089}"

# U-0.1 (U0-SEC-002): nincs beegetett credential-alapertelmezes.
# Hianyzo vagy ures ertek eseten a szkript azonnal leall (fail-closed).
: "${FT_USER:?FT_USER kornyezeti valtozo kotelezo (nincs alapertelmezes)}"
: "${FT_PASS:?FT_PASS kornyezeti valtozo kotelezo (nincs alapertelmezes)}"

echo "FT_URL=$FT_URL"

echo "== ping =="
curl -sS "$FT_URL/api/v1/ping" ; echo

# A token valasz csak a futtato user szamara olvashato ideiglenes fajlba kerul,
# es a szkript vegen torlodik. A body-t NEM irjuk ki, mert JWT-t tartalmaz.
umask 077
TOKEN_FILE="$(mktemp "${TMPDIR:-/tmp}/ft_token.XXXXXXXX.json")"
trap 'rm -f "$TOKEN_FILE"' EXIT

echo "== token/login (Basic + body) =="
HTTP_CODE="$(curl -sS -u "$FT_USER:$FT_PASS" -o "$TOKEN_FILE" -w "%{http_code}" -X POST "$FT_URL/api/v1/token/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$FT_USER\",\"password\":\"$FT_PASS\"}")"

echo "HTTP_CODE=$HTTP_CODE"
echo "BODY: [redacted - token response]"

FT_ACCESS="$(TOKEN_FILE="$TOKEN_FILE" python3 - <<'PY'
import json,os,sys
p=os.environ["TOKEN_FILE"]
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
  echo "HIBA: nincs token. (lasd a HTTP_CODE erteket fent)"
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
