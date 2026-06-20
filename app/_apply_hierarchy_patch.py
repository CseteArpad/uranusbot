# -*- coding: utf-8 -*-
import re
from pathlib import Path

APP = Path("/opt/bots/uranus/app/app.py")
src = APP.read_text(encoding="utf-8")

# 1) Import beszúrás (ha még nincs)
if "from rules_engine import enforce_targets_hierarchy" not in src:
    # Import blokk után próbáljuk szúrni
    m = re.search(r"(^import .+$|^from .+ import .+$)", src, flags=re.M)
    if m:
        # az első import(ok) után (következő üres sor elé)
        # egyszerűbb: a legelső import sor után szúrjuk
        lines = src.splitlines(True)
        inserted = False
        out = []
        for i, line in enumerate(lines):
            out.append(line)
            if not inserted and (line.startswith("import ") or line.startswith("from ")):
                # nézzük, hogy a következő sor nem import-e; ha az, akkor várunk
                # végül a folyamatos import blokk végén szúrunk
                j = i
                while j + 1 < len(lines) and (lines[j+1].startswith("import ") or lines[j+1].startswith("from ")):
                    j += 1
                    out.append(lines[j])
                out.append("\nfrom rules_engine import enforce_targets_hierarchy\n")
                # kihagyjuk azokat, amiket már hozzáadtunk
                # (mivel out-ba már betettük, a ciklusban duplázódna, ezért trükk: jelöljük)
                inserted = True
                # a fő ciklust úgy fejezzük be, hogy a maradékot az eredeti i->j után tesszük
                out.extend(lines[j+1:])
                src = "".join(out)
                break
        if not inserted:
            src = "from rules_engine import enforce_targets_hierarchy\n" + src
    else:
        src = "from rules_engine import enforce_targets_hierarchy\n" + src

# 2) /api/targets: jsonify(targets) elé kényszerítés
def patch_api_targets(text: str) -> str:
    # próbáljuk megtalálni a route-ot és azon belül a jsonify(targets)-t
    # többféle dekorátor lehet, ezért lazább keresés
    pat = r"(@app\.(?:get|route)\([\"']\/api\/targets[\"'].*?\):\s*\n)([\s\S]*?)(\n\s*return\s+jsonify\(\s*targets\s*\))"
    m = re.search(pat, text)
    if not m:
        return text

    block_header = m.group(1)
    body = m.group(2)
    ret = m.group(3)

    if "enforce_targets_hierarchy(targets)" in body:
        return text  # már patchelve

    injection = "\n    targets = enforce_targets_hierarchy(targets)\n"
    new_body = body + injection
    return text[:m.start()] + block_header + new_body + ret + text[m.end():]

src2 = patch_api_targets(src)

# 3) /api/state: ha state-be tesz targets-ot, húzzuk rá
# Keresünk egy tipikus sort: state["targets"] = targets  vagy state['targets']=...
# és közvetlenül elé beszúrjuk: targets = enforce_targets_hierarchy(targets)
def patch_state_targets(text: str) -> str:
    pat = r"(\n\s*)(state\[\s*[\"']targets[\"']\s*\]\s*=\s*targets\s*)"
    if re.search(pat, text) and "targets = enforce_targets_hierarchy(targets)" not in text:
        return re.sub(pat, r"\1targets = enforce_targets_hierarchy(targets)\1\2", text, count=1)
    return text

src3 = patch_state_targets(src2)

APP.write_text(src3, encoding="utf-8")
print("OK: patch alkalmazva:", APP)
