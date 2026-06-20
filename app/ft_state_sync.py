from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from state_schema import ensure_state, validate_state
from ft_jwt_client import FreqtradeJWTClient

STATE_PATH = Path("/opt/bots/uranus/state.json")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_state() -> Dict[str, Any]:
    raw = None
    if STATE_PATH.exists():
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return ensure_state(raw)


def _strip_ts(ft: Dict[str, Any]) -> Dict[str, Any]:
    """Összehasonlításhoz kivesszük a csak időbélyeg mezőket."""
    out = dict(ft or {})
    out.pop("synced_utc", None)
    out.pop("heartbeat_utc", None)
    out.pop("last_poll_utc", None)
    return out


def _should_heartbeat_write(state: Dict[str, Any], heartbeat_sec: int) -> bool:
    """Ha régebbi a heartbeat_utc mint heartbeat_sec, akkor írunk egy 'heartbeat' frissítést."""
    if heartbeat_sec <= 0:
        return False

    meta = state.get("meta") or {}
    ft = (meta.get("ft") or {}) if isinstance(meta, dict) else {}
    hb = ft.get("heartbeat_utc")

    if not hb:
        return True

    try:
        last = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age >= heartbeat_sec
    except Exception:
        return True


def fetch_ft_snapshot(client: FreqtradeJWTClient) -> Dict[str, Any]:
    ping_code, ping_body = client.get("/api/v1/ping")
    status_code, status_body = client.get("/api/v1/status")
    count_code, count_body = client.get("/api/v1/count")

    return {
        "base_url": client.base_url,
        "ping": {"status_code": ping_code, "body": ping_body},
        "status": {"status_code": status_code, "body": status_body},
        "count": {"status_code": count_code, "body": count_body},
        "error": None,
        "ok": True,
    }


def merge_ft_into_state(state: Dict[str, Any], ft_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    meta = state.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    ft = meta.get("ft") or {}
    if not isinstance(ft, dict):
        ft = {}

    # mindig rögzítjük a legutóbbi poll-t (de ez önmagában nem kötelező write)
    ft["last_poll_utc"] = utcnow_iso()

    # snapshot beemelése
    for k, v in ft_snapshot.items():
        ft[k] = v

    meta["ft"] = ft
    state["meta"] = meta
    return state


def decide_write(old_state: Dict[str, Any], new_state: Dict[str, Any], heartbeat_sec: int) -> Tuple[bool, bool]:
    """
    returns: (should_write, data_changed)
    - data_changed: FT snapshot érdemi része változott (timestamp mezők nélkül)
    - should_write: data_changed VAGY heartbeat write esedékes
    """
    old_meta = old_state.get("meta") or {}
    new_meta = new_state.get("meta") or {}
    old_ft = (old_meta.get("ft") or {}) if isinstance(old_meta, dict) else {}
    new_ft = (new_meta.get("ft") or {}) if isinstance(new_meta, dict) else {}

    data_changed = _strip_ts(old_ft) != _strip_ts(new_ft)
    hb_due = _should_heartbeat_write(old_state, heartbeat_sec)

    should_write = data_changed or hb_due
    return should_write, data_changed


def sync_once(client: FreqtradeJWTClient, interval_sec: int, heartbeat_sec: int) -> None:
    old_state = load_state()

    ft_snapshot = fetch_ft_snapshot(client)
    state = json.loads(json.dumps(old_state))  # deep copy (egyszerű, determinisztikus)

    state = merge_ft_into_state(state, ft_snapshot)

    should_write, data_changed = decide_write(old_state, state, heartbeat_sec)

    if not should_write:
        # semmi érdemi változás és heartbeat sem esedékes → nem írunk fájlt
        return

    # ha írunk: jelöljük, miért
    meta = state.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    ft = meta.get("ft") or {}
    if not isinstance(ft, dict):
        ft = {}

    now = utcnow_iso()

    if data_changed:
        ft["synced_utc"] = now  # csak érdemi változáskor frissül
    else:
        # heartbeat write (UI frissítési trigger)
        ft["heartbeat_utc"] = now

    meta["updated_utc"] = now
    meta["ft"] = ft
    state["meta"] = meta

    ok, errors = validate_state(state)
    if not ok:
        raise RuntimeError(f"state invalid: {errors[:5]}")

    atomic_write_json(STATE_PATH, state)


def main() -> int:
    base_url = os.environ.get("FT_URL", "http://127.0.0.1:8090").rstrip("/")
    user = os.environ.get("FT_USER", "Freqtrade")
    pw = os.environ.get("FT_PASS", "")
    interval_sec = int(os.environ.get("FT_SYNC_INTERVAL", "10"))
    heartbeat_sec = int(os.environ.get("FT_HEARTBEAT_SEC", "15"))  # UI refresh trigger (write) max 15s-onként

    client = FreqtradeJWTClient(base_url=base_url, username=user, password=pw)

    while True:
        try:
            sync_once(client, interval_sec=interval_sec, heartbeat_sec=heartbeat_sec)
        except Exception as e:
            # logolunk, de nem állunk le
            print(f"[ft_state_sync] ERROR: {e}")
        time.sleep(max(1, interval_sec))


if __name__ == "__main__":
    raise SystemExit(main())
