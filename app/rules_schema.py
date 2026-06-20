import copy
from datetime import datetime, timezone


ALLOWED_SIDES = {"buy", "sell"}


class RulesValidationError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_number(value) -> bool:
    try:
        float(value)
        return True
    except Exception:
        return False


def _ensure_dict(value, name: str):
    if not isinstance(value, dict):
        raise RulesValidationError(f"{name} mezőnek objektumnak kell lennie.")
    return value


def _ensure_list(value, name: str):
    if not isinstance(value, list):
        raise RulesValidationError(f"{name} mezőnek listának kell lennie.")
    return value


def _validate_rule(rule: dict, section_name: str):
    _ensure_dict(rule, f"{section_name} szabály")

    required = ["id", "title", "side", "enabled", "priority", "params", "lab_only"]
    for key in required:
        if key not in rule:
            raise RulesValidationError(f"Hiányzó kötelező mező a(z) {section_name} szabálynál: {key}")

    rule_id = str(rule.get("id", "")).strip()
    if not rule_id:
        raise RulesValidationError(f"A(z) {section_name} egyik szabályának azonosítója üres.")

    side = str(rule.get("side", "")).strip().lower()
    if side not in ALLOWED_SIDES:
        raise RulesValidationError(f"A(z) {rule_id} szabály oldala csak 'buy' vagy 'sell' lehet.")

    if not isinstance(rule.get("enabled"), bool):
        raise RulesValidationError(f"A(z) {rule_id} szabály enabled mezője csak true/false lehet.")

    if not isinstance(rule.get("lab_only"), bool):
        raise RulesValidationError(f"A(z) {rule_id} szabály lab_only mezője csak true/false lehet.")

    if not isinstance(rule.get("priority"), int):
        raise RulesValidationError(f"A(z) {rule_id} szabály priority mezője csak egész szám lehet.")

    params = rule.get("params")
    _ensure_dict(params, f"{rule_id}.params")

    for key, value in params.items():
        if not _is_number(value):
            raise RulesValidationError(f"A(z) {rule_id} szabály {key} paramétere nem szám: {value}")

    for key in ["title", "goal", "description", "level_key", "level_formula_human", "system_comment", "user_comment"]:
        if key in rule and rule.get(key) is not None and not isinstance(rule.get(key), str):
            raise RulesValidationError(f"A(z) {rule_id} szabály {key} mezője csak szöveg lehet.")

    for key in ["conditions_human"]:
        if key in rule and rule.get(key) is not None:
            _ensure_list(rule.get(key), f"{rule_id}.{key}")


def _validate_rule_list(rules: list, expected_side: str, section_name: str):
    _ensure_list(rules, section_name)

    seen_ids = set()
    seen_priorities = set()

    for rule in rules:
        _validate_rule(rule, section_name)

        rule_id = str(rule["id"]).strip()
        if rule_id in seen_ids:
            raise RulesValidationError(f"Duplikált szabályazonosító a(z) {section_name} listában: {rule_id}")
        seen_ids.add(rule_id)

        if str(rule["side"]).strip().lower() != expected_side:
            raise RulesValidationError(f"A(z) {rule_id} szabály side mezője hibás, elvárt: {expected_side}")

        prio = rule["priority"]
        if prio in seen_priorities:
            raise RulesValidationError(f"Duplikált priority a(z) {section_name} listában: {prio}")
        seen_priorities.add(prio)


