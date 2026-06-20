import re
from pathlib import Path

APP = Path("app.py")

INSERT_IMPORT = """\
from rule_engine import (
    RuleLevels,
    TickContext,
    evaluate,
    compute_insufficient_rise,
    Action,
)
"""

RULE6_BLOCK = r"""
# ==========================
# RULE6 ENGINE (SINGLE SOURCE OF TRUTH)
# ==========================
def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def _rule6_get_levels(state: dict):
    # These keys must exist in state.json (UI settings) OR be computed earlier in app.py.
    # If a key is missing, we return None and skip rule-eval for safety.
    keys = [
        "std_sell", "profitless_sell", "panic_sell",
        "std_buy", "profitless_buy", "panic_buy",
        "enough_rise_level",
    ]
    vals = {k: _safe_float(state.get(k), None) for k in keys}
    if any(vals[k] is None for k in keys):
        return None

    allow_profitless_buy = bool(state.get("allow_profitless_buy", True))

    return RuleLevels(
        std_sell=vals["std_sell"],
        profitless_sell=vals["profitless_sell"],
        panic_sell=vals["panic_sell"],
        std_buy=vals["std_buy"],
        profitless_buy=vals["profitless_buy"],
        panic_buy=vals["panic_buy"],
        enough_rise_level=vals["enough_rise_level"],
        allow_profitless_buy=allow_profitless_buy,
    )

def _rule6_apply(state: dict, last: float, prev_last: float) -> str:
    \"\"\"Compute ONE action per tick using rule_engine.py and write into state.
    Returns action string (e.g. 'SELL_STD') or 'NONE'.
    \"\"\"
    levels = _rule6_get_levels(state)
    if levels is None:
        state["last_action"] = state.get("last_action") or "NONE"
        return state["last_action"]

    in_position = bool(state.get("in_position", False))

    # Maintain peak/low depending on position state
    if in_position:
        peak = _safe_float(state.get("peak_price"), last) or last
        peak = max(peak, last)
        state["peak_price"] = peak
    else:
        low = _safe_float(state.get("low_price"), last) or last
        low = min(low, last)
        state["low_price"] = low

    # Flag: "nem volt elég emelkedés"
    insufficient_rise = False
    if in_position:
        insufficient_rise = compute_insufficient_rise(
            peak_since_entry=float(state.get("peak_price", last)),
            enough_rise_level=float(levels.enough_rise_level),
        )
    state["insufficient_rise"] = bool(insufficient_rise)

    ctx = TickContext(
        in_position=in_position,
        prev_last=float(prev_last),
        last=float(last),
        peak_since_entry=float(state.get("peak_price", last)),
        low_since_exit=float(state.get("low_price", last)),
        insufficient_rise=bool(insufficient_rise),
    )

    action = evaluate(levels, ctx)
    state["last_action"] = action.value
    return state["last_action"]
"""

PATCH_MARKER = "# ==========================\n# RULE6 ENGINE (SINGLE SOURCE OF TRUTH)\n# ==========================\n"

def main():
    txt = APP.read_text(encoding="utf-8")

    # 1) Ensure import exists
    if "from rule_engine import" not in txt:
        # Insert after last import/from line near the top.
        lines = txt.splitlines(True)
        ins_at = 0
        for i, line in enumerate(lines[:250]):
            if line.startswith("import ") or line.startswith("from "):
                ins_at = i + 1
        lines.insert(ins_at, INSERT_IMPORT + "\n")
        txt = "".join(lines)

    # 2) Ensure RULE6 block exists (single source of truth)
    if PATCH_MARKER not in txt:
        # Insert RULE6 block after helper section or near top after imports.
        # Strategy: insert after the first blank line following imports.
        m = re.search(r"(?m)^(from |import ).*\n(?:from |import ).*\n", txt)
        if m:
            # Find next blank line after imports block
            start = m.end()
            m2 = re.search(r"\n\s*\n", txt[start:])
            if m2:
                insert_pos = start + m2.end()
            else:
                insert_pos = start
        else:
            insert_pos = 0
        txt = txt[:insert_pos] + "\n" + RULE6_BLOCK.strip("\n") + "\n\n" + txt[insert_pos:]

    # 3) Hook into tick update: after last/prev_last becomes available.
    # We look for a robust pattern you already have (seen in your screenshots):
    #   prices = get_last_prev_last(...)
    #   last = prices["last"]
    #   prev_last = prices["prev_last"]
    #
    # Insert: _rule6_apply(state, last, prev_last)
    hook_re = re.compile(
        r'(?s)(prices\s*=\s*get_last_prev_last\([^\n]*\)\s*\n.*?last\s*=\s*prices\[[\'"]last[\'"]\]\s*\n.*?prev_last\s*=\s*prices\[[\'"]prev_last[\'"]\]\s*\n)'
    )

    if "_rule6_apply(state, last, prev_last)" not in txt:
        m = hook_re.search(txt)
        if m:
            block = m.group(1)
            inject = block + "\n    _rule6_apply(state, last, prev_last)\n"
            txt = txt[:m.start(1)] + inject + txt[m.end(1):]
        else:
            # fallback: try a simpler last/prev_last tuple pattern
            hook_re2 = re.compile(r'(?s)(last\s*=\s*.*\n.*prev_last\s*=\s*.*\n)')
            m2 = hook_re2.search(txt)
            if m2:
                block = m2.group(1)
                inject = block + "\n    _rule6_apply(state, last, prev_last)\n"
                txt = txt[:m2.start(1)] + inject + txt[m2.end(1):]
            else:
                # If we cannot find, append a WARNING comment (no hard break).
                txt += "\n\n# WARNING: rule6 hook not auto-inserted. Insert manually: _rule6_apply(state, last, prev_last)\n"

    APP.write_text(txt, encoding="utf-8")
    print("OK: app.py patched with RULE6 engine and hook")

if __name__ == "__main__":
    main()
