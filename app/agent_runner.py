#!/usr/bin/env python3
import os, json, time, datetime, traceback
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

STATE_PATH = os.getenv("URANUS_STATE_PATH", "/opt/bots/uranus/state.json")
LOG_PATH   = os.getenv("URANUS_AGENT_LOG", "/opt/bots/uranus/logs/agent.log")

FT_URL     = os.getenv("URANUS_FT_URL", "http://127.0.0.1:8090")

# U-0.1 (U0-SEC-002): nincs beégetett credential-alapértelmezés.
# Hiányzó érték esetén a runner fail-closed módon leáll (lásd _require_credentials).
FT_USER    = os.getenv("URANUS_FT_USER", "")
FT_PASS    = os.getenv("URANUS_FT_PASS", "")


class MissingCredentialError(RuntimeError):
    """Kötelező credential hiányzik – fail-closed."""


def _require_credentials():
    if not FT_USER or not FT_PASS:
        raise MissingCredentialError(
            "URANUS_FT_USER / URANUS_FT_PASS nincs beállítva "
            "(kötelező, alapértelmezett érték nincs)"
        )

TICK_SEC   = int(os.getenv("URANUS_TICK_INTERVAL", "10"))

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def ensure_dir_for(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def log(line: str):
    ensure_dir_for(LOG_PATH)
    ts = now_iso()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{ts} {line}\n")

def read_state():
    if not os.path.exists(STATE_PATH):
        return {"schema_version": 1}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def write_state(s):
    ensure_dir_for(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STATE_PATH)

def http_json(url: str):
    _require_credentials()
    req = Request(url, method="GET")
    import base64
    token = base64.b64encode(f"{FT_USER}:{FT_PASS}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=5) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data) if data else {}

def ft_health():
    for path in ("/api/v1/ping", "/ping", "/api/v1/status", "/api/v1/version"):
        try:
            return True, path, http_json(FT_URL + path)
        except Exception:
            pass
    return False, None, None

def main():
    # Fail-closed indulás: hiányzó credential mellett el sem indulunk, hogy a
    # hiba egyértelmű legyen és ne gyenge auth-tal próbálkozzunk körben.
    try:
        _require_credentials()
    except MissingCredentialError as e:
        log(f"FATAL config: {e}")
        raise SystemExit(2)

    log("agent_runner start")
    while True:
        try:
            s = read_state()
            if not isinstance(s, dict):
                s = {"schema_version": 1}

            ok, ep, payload = ft_health()

            s.setdefault("schema_version", 1)
            s.setdefault("agent", {})
            s["agent"]["updated_at"] = now_iso()
            s["agent"]["tick_sec"] = TICK_SEC
            s["agent"]["ft_url"] = FT_URL
            s["agent"]["ft_ok"] = bool(ok)
            s["agent"]["ft_endpoint"] = ep
            s["agent"]["ft_payload"] = payload if isinstance(payload, dict) else None

            write_state(s)
            log(f"tick ft_ok={s['agent']['ft_ok']} endpoint={s['agent']['ft_endpoint']}")
        except (HTTPError, URLError) as e:
            log(f"ERROR http: {repr(e)}")
        except Exception:
            log("ERROR exception: " + traceback.format_exc())

        time.sleep(TICK_SEC)

if __name__ == "__main__":
    main()
