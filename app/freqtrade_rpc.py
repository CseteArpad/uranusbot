# /opt/bots/uranus/app/freqtrade_rpc.py
# Read-only Freqtrade RPC (API Server) kliens Flask UI-hoz
#
# Várható endpointok (Freqtrade):
#   GET /api/v1/ping
#   GET /api/v1/version
#   GET /api/v1/show_config
#   GET /api/v1/status
#   GET /api/v1/trades?limit=50
#
# Auth: Basic (api_server.username/password)
# HTTPS nélkül (localhost), de támogatja a https-t is.

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth


@dataclass
class RpcResult:
    ok: bool
    http_status: Optional[int]
    latency_ms: Optional[int]
    data: Optional[Dict[str, Any]]
    error: Optional[str]


class FreqtradeRPCClient:
    """
    Read-only RPC kliens Freqtrade API Serverhez.

    Környezeti változók (opcionális):
      FREQTRADE_RPC_URL        pl. http://127.0.0.1:8090
      FREQTRADE_RPC_USERNAME   pl. deploy
      FREQTRADE_RPC_PASSWORD   pl. ********
      FREQTRADE_RPC_VERIFY_TLS 0/1 (https esetén)
      FREQTRADE_RPC_TIMEOUT_S  pl. 3.5
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_tls: Optional[bool] = None,
        timeout_s: Optional[float] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or os.getenv("FREQTRADE_RPC_URL", "http://127.0.0.1:8090")).rstrip("/")
        self.username = username or os.getenv("FREQTRADE_RPC_USERNAME", "")
        self.password = password or os.getenv("FREQTRADE_RPC_PASSWORD", "")
        self.verify_tls = verify_tls if verify_tls is not None else (os.getenv("FREQTRADE_RPC_VERIFY_TLS", "1") != "0")
        self.timeout_s = timeout_s if timeout_s is not None else float(os.getenv("FREQTRADE_RPC_TIMEOUT_S", "3.5"))
        self._s = session or requests.Session()

    # ---------------------------
    # Low-level helpers
    # ---------------------------

    def _auth(self) -> Optional[HTTPBasicAuth]:
        if self.username and self.password:
            return HTTPBasicAuth(self.username, self.password)
        return None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> RpcResult:
        url = f"{self.base_url}{path}"
        t0 = time.perf_counter()
        try:
            r = self._s.get(
                url,
                params=params,
                auth=self._auth(),
                timeout=self.timeout_s,
                verify=self.verify_tls,
                headers={"Accept": "application/json"},
            )
            latency_ms = int((time.perf_counter() - t0) * 1000)

            # próbáljunk json-t olvasni akkor is, ha hibás a status
            data = None
            err = None
            try:
                data = r.json()
            except Exception:
                data = None

            if 200 <= r.status_code < 300:
                return RpcResult(ok=True, http_status=r.status_code, latency_ms=latency_ms, data=data, error=None)

            # hiba eset: próbáljunk értelmes üzenetet
            if isinstance(data, dict):
                err = data.get("error") or data.get("message") or str(data)
            else:
                err = f"HTTP {r.status_code}"
            return RpcResult(ok=False, http_status=r.status_code, latency_ms=latency_ms, data=data, error=err)

        except requests.exceptions.RequestException as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            return RpcResult(ok=False, http_status=None, latency_ms=latency_ms, data=None, error=str(e))

    # ---------------------------
    # Public API (read-only)
    # ---------------------------

    def ping(self) -> RpcResult:
        return self._get("/api/v1/ping")

    def version(self) -> RpcResult:
        return self._get("/api/v1/version")

    def show_config(self) -> RpcResult:
        return self._get("/api/v1/show_config")

    def status(self) -> RpcResult:
        return self._get("/api/v1/status")

    def trades(self, limit: int = 50, offset: int = 0) -> RpcResult:
        # Egyes verziók /trades?limit=..&offset=.. paramokat várnak
        return self._get("/api/v1/trades", params={"limit": limit, "offset": offset})

    def open_trades(self, limit: int = 50) -> RpcResult:
        # Bizonyos verziókban van is_open param (ha nincs, a hívó oldalon szűrünk)
        return self._get("/api/v1/trades", params={"limit": limit, "offset": 0, "is_open": "true"})

    # ---------------------------
    # Convenience: one-shot snapshot
    # ---------------------------

    def snapshot(self) -> Dict[str, Any]:
        """
        Egyetlen hívással jól használható UI snapshot.
        Nem dob kivételt, mindent eredményként ad vissza.
        """
        out: Dict[str, Any] = {
            "rpc_url": self.base_url,
            "timestamp_utc": int(time.time()),
            "ping": None,
            "version": None,
            "show_config": None,
            "status": None,
            "trades": None,
        }

        out["ping"] = self.ping().__dict__
        out["version"] = self.version().__dict__
        out["show_config"] = self.show_config().__dict__
        out["status"] = self.status().__dict__
        out["trades"] = self.trades(limit=50).__dict__
        return out
