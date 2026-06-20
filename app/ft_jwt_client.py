import os
import json
import time
import base64
from typing import Any, Dict, Optional, Tuple

import requests


def _b64url_decode(data: str) -> bytes:
    data += "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def _jwt_exp_unverified(token: str) -> int:
    """
    JWT 'exp' kiolvasása verifikáció nélkül (csak cache TTL-hez).
    Ha nincs exp, akkor 0.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        return int(payload.get("exp", 0) or 0)
    except Exception:
        return 0


class FreqtradeJWTClient:
    """
    Freqtrade API kliens:
      - token/login Basic+JSON body-val
      - Bearer token injection minden további hívásra
      - token cache exp alapján
      - 401 esetén automatikus relogin és retry
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8090",
        username: Optional[str] = None,
        password: Optional[str] = None,
        config_path: str = "/opt/bots/uranus/freqtrade/user_data/config.json",
        timeout: int = 10,
        skew_seconds: int = 20,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.config_path = config_path
        self.timeout = timeout
        self.skew_seconds = skew_seconds

        self._access_token: str = ""
        self._access_exp: int = 0  # epoch seconds

        self._load_creds_if_missing()

    def _load_creds_if_missing(self) -> None:
        # env elsődleges
        self.base_url = os.getenv("FT_URL", self.base_url).rstrip("/")
        self.username = os.getenv("FT_USER", self.username or "")
        self.password = os.getenv("FT_PASS", self.password or "")

        if self.username and self.password:
            return

        # config.json fallback
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api = cfg.get("api_server", {}) or {}
            u = api.get("username", "") or ""
            p = api.get("password", "") or ""
            if not self.username:
                self.username = u
            if not self.password:
                self.password = p
        except Exception:
            pass

    def _token_valid(self) -> bool:
        if not self._access_token:
            return False
        now = int(time.time())
        # exp előtt kicsivel frissítünk
        return self._access_exp and (now + self.skew_seconds) < self._access_exp

    def _login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("FreqtradeJWTClient: hiányzó FT_USER/FT_PASS (env vagy config.json)")

        url = f"{self.base_url}/api/v1/token/login"
        # Freqtrade-nél a token/login gyakran Basic auth-ot is kér
        r = requests.post(
            url,
            auth=(self.username, self.password),
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"token/login HTTP {r.status_code}: {r.text}")

        data = r.json() if r.content else {}
        tok = (data.get("access_token") or data.get("access") or "").strip()
        if len(tok) < 50:
            raise RuntimeError(f"token/login: nincs access token. body={data}")

        self._access_token = tok
        self._access_exp = _jwt_exp_unverified(tok) or (int(time.time()) + 60)  # ha nincs exp, legyen rövid TTL

    def _ensure_token(self) -> str:
        self._load_creds_if_missing()
        if self._token_valid():
            return self._access_token
        self._login()
        return self._access_token

    def request(self, method: str, path: str, **kwargs) -> Tuple[int, Any]:
        """
        Visszaad: (status_code, json_or_text)
        401 esetén 1x relogin + retry.
        """
        method = method.upper().strip()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.base_url}{path}"

        token = self._ensure_token()
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", self.timeout)

        r = requests.request(method, url, **kwargs)

        if r.status_code == 401:
            # token eldob + újra
            self._access_token = ""
            self._access_exp = 0
            token = self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"
            r = requests.request(method, url, **kwargs)

        try:
            return r.status_code, (r.json() if r.content else {})
        except Exception:
            return r.status_code, (r.text or "")

    # kényelmi metódusok
    def get(self, path: str, **kwargs) -> Tuple[int, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Tuple[int, Any]:
        return self.request("POST", path, **kwargs)
