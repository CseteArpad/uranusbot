import json
import os
import tempfile
from typing import Any, Dict

from rules_schema import (
    RulesValidationError,
    clone_doc,
    utc_now_iso,
    validate_lab_override_doc,
    validate_rules_doc,
)


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))

RULES_PATH = os.path.join(APP_DIR, "config", "rules.json")
LAB_OVERRIDE_PATH = "/opt/bots/uranus_monitor/data/lab_rules_override.json"
LAB_PROFILES_DIR = "/opt/bots/uranus_monitor/data/lab_rule_profiles"


def read_json(path: str, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return clone_doc(default)


def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    dirn = os.path.dirname(path) or "."
    base = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=dirn)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def default_lab_override_doc() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_name": "default",
        "enabled": False,
        "notes": "Alap labor override fájl. Az éles botot nem érinti.",
        "overrides": {},
        "extra_rules": [],
    }


def load_rules_doc() -> Dict[str, Any]:
    doc = read_json(RULES_PATH, default={})
    return validate_rules_doc(doc, allow_lab_extra_rules=False)


def save_rules_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_rules_doc(clone_doc(doc), allow_lab_extra_rules=False)
    validated.setdefault("meta", {})
    validated["meta"]["updated_at_utc"] = utc_now_iso()
    atomic_write_json(RULES_PATH, validated)
    return validated


def load_lab_override_doc() -> Dict[str, Any]:
    doc = read_json(LAB_OVERRIDE_PATH, default=default_lab_override_doc())
    return validate_lab_override_doc(doc)


def save_lab_override_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_lab_override_doc(clone_doc(doc))
    atomic_write_json(LAB_OVERRIDE_PATH, validated)
    return validated


def ensure_lab_profiles_dir() -> str:
    os.makedirs(LAB_PROFILES_DIR, exist_ok=True)
    return LAB_PROFILES_DIR


def list_lab_profiles() -> list[str]:
    ensure_lab_profiles_dir()
    names = []
    for name in sorted(os.listdir(LAB_PROFILES_DIR)):
        if name.lower().endswith(".json"):
            names.append(name)
    return names


def profile_path(profile_name: str) -> str:
    safe = str(profile_name or "").strip()
    if not safe:
        raise RulesValidationError("A profil neve nem lehet üres.")
    if "/" in safe or "\\" in safe or safe.startswith("."):
        raise RulesValidationError("Érvénytelen profilnév.")
    return os.path.join(ensure_lab_profiles_dir(), f"{safe}.json")


def load_lab_profile(profile_name: str) -> Dict[str, Any]:
    path = profile_path(profile_name)
    doc = read_json(path, default={})
    return validate_lab_override_doc(doc)


def save_lab_profile(profile_name: str, doc: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_lab_override_doc(clone_doc(doc))
    path = profile_path(profile_name)
    atomic_write_json(path, validated)
    return validated
