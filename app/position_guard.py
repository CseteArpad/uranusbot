import json
import subprocess
from datetime import datetime, timezone


STATE_PATH = "/opt/bots/uranus/state.json"
FT_CONFIG = "/opt/bots/uranus/freqtrade/user_data/config.json"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_position_sync(pair: str) -> None:
    try:
        # --- state.json ---
        s = json.load(open(STATE_PATH, "r", encoding="utf-8"))
        ps = ((s.get("pairs") or {}).get(pair) or {})
        state_in_position = bool(ps.get("in_position", False))

        # --- Freqtrade RPC ---
        cfg = json.load(open(FT_CONFIG, "r", encoding="utf-8"))
        api = cfg.get("api_server", {})

        u = api.get("username")
        p = api.get("password")
        host = api.get("listen_ip_address", "127.0.0.1")
        port = api.get("listen_port", 8090)

        base = f"http://{host}:{port}/api/v1"

        status_raw = subprocess.check_output(
            ["curl", "-sS", "-u", f"{u}:{p}", f"{base}/status"]
        ).decode()

        status = json.loads(status_raw)

        ft_in_position = any(t.get("pair") == pair for t in status)

        print(f"{utc_now()} [POSITION_GUARD]")
        print("  state_in_position:", state_in_position)
        print("  ft_in_position   :", ft_in_position)

        if state_in_position == ft_in_position:
            print("  RESULT: POSITION CONSISTENT ✅")
        else:
            print("  RESULT: POSITION DRIFT DETECTED ⚠️")

    except Exception as e:
        print(f"{utc_now()} [POSITION_GUARD ERROR] {type(e).__name__}: {e}")
