#!/usr/bin/env python3
"""
Uranus – VENUE PREFLIGHT (systemd ``ExecStartPre`` guard).

Ez a szkript az utolsó kapu a **folyamat elindulása előtt**. Ha az
``execution_venue_policy`` elutasítja az indulást, nem nullával lép ki, és a
systemd ``ExecStartPre`` szemantikája miatt a service **el sem indul**.

Miért ``ExecStartPre`` és nem csak a runner belsejében
------------------------------------------------------
A Freqtrade harmadik féltől származó bináris: nem tudunk kódot tenni bele.
Az egyetlen hely, ahol a *Freqtrade* indulását még meg lehet akadályozni, a
unit ``ExecStartPre``-je. Ugyanezt a szkriptet használja a runner unit is, így
mindkét végrehajtási réteg **ugyanabból a politikából** kap tiltást – nem két,
idővel elcsúszó implementációból.

Kilépési kódok::

    0   – az indulás engedélyezett
    78  – az indulás ELUTASÍTVA (EX_CONFIG; a unit
          ``RestartPreventExitStatus=78``-cal nem esik restart-hurokba)

Használat::

    python3 /opt/bots/uranus/app/venue_preflight.py            # production config
    python3 app/venue_preflight.py --config path/to/config.json
    python3 app/venue_preflight.py --json                      # gépi kimenet

A szkript **csak olvas**: nem indít folyamatot, nem ír fájlt, nem küld hálózati
kérést, és titok-értéket sem ír ki – a hitelesítési nyomokat kizárólag
mezőnév/útvonal szinten nevezi meg.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

try:  # package import (pytest, repo gyökér a sys.path-on)
    from app import execution_venue_policy as policy  # type: ignore
except ImportError:  # flat import (production runner / systemd)
    import execution_venue_policy as policy  # type: ignore


def run(config_path: str | None = None, as_json: bool = False) -> int:
    config = policy.load_ft_config(config_path)
    verdict = policy.evaluate_startup(config, os.environ)

    if as_json:
        payload = {
            "preflight": "uranus-venue-preflight",
            "config_path": config_path or "<resolved>",
            "policy": policy.policy_summary(),
            "verdict": verdict.as_dict(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"[venue_preflight] {verdict.log_line()}")
        if not verdict.allowed:
            print(
                "[venue_preflight] URANUS_EXECUTION_VENUE_POLICY=OKX_SPOT_ONLY · "
                "BINANCE_EXECUTION_ALLOWED=NO · "
                f"OKX_LIVE_EXECUTION_AUTHORIZED="
                f"{'YES' if policy.LIVE_EXECUTION_AUTHORIZED else 'NO'}",
                file=sys.stderr,
            )
            print(
                "[venue_preflight] startup REFUSED - the service will not be started.",
                file=sys.stderr,
            )

    return 0 if verdict.allowed else policy.EXIT_STARTUP_REFUSED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Uranus execution-venue preflight guard (read-only).",
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Freqtrade config.json path (default: canonical Uranus config).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Machine-readable output.",
    )
    args = parser.parse_args(argv)
    return run(args.config, args.as_json)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
