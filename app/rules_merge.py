from copy import deepcopy
from typing import Dict, List

from rules_schema import RulesValidationError, validate_lab_override_doc, validate_rules_doc


def _index_rules(rules: List[dict]) -> Dict[str, dict]:
    return {str(rule["id"]).strip(): rule for rule in rules}


def _sort_rules(rules: List[dict]) -> List[dict]:
    return sorted(rules, key=lambda r: (int(r.get("priority", 999999)), str(r.get("id", ""))))


def _merge_rule(base_rule: dict, override: dict) -> dict:
    merged = deepcopy(base_rule)

    if "enabled" in override:
        merged["enabled"] = override["enabled"]

    if "priority" in override:
        merged["priority"] = override["priority"]

    if "user_comment" in override:
        merged["user_comment"] = override["user_comment"]

    if "user_comment_updated_at_utc" in override:
        merged["user_comment_updated_at_utc"] = override["user_comment_updated_at_utc"]

    if "params" in override:
        params = deepcopy(merged.get("params", {}))
        params.update(override["params"])
        merged["params"] = params

    return merged


def build_effective_rules(canonical_rules: dict, override_doc: dict | None = None, *, mode: str = "bot") -> dict:
    mode = str(mode or "bot").strip().lower()

    canonical = deepcopy(canonical_rules)
    validate_rules_doc(canonical, allow_lab_extra_rules=False)

    if mode == "bot":
        return canonical

    if mode != "lab":
        raise RulesValidationError(f"Ismeretlen rules merge mód: {mode}")

    if not override_doc:
        return canonical

    override = deepcopy(override_doc)
    validate_lab_override_doc(override)

    if not override.get("enabled", False):
        return canonical

    sell_rules = canonical.get("sell_rules", [])
    buy_rules = canonical.get("buy_rules", [])

    sell_index = _index_rules(sell_rules)
    buy_index = _index_rules(buy_rules)

    overrides = override.get("overrides", {})
    for rule_id, patch in overrides.items():
        if rule_id in sell_index:
            sell_index[rule_id] = _merge_rule(sell_index[rule_id], patch)
        elif rule_id in buy_index:
            buy_index[rule_id] = _merge_rule(buy_index[rule_id], patch)
        else:
            raise RulesValidationError(f"Labor override ismeretlen szabályra hivatkozik: {rule_id}")

    extra_rules = override.get("extra_rules", [])
    for extra in extra_rules:
        side = str(extra.get("side", "")).strip().lower()
        if side == "sell":
            if extra["id"] in sell_index:
                raise RulesValidationError(f"Az extra labor sell szabály már létezik: {extra['id']}")
            sell_index[extra["id"]] = deepcopy(extra)
        elif side == "buy":
            if extra["id"] in buy_index:
                raise RulesValidationError(f"Az extra labor buy szabály már létezik: {extra['id']}")
            buy_index[extra["id"]] = deepcopy(extra)
        else:
            raise RulesValidationError(f"Az extra labor szabály side mezője hibás: {extra.get('id')}")

    merged = deepcopy(canonical)
    merged["meta"] = deepcopy(canonical.get("meta", {}))
    merged["meta"]["profile_type"] = "lab-effective"
    merged["meta"]["lab_override_profile_name"] = override.get("profile_name")
    merged["meta"]["lab_override_enabled"] = True

    merged["sell_rules"] = _sort_rules(list(sell_index.values()))
    merged["buy_rules"] = _sort_rules(list(buy_index.values()))

    sell_hierarchy = []
    for rule in merged["sell_rules"]:
        if not rule.get("lab_only", False):
            sell_hierarchy.append(rule["id"])

    buy_hierarchy = []
    for rule in merged["buy_rules"]:
        if not rule.get("lab_only", False):
            buy_hierarchy.append(rule["id"])

    merged["sell_hierarchy"] = sell_hierarchy
    merged["buy_hierarchy"] = buy_hierarchy

    return merged
