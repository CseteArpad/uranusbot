import os
import time
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple


@dataclass
class ExecResult:
    ok: bool
    action: str
    detail: str
    http_status: Optional[int] = None
    response: Optional[Any] = None


class FreqtradeExecutor:
    """
    Safe-by-default executor:
    - execution disabled unless EXECUTION_ENABLED=1
    - provides API calls + confirmation helpers
    - does NOT write state.json
    - HARD GUARD: FORCEEXIT is blocked if the open trade is not profitable after fee buffer
    """

    def __init__(self):
        self.enabled = os.getenv("EXECUTION_ENABLED", "0") == "1"
        self.ft_base_url = os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")
        self.username = os.getenv("FT_USERNAME", "")
        self.password = os.getenv("FT_PASSWORD", "")
        self.pair = os.getenv("PAIR", "")
        self.timeout_sec = int(os.getenv("EXECUTION_TIMEOUT_SEC", "8"))
        self.confirm_wait_sec = float(os.getenv("EXECUTION_CONFIRM_WAIT_SEC", "0.8"))

        # Binance fee kb. 0.075% oldalanként; round-trip védelem alapból 0.15%
        self.forceexit_min_profit_ratio = float(os.getenv("FORCEEXIT_MIN_PROFIT_RATIO", "0.0015"))

    def _basic_auth_header(self) -> Dict[str, str]:
        if not self.username and not self.password:
            return {}
        import base64
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        url = f"{self.ft_base_url}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        headers.update(self._basic_auth_header())

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw_bytes = resp.read()
                raw = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
                try:
                    body = json.loads(raw) if raw else {}
                except Exception:
                    body = {"raw": raw}
                return resp.status, body

        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw_bytes = e.read()
                raw = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
            except Exception:
                raw = ""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {"raw": raw}
            return e.code, body

        except Exception as e:
            return 0, {"error": str(e)}

    def get_status(self) -> ExecResult:
        code, body = self._request_json("GET", "/api/v1/status", None)
        ok = (code == 200)
        return ExecResult(ok=ok, action="STATUS", detail="ok" if ok else "status_failed", http_status=code, response=body)

    def get_trades(self) -> ExecResult:
        code, body = self._request_json("GET", "/api/v1/trades", None)
        ok = (code == 200)
        return ExecResult(ok=ok, action="TRADES", detail="ok" if ok else "trades_failed", http_status=code, response=body)

    def _get_open_trade_from_status(self, pair: Optional[str] = None) -> ExecResult:
        pair = pair or self.pair

        r = self.get_status()
        if not r.ok:
            return ExecResult(False, "OPEN_TRADE_LOOKUP", "status_failed", http_status=r.http_status, response=r.response)

        raw = r.response
        if not isinstance(raw, list):
            return ExecResult(False, "OPEN_TRADE_LOOKUP", "unexpected_status_schema", http_status=r.http_status, response=raw)

        open_trades = [x for x in raw if isinstance(x, dict) and x.get("is_open")]
        if not open_trades:
            return ExecResult(False, "OPEN_TRADE_LOOKUP", "no_open_trade", http_status=r.http_status, response=raw)

        if pair:
            for t in open_trades:
                if str(t.get("pair")) == str(pair):
                    return ExecResult(True, "OPEN_TRADE_LOOKUP", "ok", http_status=r.http_status, response=t)
            return ExecResult(False, "OPEN_TRADE_LOOKUP", "open_trade_for_pair_not_found", http_status=r.http_status, response=raw)

        return ExecResult(True, "OPEN_TRADE_LOOKUP", "ok", http_status=r.http_status, response=open_trades[0])

    def _to_float(self, value, default=None):
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _trade_profit_ratio(self, trade: Dict[str, Any]) -> Optional[float]:
        """
        Freqtrade status többféle kulcsot adhat vissza.
        Elsőként a profit_ratio mezőket használjuk, ha vannak.
        Ha nincs, open_rate/current_rate alapján számolunk.
        """
        for key in (
            "profit_ratio",
            "current_profit",
            "current_profit_ratio",
            "close_profit",
        ):
            val = self._to_float(trade.get(key), None)
            if val is not None:
                return val

        open_rate = self._to_float(trade.get("open_rate"), None)
        current_rate = self._to_float(
            trade.get("current_rate")
            or trade.get("current_price")
            or trade.get("close_rate")
            or trade.get("rate"),
            None,
        )

        if open_rate and current_rate:
            return (current_rate - open_rate) / open_rate

        return None

    def force_enter(self, pair: Optional[str] = None) -> ExecResult:
        pair = pair or self.pair
        if not pair:
            return ExecResult(False, "FORCEENTER", "pair_missing")
        if not self.enabled:
            return ExecResult(False, "FORCEENTER", "execution_disabled")

        payload = {"pair": pair}
        code, body = self._request_json("POST", "/api/v1/forceenter", payload)
        ok = (code in (200, 201))
        return ExecResult(ok=ok, action="FORCEENTER", detail="ok" if ok else "forceenter_failed", http_status=code, response=body)

    def force_exit(self, pair: Optional[str] = None) -> ExecResult:
        pair = pair or self.pair
        if not pair:
            return ExecResult(False, "FORCEEXIT", "pair_missing")
        if not self.enabled:
            return ExecResult(False, "FORCEEXIT", "execution_disabled")

        trade_lookup = self._get_open_trade_from_status(pair)
        if not trade_lookup.ok:
            return ExecResult(
                False,
                "FORCEEXIT",
                f"open_trade_lookup_failed:{trade_lookup.detail}",
                http_status=trade_lookup.http_status,
                response=trade_lookup.response,
            )

        trade = trade_lookup.response if isinstance(trade_lookup.response, dict) else {}
        trade_id = trade.get("trade_id")

        try:
            trade_id = int(trade_id)
        except Exception:
            return ExecResult(
                False,
                "FORCEEXIT",
                "tradeid_missing_or_invalid",
                http_status=trade_lookup.http_status,
                response=trade,
            )

        profit_ratio = self._trade_profit_ratio(trade)

        if profit_ratio is None:
            return ExecResult(
                False,
                "FORCEEXIT",
                "blocked_profit_unknown",
                http_status=trade_lookup.http_status,
                response=trade,
            )

        if profit_ratio < self.forceexit_min_profit_ratio:
            return ExecResult(
                False,
                "FORCEEXIT",
                f"blocked_not_profitable_after_fee:profit_ratio={profit_ratio:.8f},min={self.forceexit_min_profit_ratio:.8f}",
                http_status=trade_lookup.http_status,
                response=trade,
            )

        payload = {"tradeid": trade_id}
        code, body = self._request_json("POST", "/api/v1/forceexit", payload)
        ok = (code in (200, 201))
        return ExecResult(
            ok=ok,
            action="FORCEEXIT",
            detail="ok" if ok else "forceexit_failed",
            http_status=code,
            response=body,
        )

    def confirm_open_trades(self) -> ExecResult:
        time.sleep(self.confirm_wait_sec)

        r = self.get_status()
        if not r.ok:
            return ExecResult(False, "CONFIRM", "status_check_failed", http_status=r.http_status, response=r.response)

        raw = r.response
        if isinstance(raw, list):
            try:
                open_trades = len([x for x in raw if isinstance(x, dict) and x.get("is_open")])
            except Exception:
                open_trades = 0
            data = {"open_trades": open_trades, "raw": raw}
            return ExecResult(True, "CONFIRM", f"open_trades={open_trades}", http_status=r.http_status, response=data)

        data = {"open_trades": None, "raw": raw}
        return ExecResult(False, "CONFIRM", "unexpected_status_schema", http_status=r.http_status, response=data)
