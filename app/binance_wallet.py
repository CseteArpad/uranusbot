#!/usr/bin/env python3
"""
Uranus – Binance spot wallet olvasó, **KIVEZETVE** (tulajdonosi döntés, 2026-09-01).

Ez a modul az éles Binance API-kulcsot olvasta ki a Freqtrade ``config.json``-ból,
és aláírt ``GET /api/v3/account`` kérést küldött. Bár csak olvasott, az általa
használt kulcs **kereskedési jogosultságú** volt, és a `config.json`-t
credential-forrásként kezelte – pontosan az a minta, amit a
``URANUS_EXECUTION_VENUE_POLICY = OKX_SPOT_ONLY`` döntés megszüntet.

A modul importálható marad, és a publikus API alakja változatlan, de a
hitelesítési útra **nem lép rá**: ha a config helyszíne kivezetett (Binance),
azonnal ``VENUE_RETIRED`` státusszal tér vissza, anélkül hogy a kulcsot
egyáltalán beolvasná.

Hívói viselkedés változatlan: az egyetlen hívó (``shadow_position.py`` „Step 2”)
kizárólag ``status == "OK"`` esetén használja az eredményt, tehát ez a modul a
korábbi hibaágra esik – új hibaosztály nem keletkezik.
"""
import hmac
import hashlib
import json
import time
import urllib.parse
import urllib.request

try:  # package import (pytest, repo gyökér a sys.path-on)
    from app import execution_venue_policy  # type: ignore
except ImportError:  # flat import (production runner)
    import execution_venue_policy  # type: ignore

BINANCE_API_BASE = "https://api.binance.com"
DEFAULT_TIMEOUT = 8

def _read_ft_keys(cfg_path: str) -> tuple[str, str]:
    c = json.load(open(cfg_path, "r", encoding="utf-8"))
    ex = c.get("exchange") or {}
    key = str(ex.get("key") or "")
    sec = str(ex.get("secret") or "")
    return key, sec

def _signed_get(path: str, api_key: str, api_secret: str, params: dict | None = None) -> tuple[int, str]:
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_API_BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            status = getattr(r, "status", 200)
            body = r.read().decode("utf-8", errors="replace")
        return int(status), body
    except Exception as e:
        return 0, json.dumps({"error": type(e).__name__, "detail": str(e)})

def _venue_is_retired(cfg_path: str) -> bool:
    """
    Igaz, ha a config helyszíne kivezetett – ilyenkor a kulcsot NEM olvassuk be.

    Fail-closed: ha a config nem olvasható vagy a helyszín nem állapítható meg,
    szintén kivezetettként kezeljük. Egy olvashatatlan configból nem szabad
    hitelesítési útra lépni.
    """
    try:
        with open(cfg_path, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except Exception:
        return True
    exchange = cfg.get("exchange") if isinstance(cfg, dict) else None
    name = exchange.get("name") if isinstance(exchange, dict) else None
    return not execution_venue_policy.is_allowed_live_venue(name)


def get_spot_balances_from_freqtrade_config(cfg_path: str) -> dict:
    if _venue_is_retired(cfg_path):
        return {
            "status": "VENUE_RETIRED",
            "balances": {},
            "error": "binance_wallet_retired:URANUS_EXECUTION_VENUE_POLICY=OKX_SPOT_ONLY",
        }

    api_key, api_secret = _read_ft_keys(cfg_path)
    if not api_key or not api_secret:
        return {"status": "NO_KEYS", "balances": {}, "error": "missing_exchange_key_or_secret"}
    status, body = _signed_get("/api/v3/account", api_key, api_secret, params={"recvWindow": 5000})
    if status != 200:
        try:
            j = json.loads(body)
        except Exception:
            j = {"detail": body[:500]}
        return {"status": "ERROR", "balances": {}, "http_status": status, "error": j}
    j = json.loads(body)
    out = {}
    for row in j.get("balances") or []:
        a = str(row.get("asset") or "").upper()
        if not a:
            continue
        free = float(row.get("free") or 0.0)
        locked = float(row.get("locked") or 0.0)
        total = free + locked
        if total > 0:
            out[a] = {"free": free, "locked": locked, "total": total}
    return {"status": "OK", "balances": out, "http_status": 200}
