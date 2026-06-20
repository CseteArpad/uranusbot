#!/usr/bin/env python3
import json
import math
import time
import hmac
import hashlib
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

CONFIG_PATH = "/opt/bots/uranus/freqtrade/user_data/config.json"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def binance_signed_request(api_key, api_secret, method, path, params):
    query = urllib.parse.urlencode(params, doseq=True)
    signature = hmac.new(
        api_secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{query}&signature={signature}"
    req = urllib.request.Request(url, method=method)
    req.add_header("X-MBX-APIKEY", api_key)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def get_exchange_info(symbol):
    url = f"https://api.binance.com/api/v3/exchangeInfo?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["symbols"][0]

def get_account(api_key, api_secret):
    return binance_signed_request(
        api_key, api_secret, "GET", "/api/v3/account",
        {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
    )

def floor_to_step(value, step):
    value = Decimal(str(value))
    step = Decimal(str(step))
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

def main():
    cfg = load_config()
    ex = cfg.get("exchange", {})
    api_key = ex.get("key")
    api_secret = ex.get("secret")
    whitelist = ex.get("pair_whitelist", [])
    stake_currency = cfg.get("stake_currency", "USDC")

    if not api_key or not api_secret:
        raise SystemExit("ERROR: Binance API kulcs/secret hiányzik a configból.")

    if not whitelist:
        raise SystemExit("ERROR: pair_whitelist üres.")

    account = get_account(api_key, api_secret)
    balances = {b["asset"]: Decimal(b["free"]) for b in account.get("balances", [])}

    for pair in whitelist:
        if "/" not in pair:
            print(f"SKIP: hibás párformátum: {pair}")
            continue

        base, quote = pair.split("/")
        if quote != stake_currency:
            print(f"SKIP: {pair} nem {stake_currency} stake-es pár")
            continue

        free_amount = balances.get(base, Decimal("0"))
        if free_amount <= 0:
            print(f"OK: nincs eladható {base}")
            continue

        symbol = f"{base}{quote}"
        info = get_exchange_info(symbol)

        lot_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        min_qty = Decimal(lot_filter["minQty"])
        step_size = Decimal(lot_filter["stepSize"])

        sell_qty = floor_to_step(free_amount, step_size)

        if sell_qty < min_qty:
            print(f"SKIP: {base} mennyiség túl kicsi eladáshoz. free={free_amount} sellable={sell_qty} minQty={min_qty}")
            continue

        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": str(sell_qty.normalize()),
            "timestamp": int(time.time() * 1000),
            "recvWindow": 10000
        }

        print(f"SELL: {symbol} quantity={sell_qty}")
        resp = binance_signed_request(api_key, api_secret, "POST", "/api/v3/order", params)
        print(json.dumps(resp, ensure_ascii=False))

if __name__ == "__main__":
    main()
