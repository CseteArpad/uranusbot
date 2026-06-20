from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify

TZ_DEFAULT = "Europe/Budapest"

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health():
    """
    Hard rule: /health must never raise, must not touch state.json, must not call external services.
    """
    tz = TZ_DEFAULT
    try:
        now_local = datetime.now(ZoneInfo(tz)).isoformat()
    except Exception:
        tz = "UTC"
        now_local = datetime.now(timezone.utc).isoformat()

    return jsonify(
        ui={
            "exchange": "Binance",
            "ok": True,
            "time_local": now_local,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "tz": tz,
        }
    ), 200
