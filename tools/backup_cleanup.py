#!/usr/bin/env python3
from pathlib import Path
import shutil

DELETE = True

GROUPS = [
    {
        "name": "/opt/backups közvetlen mentések",
        "root": Path("/opt/backups"),
        "keep_last": 3,
        "prefixes": None,
        "depth": "direct",
    },
    {
        "name": "/root rollback és régi botapp mentések",
        "root": Path("/root"),
        "keep_last": 3,
        "prefixes": ["rollback_", "botapp_", "_TRASH_"],
        "depth": "direct",
    },
]

def candidates_for(group):
    root = group["root"]
    prefixes = group["prefixes"]
    if not root.exists():
        return []

    items = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        if prefixes is None:
            items.append(p)
        elif any(p.name.startswith(x) for x in prefixes):
            items.append(p)
    return sorted(items, key=lambda x: x.stat().st_mtime, reverse=True)

def main():
    print("=== BACKUP CLEANUP SAFE DRY RUN ===")
    print(f"DELETE = {DELETE}")
    print()

    total_delete = 0

    for group in GROUPS:
        items = candidates_for(group)
        keep = items[:group["keep_last"]]
        delete = items[group["keep_last"]:]

        print()
        print(f"CSOPORT: {group['name']}")
        print(f"KEEP_LAST = {group['keep_last']}")

        print("MEGTARTVA:")
        for p in keep:
            print(f"  KEEP   {p}")

        print("TÖRLÉSRE JELÖLVE:")
        for p in delete:
            print(f"  DELETE {p}")
            total_delete += 1
            if DELETE:
                shutil.rmtree(p)

    print()
    print(f"Összes törlésre jelölt mappa: {total_delete}")
    if DELETE:
        print("Törlés végrehajtva.")
    else:
        print("Ez még NEM törölt semmit.")

if __name__ == "__main__":
    main()
