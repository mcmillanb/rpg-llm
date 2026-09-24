"""Real dice for the GM: the model asks, the app rolls."""

import re
import secrets

TOOL = {"type": "function", "function": {
    "name": "roll_dice",
    "description": "Roll dice for an uncertain action. The app rolls real dice and returns the "
                   "result; narrate from it. Examples: dice='2D6+1', target=8 (need 8 or more); "
                   "dice='d100', target=45, success_if='at_most' (roll under a skill).",
    "parameters": {"type": "object", "properties": {
        "dice": {"type": "string", "description": "e.g. 2D6, 2D6+1, d20-2, 3d6, d100"},
        "reason": {"type": "string", "description": "what the roll is for, a few words"},
        "target": {"type": "integer", "description": "number to compare against (optional)"},
        "success_if": {"type": "string", "enum": ["at_least", "at_most"],
                       "description": "at_least (default): total >= target succeeds"},
    }, "required": ["dice", "reason"]}}}

_DICE = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+|%)\s*(?:([+-])\s*(\d+))?\s*$")


def roll(dice: str, reason: str = "", target: int | None = None,
         success_if: str = "at_least") -> dict:
    m = _DICE.match(dice or "")
    if not m:
        raise ValueError(f"can't read dice {dice!r}; use a form like 2D6+1")
    count = int(m.group(1) or 1)
    sides = 100 if m.group(2) == "%" else int(m.group(2))
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        raise ValueError("between 1 and 20 dice of 2 to 1000 sides")
    mod = int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)
    rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
    total = sum(rolls) + mod
    out = {"dice": f"{count}D{sides}{f'{mod:+d}' if mod else ''}", "reason": reason.strip(),
           "rolls": rolls, "modifier": mod, "total": total}
    if target is not None:
        ok = total <= target if success_if == "at_most" else total >= target
        out.update(target=target, success_if=success_if, success=ok)
    return out


def describe(r: dict) -> str:
    """What the GM reads back."""
    parts = " + ".join(map(str, r["rolls"]))
    mod = f" {'+' if r['modifier'] > 0 else '-'} {abs(r['modifier'])}" if r["modifier"] else ""
    text = f"{r['dice']} for {r['reason'] or 'the action'}: rolled {parts}{mod} = {r['total']}."
    if "target" in r:
        need = f"{r['target']} or {'less' if r['success_if'] == 'at_most' else 'more'}"
        text += f" Needed {need}: {'SUCCESS' if r['success'] else 'FAILURE'}"
        margin = abs(r["total"] - r["target"])
        text += f" (by {margin})." if margin else " (exactly)."
    return text + " Narrate from this result."
