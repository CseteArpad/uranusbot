from __future__ import annotations

import os
import json
import subprocess
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, redirect, render_template

from health_bp import health_bp
from state_schema import ensure_state, validate_state
from ft_jwt_client import FreqtradeJWTClient


STATE_PATH = Path("/opt/bots/uranus/state.json")
TZ_DEFAULT = "Europe/Budapest"

SERVICE_MAP = {
    "runner": "uranus-runner.service",
    "freqtrade": "uranus-freqtrade.service",
}


def now_pair(tz_name: str = TZ_DEFAULT) -> tuple[datetime, datetime, str]:
    tz = ZoneInfo(tz_name)
    dt_utc = datetime.now(timezone.utc)
    dt_local = dt_utc.astimezone(tz)
    return dt_local, dt_utc, tz_name


def _file_mtime_iso(path: Path) -> Optional[str]:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def pick(*vals: Any) -> Optional[Any]:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return v
    return None


def load_state() -> tuple[dict, bool, list[str]]:
    raw = None
    if STATE_PATH.exists():
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    state = ensure_state(raw)
    ok, errors = validate_state(state)
    return state, ok, errors


def build_ui(exchange: str) -> dict:
    dt_local, dt_utc, tz_name = now_pair()
    return {
        "alive": True,
        "exchange": exchange,
        "ok": True,
        "time_local": dt_local.isoformat(),
        "time_utc": dt_utc.isoformat(),
        "tz": tz_name,
    }


def ft_client() -> Optional[FreqtradeJWTClient]:
    base_url = os.getenv("FT_URL", "http://127.0.0.1:8090").strip()
    user = os.getenv("FT_USERNAME", "uranus").strip()
    pwd = os.getenv("FT_PASSWORD", "").strip()

    try:
        return FreqtradeJWTClient(
            base_url=base_url,
            username=user,
            password=pwd,
            timeout=8,
        )
    except Exception:
        return None


def ft_snapshot() -> dict:
    c = ft_client()
    if not c:
        return {"ok": False, "error": "ft_client_init_failed"}

    snap = {
        "ok": True,
        "base_url": getattr(c, "base_url", None),
        "user": getattr(c, "username", None),
        "ping": None,
        "count": None,
        "status": None,
        "trader_running": None,
        "error": None,
    }

    try:
        sc, body = c.get("/api/v1/ping")
        snap["ping"] = {"status_code": sc, "body": body}
    except Exception as e:
        snap["ok"] = False
        snap["error"] = f"ping_failed: {e!s}"
        return snap

    try:
        sc, body = c.get("/api/v1/count")
        snap["count"] = {"status_code": sc, "body": body}
        snap["trader_running"] = bool(sc == 200)
    except Exception as e:
        snap["trader_running"] = False
        snap["count"] = {"status_code": 0, "body": {"error": str(e)}}

    try:
        sc, body = c.get("/api/v1/status")
        snap["status"] = {"status_code": sc, "body": body}
    except Exception as e:
        snap["status"] = {"status_code": 0, "body": {"error": str(e)}}

    return snap


