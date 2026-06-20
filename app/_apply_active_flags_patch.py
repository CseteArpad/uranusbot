# -*- coding: utf-8 -*-
import re
from pathlib import Path

APP = Path("/opt/bots/uranus/app/app.py")
src = APP.read_text(encoding="utf-8")

# 1) Import csere / hozzáadás
if "from rules_engine import apply_hierarchy_and_flags" not in src:
    # ha a régi import benne van, cseréljük
    src = src.replace(
        "from rules_engine import enforce_targets_hierarchy",
        "from rules_engine import apply_hierarchy_and_flags",
    )
    if "from rules_engine import apply_hierarchy_and_flags" not in src:
        # beszúrás az import blokk végére
        m = re.search(r"^(?:import .+|from .+ import .+)(?:\n(?:import .+|from .+ import .+))*", src, flags=re.M)
        if m:
            insert_at = m.end()
            src = src[:insert_at] + "\nfrom rules_engine import apply_hierarchy_and_flags\n" + src[insert_at:]
        else:
            src = "from rules_engine import apply_hierarchy_and_flags\n" + src

# 2) Hívások cseréje
src = src.replace("enforce_targets_hierarchy(targets)", "apply_hierarchy_and_flags(targets)")

APP.write_text(src, encoding="utf-8")
print("OK: patch kész ->", APP)
