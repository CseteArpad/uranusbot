from flask import Blueprint, jsonify, render_template
import os, base64, json, urllib.request
from urllib.error import HTTPError, URLError

bp = Blueprint("dashboard", __name__)

def _api_base():
    base = os.environ.get("FREQTRADE_RPC_URL", "http://127.0.0.1:8090").rstrip("/")
    return base + "/api/v1"

class DashboardConfigError(RuntimeError):
    """Hiányzó kötelező konfiguráció – fail-closed, nincs beégetett fallback."""


def _auth_header():
    # U-0.1 (U0-SEC-002): a credential kizárólag környezeti változóból jöhet.
    # Beégetett alapértelmezés nincs; hiány esetén a hívás elbukik.
    user = os.environ.get("FREQTRADE_RPC_USERNAME", "")
    pw = os.environ.get("FREQTRADE_RPC_PASSWORD", "")
    if not user or not pw:
        raise DashboardConfigError(
            "FREQTRADE_RPC_USERNAME / FREQTRADE_RPC_PASSWORD nincs beállítva "
            "(kötelező, alapértelmezett érték nincs)"
        )
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return "Basic " + token

def ft_get(path: str):
    url = _api_base() + "/" + path.lstrip("/")
    req = urllib.request.Request(url)
    req.add_header("Authorization", _auth_header())
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def ft_get_safe(path: str):
    try:
        return ft_get(path), None
    except HTTPError as e:
        # 404/401 stb.
        return None, f"HTTP {e.code} on {path}"
    except URLError as e:
        return None, f"URL error on {path}: {e}"
    except Exception as e:
        return None, f"Error on {path}: {e}"

def _as_list(payload):
    if payload is None:
        return []
    if isinstance(payload, dict):
        v = payload.get("trades", payload.get("data", payload))
        if isinstance(v, list):
            return v
        # néha dictben jön 1 trade
        return [v] if isinstance(v, dict) else []
    if isinstance(payload, list):
        return payload
    return []

def _detect_open_trades():
    # 1) próbálunk tipikus endpointokat
    candidates = [
        "trades/open",
        "open_trades",
        "trades?open_only=true",
        "trades?is_open=true",
    ]
    last_err = None
    for c in candidates:
        data, err = ft_get_safe(c)
        if err:
            last_err = err
            continue
        lst = _as_list(data)
        # ha üres lista is jöhet, attól még valid endpoint
        return lst, None

    # 2) fallback: lekérjük a trade listát és szűrünk (ha van is_open mező)
    data, err = ft_get_safe("trades?limit=50")
    if not err:
        lst = _as_list(data)
        open_lst = []
        for t in lst:
            if isinstance(t, dict):
                if t.get("is_open") is True:
                    open_lst.append(t)
                # egyes verziókban close_date None jelzi a nyitottat
                elif t.get("close_date") in (None, "", "null"):
                    # csak akkor, ha van open_date (különben summary sor is lehet)
                    if t.get("open_date") or t.get("open_timestamp"):
                        open_lst.append(t)
        return open_lst, None

    return [], (last_err or err or "No open trades endpoint found")

def summary():
    open_list, open_err = _detect_open_trades()

    # utolsó trade
    trades, trades_err = ft_get_safe("trades?limit=1")
    trades_list = _as_list(trades)
    last_trade = trades_list[0] if trades_list else None

    # profit
    profit, profit_err = ft_get_safe("profit")

    return {
        "open_count": len(open_list),
        "open_trades": open_list,
        "last_trade": last_trade,
        "profit": profit if profit is not None else {"error": profit_err},
        "errors": {
            "open_trades": open_err,
            "last_trade": trades_err,
            "profit": profit_err,
        }
    }

@bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", data=summary())

@bp.route("/api/dashboard")
def dashboard_api():
    return jsonify(summary())
