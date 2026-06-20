#!/usr/bin/env python3
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_TIMEOUT = 8


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _ft_base() -> str:
    return str(_env("FT_API_URL", "http://127.0.0.1:8090")).rstrip("/")


def _safe_float(x) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _split_pair(pair: str):
    if "/" in pair:
        b, q = pair.split("/", 1)
        return b.strip().upper(), q.strip().upper()
    return pair.strip().upper(), ""


def _http(method: str, url: str, headers: dict, body: bytes | None = None, timeout: int = DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = getattr(r, "status", 200)
            ctype = r.headers.get("Content-Type", "")
            raw = r.read()
        text = raw.decode("utf-8", errors="replace")
        return status, ctype, text
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode("utf-8", errors="replace")
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        return int(e.code), ctype, text


def _json_load_maybe(text: str):
    try:
        return True, json.loads(text)
    except Exception:
        return False, None


def _basic_auth_header(user: str, passwd: str) -> str:
    raw = f"{user}:{passwd}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _auth_headers(bearer: str | None = None) -> dict:
    h = {"Accept": "application/json"}
    tok = bearer or _env("FT_API_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
        return h

    u = _env("FT_API_USER")
    p = _env("FT_API_PASS")
    if u and p:
        h["Authorization"] = _basic_auth_header(str(u), str(p))
    return h


def _try_token_login() -> str | None:
    u = _env("FT_API_USER")
    p = _env("FT_API_PASS")
    if not (u and p):
        return None

    endpoints = [
        "/api/v1/token/login",
        "/api/v1/token",
        "/api/v1/login",
        "/api/v1/auth/login",
    ]
    payload = json.dumps({"username": u, "password": p}).encode("utf-8")

    for ep in endpoints:
        url = _ft_base() + ep
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        status, ctype, body = _http("POST", url, h, payload)
        is_json, parsed = _json_load_maybe(body)
        if status == 200 and is_json and isinstance(parsed, dict):
            for k in ["access_token", "token", "jwt", "bearer", "accessToken"]:
                v = parsed.get(k)
                if isinstance(v, str) and v:
                    return v
    return None


def _looks_like_balance_row(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    has_ccy = any(k in d for k in ("currency", "asset", "coin", "symbol"))
    has_amount = any(k in d for k in ("free", "available", "avail", "balance", "total", "amount", "totalBalance", "availableBalance"))
    return bool(has_ccy and has_amount)


def _walk_find_rows(obj, depth=0, max_depth=6):
    """
    Rekurzív keresés: talál-e listában/dictben olyan rekordokat, amik balansz-soroknak tűnnek.
    """
    if depth > max_depth:
        return []

    rows = []
    if isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict) and _looks_like_balance_row(it):
                rows.append(it)
            else:
                rows.extend(_walk_find_rows(it, depth + 1, max_depth))
        return rows

    if isinstance(obj, dict):
        # ha ez már direkt map: {"USDC": {...}, ...}
        if all(isinstance(k, str) for k in obj.keys()) and any(isinstance(v, dict) for v in obj.values()):
            # ez lehet közvetlen pénznem-map, visszaadjuk úgy, hogy a feldolgozó tudja kezelni
            # (nem sorok, de a _extract_balances tudja)
            return [obj]  # sentinel: egy dict-map
        for v in obj.values():
            rows.extend(_walk_find_rows(v, depth + 1, max_depth))
        return rows

    return []


def _extract_balances(parsed) -> dict:
    """
    Egységesít: {"USDC":{"free":..,"total":..}, "SOL":{...}}
    Többféle Freqtrade JSON shape + rekurzív fallback.
    """
    out = {}

    # A) közvetlen dict-map: {"USDC": {"free":..}, ...}
    if isinstance(parsed, dict):
        # tipikus kulcsok mögött listák
        for key in ["balances", "data", "result", "wallets", "currencies"]:
            v = parsed.get(key)
            if isinstance(v, list):
                parsed = v
                break
            if isinstance(v, dict) and key in ("data", "result", "wallet"):
                # tovább bontunk (pl. {"data":{"balances":[...]}})
                parsed = v
                break

        # ha még mindig dict és úgy néz ki mint pénznem-map
        if isinstance(parsed, dict):
            # eset: {"currencies": {"USDC": {...}, "SOL": {...}}}
            for key in ["currencies", "balances", "wallet", "data", "result"]:
                vv = parsed.get(key)
                if isinstance(vv, dict) and any(isinstance(x, dict) for x in vv.values()):
                    parsed = vv
                    break

        if isinstance(parsed, dict):
            # pénznem-map
            for c, v in parsed.items():
                if not isinstance(v, dict):
                    continue
                ccy = str(c).upper()
                free = v.get("free", v.get("available", v.get("avail", v.get("availableBalance", 0))))
                total = v.get("total", v.get("balance", v.get("amount", v.get("totalBalance", free))))
                if ccy and (free is not None or total is not None):
                    out[ccy] = {"free": _safe_float(free), "total": _safe_float(total)}
            if out:
                return out

    # B) lista rekordok
    if isinstance(parsed, list):
        for r in parsed:
            if not isinstance(r, dict):
                continue
            c = str(r.get("currency") or r.get("asset") or r.get("coin") or r.get("symbol") or "").upper()
            if not c:
                continue
            free = r.get("free", r.get("available", r.get("avail", r.get("availableBalance", 0))))
            total = r.get("total", r.get("balance", r.get("amount", r.get("totalBalance", free))))
            out[c] = {"free": _safe_float(free), "total": _safe_float(total)}
        if out:
            return out

    # C) rekurzív fallback: keressünk balansz-sorokat a JSON-ban
    rows = _walk_find_rows(parsed)
    if rows:
        # sentinel: ha az első "row" egy dict-map (A)-hoz hasonló
        if len(rows) == 1 and isinstance(rows[0], dict) and all(isinstance(k, str) for k in rows[0].keys()) and any(isinstance(v, dict) for v in rows[0].values()):
            for c, v in rows[0].items():
                if not isinstance(v, dict):
                    continue
                ccy = str(c).upper()
                free = v.get("free", v.get("available", v.get("avail", v.get("availableBalance", 0))))
                total = v.get("total", v.get("balance", v.get("amount", v.get("totalBalance", free))))
                out[ccy] = {"free": _safe_float(free), "total": _safe_float(total)}
            return out

        for r in rows:
            if not isinstance(r, dict):
                continue
            c = str(r.get("currency") or r.get("asset") or r.get("coin") or r.get("symbol") or "").upper()
            if not c:
                continue
            free = r.get("free", r.get("available", r.get("avail", r.get("availableBalance", 0))))
            total = r.get("total", r.get("balance", r.get("amount", r.get("totalBalance", free))))
            out[c] = {"free": _safe_float(free), "total": _safe_float(total)}
        return out

    return {}


def check_position_sync(pair: str) -> dict:
    base, quote = _split_pair(pair)

    result = {
        "ts": utc_now_iso(),
        "pair": pair,
        "base": base,
        "quote": quote,
        "ft_api_url": _ft_base(),
        "balances": {},
        "status": "ERROR",
        "error": None,
        "debug": {"attempts": []},
        "summary": {},
    }

    bearer = None
    headers = _auth_headers()

    balance_eps = [
        "/api/v1/balances",
        "/api/v1/balance",
        "/api/v1/wallets",
        "/api/v1/wallet",
    ]

    def try_balances(hdrs: dict):
        for ep in balance_eps:
            url = _ft_base() + ep
            status, ctype, body = _http("GET", url, hdrs, None)
            is_json, parsed = _json_load_maybe(body)

            attempt = {
                "endpoint": ep.lstrip("/"),
                "http_status": status,
                "content_type": ctype,
                "json": bool(is_json),
                "detail": (parsed.get("detail") if is_json and isinstance(parsed, dict) else None),
            }

            # ha JSON, tegyünk be kulcs-mintát debugba
            if is_json and isinstance(parsed, dict):
                attempt["top_keys"] = sorted(list(parsed.keys()))[:40]

            result["debug"]["attempts"].append(attempt)

            if status == 200 and is_json:
                bals = _extract_balances(parsed)
                if bals:
                    return True, bals, None
                # 200 + JSON, de nem sikerült értelmezni -> jelezzük
                return True, {}, "balances_json_but_unrecognized_shape"

            if status == 401:
                return False, {}, "unauthorized"

            if status == 404:
                continue

        return False, {}, "no_balance_endpoint_worked"

    ok, bals, err = try_balances(headers)

    if (not ok) and err == "unauthorized":
        bearer = _try_token_login()
        if bearer:
            headers2 = _auth_headers(bearer=bearer)
            ok, bals, err = try_balances(headers2)
        else:
            err = "auth_required_and_no_token_obtained"

    result["balances"] = bals

    base_free = _safe_float((bals.get(base, {}) or {}).get("free"))
    quote_free = _safe_float((bals.get(quote, {}) or {}).get("free"))

    result["summary"] = {
        "base_free": base_free,
        "quote_free": quote_free,
        "sell_ok": bool(base_free > 0.0),
        "buy_ok": bool(quote_free > 0.0),
        "note": "SELL-hez base kell. BUY-hoz quote kell.",
    }

    if ok and not err:
        if base_free <= 0.0 and quote_free <= 0.0:
            result["status"] = "EMPTY_OR_DUST"
        else:
            result["status"] = "OK"
        result["error"] = None
    else:
        result["status"] = "AUTH_REQUIRED" if (err and ("auth" in err or err == "unauthorized")) else "ERROR"
        result["error"] = err

    return result


def main():
    pair = sys.argv[1] if len(sys.argv) > 1 else "XRP/USDC"
    out = check_position_sync(pair)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