def _run_cmd(cmd: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        msg = (r.stderr or r.stdout or "").strip()
        return r.returncode == 0, msg
    except Exception as e:
        return False, str(e)


def _systemctl_status(service: str) -> dict:
    active_ok, active_out = _run_cmd(["systemctl", "is-active", service], timeout=5)
    enabled_ok, enabled_out = _run_cmd(["systemctl", "is-enabled", service], timeout=5)
    show_ok, show_out = _run_cmd(
        ["systemctl", "show", service, "--property=SubState,ActiveState,UnitFileState", "--no-pager"],
        timeout=5,
    )

    substate = None
    active_state = None
    unit_file_state = None

    if show_ok and show_out:
        for line in show_out.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "SubState":
                substate = v
            elif k == "ActiveState":
                active_state = v
            elif k == "UnitFileState":
                unit_file_state = v

    return {
        "service": service,
        "active": active_out.strip() if active_out else ("active" if active_ok else "unknown"),
        "enabled": enabled_out.strip() if enabled_out else ("enabled" if enabled_ok else "unknown"),
        "substate": substate,
        "active_state": active_state,
        "unit_file_state": unit_file_state,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def service_statuses() -> dict:
    return {
        "runner": _systemctl_status(SERVICE_MAP["runner"]),
        "freqtrade": _systemctl_status(SERVICE_MAP["freqtrade"]),
    }


def _svc(action: str, service: str) -> tuple[bool, str]:
    return _run_cmd(["sudo", "systemctl", action, service], timeout=10)


def _svc_many(action: str, targets: list[str]) -> dict:
    results = {}
    ok_all = True
    for target in targets:
        service = SERVICE_MAP[target]
        ok, msg = _svc(action, service)
        results[target] = {
            "ok": ok,
            "service": service,
            "message": msg or "ok",
        }
        if not ok:
            ok_all = False
    return {"ok": ok_all, "results": results}


def build_payload(exchange: str) -> dict:
    state, state_ok, state_errors = load_state()
    ui = build_ui(exchange)
    services = service_statuses()
    ft = ft_snapshot()

    return {
        "ok": True,
        "ui": ui,
        "state": state,
        "ft": ft,
        "services": services,
        "state_ok": state_ok,
        "state_errors": state_errors,
        "state_path": str(STATE_PATH),
        "time_local": ui["time_local"],
        "time_utc": ui["time_utc"],
        "tz": ui["tz"],
    }


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(health_bp)

    @app.get("/")
    def index():
        return redirect("/exchange/binance", code=302)

    @app.get("/exchange/<exchange>")
    def exchange_page(exchange: str):
        payload = build_payload(exchange)
        return render_template(
            "exchange_detail.html",
            exchange=exchange,
            state=payload["state"],
            services=payload["services"],
            ft=payload["ft"],
            ui=payload["ui"],
        )

    @app.get("/ui/exchange/<exchange>")
    def ui_exchange(exchange: str):
        return redirect(f"/exchange/{exchange.lower()}", code=302)

    @app.get("/api/exchange/<exchange>")
    def api_exchange(exchange: str):
        return jsonify(build_payload(exchange))

    @app.get("/api/state")
    def api_state():
        state, ok, errors = load_state()
        return jsonify({
            "ok": ok,
            "errors": errors,
            "state": state,
            "mtime_utc": _file_mtime_iso(STATE_PATH),
        })

    @app.get("/api/state/head")
    def api_state_head():
        state, ok, errors = load_state()
        meta = state.get("meta", {}) or {}
        ftm = meta.get("ft", {}) or {}

        return jsonify({
            "ok": True,
            "state_ok": ok,
            "state_errors": errors[:10],
            "meta": {
                "created_utc": meta.get("created_utc"),
                "updated_utc": meta.get("updated_utc"),
                "schema_version": state.get("schema_version"),
                "ft": {
                    "base_url": ftm.get("base_url"),
                    "synced_utc": ftm.get("synced_utc"),
                    "heartbeat_utc": ftm.get("heartbeat_utc"),
                },
                "state_mtime_utc": _file_mtime_iso(STATE_PATH),
            },
        })

    @app.get("/api/ft/snapshot")
    def api_ft_snapshot():
        return jsonify(ft_snapshot())

    @app.get("/api/services")
    def api_services():
        return jsonify({
            "ok": True,
            "services": service_statuses(),
        })

    @app.post("/ui/control/<target>/<action>")
    def ui_control(target: str, action: str):
        target = (target or "").strip().lower()
        action = (action or "").strip().lower()

        if action not in {"start", "stop"}:
            return jsonify({"ok": False, "error": "invalid_action"}), 400

        if target == "all":
            result = _svc_many(action, ["runner", "freqtrade"])
        elif target in SERVICE_MAP:
            ok, msg = _svc(action, SERVICE_MAP[target])
            result = {
                "ok": ok,
                "results": {
                    target: {
                        "ok": ok,
                        "service": SERVICE_MAP[target],
                        "message": msg or "ok",
                    }
                }
            }
        else:
            return jsonify({"ok": False, "error": "invalid_target"}), 400

        payload = {
            "ok": result["ok"],
            "action": action,
            "target": target,
            "result": result,
            "services": service_statuses(),
        }
        return jsonify(payload), (200 if result["ok"] else 500)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8016, debug=False)
