# rule_engine_selftest.py
# Uranus – Selftest runner (SELL + BUY)
#
# Futás:
#   cd /opt/bots/uranus/app && python3 rule_engine_selftest.py

import subprocess
import sys
from pathlib import Path


def run_one(script: str) -> int:
    p = Path(__file__).resolve().parent / script
    if not p.exists():
        print(f"Missing: {p}")
        return 2
    print(f"\n=== RUN: {script} ===")
    return subprocess.call([sys.executable, str(p)])


def main() -> int:
    rc = 0
    rc = rc or run_one("rule_engine_selftest_sell.py")
    rc = rc or run_one("rule_engine_selftest_buy.py")
    print("\nALL OK (RUNNER)" if rc == 0 else "\nFAILED (RUNNER)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
