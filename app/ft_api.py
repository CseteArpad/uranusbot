import time
import json
import base64
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests


def _jwt_payload(token: str) -> Dict[str, Any]:
    """
    JWT payload decode WITHOUT signature validation (only for exp reading).
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1] + "==="
        payload = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


@dataclass
class FreqtradeAuth:
    access_token: str
    refresh_token: Optional[str]
    exp_utc: Optional[int]  # epoch seconds


class FreqtradeApi:
    """
    Server-side Freqtrade API client with auto token login/refresh.
    Frontend never sees tokens or basic credentials.
    """

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

        self._auth: Optional[FreqtradeAuth] = None
        self._last_login_fail_utc: Optional[int] = None

    # -------- auth helpers --------

    def _need_token(self) -> bool:
        if not self._auth or not self._auth.access_token:
            return True
        if not self._auth.exp_utc:
            # if unknown exp, keep it but be ready to reauth on 401
            return False
        # refresh/login 30 seconds before expiry
        return int(time.time()) >= (self._auth.exp_utc - 30)

    def _login(self) -> None:
        url = f"{self.base_url}/api/v1/token/login"
        data = {
            "username": self.username,
            "password": self.password,
        }
        r = requests.post(
            url,
            data=data,  # form-urlencoded
            timeout=self.timeout,
        )
        if r.status_code != 200:
            self._last_login_fail_utc = int(time.time())
            raise RuntimeError(f"Freqtrade login failed: {r.status_code} {r.text}")

        j = r.json()
        access = j.get("access_token")
        refresh = j.get("refresh_token")

        payload = _jwt_payload(access) if access else {}
        exp = payload.get("exp")
        self._auth = FreqtradeAuth(access_token=access, refresh_token=refresh, exp_utc=exp)

    def _refresh(self) -> bool:
        """
        Try refresh if endpoint exists; return True if refreshed.
        Not all builds expose refresh, so we fallback to login.
        """
        if not self._auth or not self._auth.refresh_token:
            return False

        url = f"{self.base_url}/api/v1/token/refresh"
        headers = {"Authorization": f"Bearer {self._auth.refresh_token}"}

        try:
            r = requests.post(url, headers=headers, timeout=self.timeout)
        except Exception:
            return False

        if r.status_code != 200:
            return False

        j = r.json()
        access = j.get("access_token")
        refresh = j.get("refresh_token", self._auth.refresh_token)

        payload = _jwt_payload(access) if access else {}
        exp = payload.get("exp")
        self._auth = FreqtradeAuth(access_token=access, refresh_token=refresh, exp_utc=exp)
        return True

    def _ensure_auth(self) -> None:
        # avoid tight loop if credentials are wrong
        if self._last_login_fail_utc and int(time.time()) - self._last_login_fail_utc < 5:
            raise RuntimeError("Freqtrade auth throttled (recent login failure).")

        if self._need_token():
            # prefer refresh then login
            if not self._refresh():
                self._login()

    # -------- request layer --------

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Tuple[int, Any]:
        self._ensure_auth()

        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._auth.access_token}"}

        r = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )

        # if token expired /ỉ 401 -> relogin once
        if r.status_code == 401:
            self._login()
            headers = {"Authorization": f"Bearer {self._auth.access_token}"}
            r = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )

        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    # -------- exposed API calls --------

    def ping(self) -> Dict[str, Any]:
        code, data = self._request("GET", "/api/v1/ping")
        if code != 200:
            raise RuntimeError(f"ping failed: {code} {data}")
        return data

    def status(self) -> Any:
        code, data = self._request("GET", "/api/v1/status")
        if code != 200:
            raise RuntimeError(f"status failed: {code} {data}")
        return data

    def start(self) -> Any:
        code, data = self._request("POST", "/api/v1/start")
        if code != 200:
            raise RuntimeError(f"start failed: {code} {data}")
        return data

    def stop(self) -> Any:
        code, data = self._request("POST", "/api/v1/stop")
        if code != 200:
            raise RuntimeError(f"stop failed: {code} {data}")
        return data

    def stopbuy(self) -> Any:
        code, data = self._request("POST", "/api/v1/stopbuy")
        if code != 200:
            raise RuntimeError(f"stopbuy failed: {code} {data}")
        return data

    def stopentry(self) -> Any:
        code, data = self._request("POST", "/api/v1/stopentry")
        if code != 200:
            raise RuntimeError(f"stopentry failed: {code} {data}")
        return data