def validate_rules_doc(doc: dict, *, allow_lab_extra_rules: bool = False) -> dict:
    _ensure_dict(doc, "rules dokumentum")

    if int(doc.get("schema_version", 0)) != 1:
        raise RulesValidationError("A rules dokumentum schema_version mezője csak 1 lehet.")

    meta = _ensure_dict(doc.get("meta", {}), "meta")
    _ensure_dict(doc.get("definitions", {}), "definitions")
    globals_obj = _ensure_dict(doc.get("globals", {}), "globals")

    sell_hierarchy = _ensure_list(doc.get("sell_hierarchy", []), "sell_hierarchy")
    buy_hierarchy = _ensure_list(doc.get("buy_hierarchy", []), "buy_hierarchy")

    sell_rules = doc.get("sell_rules", [])
    buy_rules = doc.get("buy_rules", [])

    _validate_rule_list(sell_rules, "sell", "sell_rules")
    _validate_rule_list(buy_rules, "buy", "buy_rules")

    allow_disable = bool(globals_obj.get("allow_rule_disable", True))

    canonical_sell_ids = [r["id"] for r in sell_rules if not r.get("lab_only", False)]
    canonical_buy_ids = [r["id"] for r in buy_rules if not r.get("lab_only", False)]

    if set(sell_hierarchy) != set(canonical_sell_ids):
        raise RulesValidationError("A sell_hierarchy és a sell_rules kanonikus azonosítói nem egyeznek.")

    if set(buy_hierarchy) != set(canonical_buy_ids):
        raise RulesValidationError("A buy_hierarchy és a buy_rules kanonikus azonosítói nem egyeznek.")

    if not allow_lab_extra_rules:
        for rule in sell_rules + buy_rules:
            if rule.get("lab_only", False):
                raise RulesValidationError(f"A(z) {rule['id']} szabály lab_only=true, de ez a dokumentum nem laborprofil.")

    if not allow_disable:
        for rule in sell_rules + buy_rules:
            if rule.get("enabled") is False:
                raise RulesValidationError(f"A(z) {rule['id']} szabály nem tiltható le ebben a dokumentumban.")

    if "updated_at_utc" not in meta:
        meta["updated_at_utc"] = None

    return doc


def validate_lab_override_doc(doc: dict) -> dict:
    _ensure_dict(doc, "labor override dokumentum")

    if int(doc.get("schema_version", 0)) != 1:
        raise RulesValidationError("A labor override schema_version mezője csak 1 lehet.")

    if not isinstance(doc.get("enabled", False), bool):
        raise RulesValidationError("A labor override enabled mezője csak true/false lehet.")

    if not isinstance(doc.get("profile_name", ""), str) or not str(doc.get("profile_name", "")).strip():
        raise RulesValidationError("A labor override profile_name mezője kötelező és nem lehet üres.")

    if "notes" in doc and doc.get("notes") is not None and not isinstance(doc.get("notes"), str):
        raise RulesValidationError("A labor override notes mezője csak szöveg lehet.")

    overrides = _ensure_dict(doc.get("overrides", {}), "overrides")
    extra_rules = _ensure_list(doc.get("extra_rules", []), "extra_rules")

    for rule_id, override in overrides.items():
        _ensure_dict(override, f"override[{rule_id}]")
        for key in override.keys():
            if key not in {"enabled", "priority", "params", "user_comment", "user_comment_updated_at_utc"}:
                raise RulesValidationError(f"Ismeretlen override mező a(z) {rule_id} szabálynál: {key}")

        if "enabled" in override and not isinstance(override["enabled"], bool):
            raise RulesValidationError(f"A(z) {rule_id} override enabled mezője csak true/false lehet.")

        if "priority" in override and not isinstance(override["priority"], int):
            raise RulesValidationError(f"A(z) {rule_id} override priority mezője csak egész szám lehet.")

        if "params" in override:
            _ensure_dict(override["params"], f"{rule_id}.override.params")
            for p_key, p_val in override["params"].items():
                if not _is_number(p_val):
                    raise RulesValidationError(f"A(z) {rule_id} override {p_key} paramétere nem szám: {p_val}")

        if "user_comment" in override and override["user_comment"] is not None and not isinstance(override["user_comment"], str):
            raise RulesValidationError(f"A(z) {rule_id} override user_comment mezője csak szöveg lehet.")

    seen_ids = set()
    seen_priorities = set()

    for rule in extra_rules:
        _validate_rule(rule, "extra_rules")

        if not rule.get("lab_only", False):
            raise RulesValidationError(f"A(z) {rule['id']} extra labor szabálynál a lab_only mezőnek true-nak kell lennie.")

        rule_id = str(rule["id"]).strip()
        if rule_id in seen_ids:
            raise RulesValidationError(f"Duplikált extra labor szabályazonosító: {rule_id}")
        seen_ids.add(rule_id)

        prio = int(rule["priority"])
        if prio in seen_priorities:
            raise RulesValidationError(f"Duplikált priority az extra labor szabályoknál: {prio}")
        seen_priorities.add(prio)

    return doc


def clone_doc(doc: dict) -> dict:
    return copy.deepcopy(doc)
