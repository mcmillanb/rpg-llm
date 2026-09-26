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

REQUEST_TOOL = {"type": "function", "function": {
    "name": "request_roll",
    "description": "Ask the player to roll on-screen dice for an uncertain action, then end your "
                   "reply without narrating the outcome. Include the character's modifier in "
                   "dice; the prompt is a few words. Example: dice='d20+5', "
                   "prompt='Roll to attack the goblin', target=13.",
    "parameters": {"type": "object", "properties": {
        "dice": {"type": "string", "description": "e.g. 2D6, 2D6+1, d20+3, d100"},
        "prompt": {"type": "string", "description": "a few words, e.g. 'Roll to attack the goblin'"},
        "target": {"type": "integer", "description": "number to compare against (optional)"},
        "success_if": {"type": "string", "enum": ["at_least", "at_most"],
                       "description": "at_least (default, roll high): total >= target "
                                      "succeeds; at_most only for roll-under systems"},
    }, "required": ["dice", "prompt"]}}}

_DICE = re.compile(r"^\s*(\d*)\s*[dD]\s*(\d+|%)\s*(?:([+-])\s*(\d+))?\s*$")


def parse(dice: str) -> tuple[int, int, int]:
    """'2D6+1' -> (count, sides, modifier). Raises ValueError for anything else."""
    m = _DICE.match(dice or "")
    if not m:
        raise ValueError(f"can't read dice {dice!r}; use a form like 2D6+1")
    count = int(m.group(1) or 1)
    sides = 100 if m.group(2) == "%" else int(m.group(2))
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        raise ValueError("between 1 and 20 dice of 2 to 1000 sides")
    return count, sides, int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)


def notation(count: int, sides: int, mod: int) -> str:
    return f"{count}D{sides}{f'{mod:+d}' if mod else ''}"


def request(args: dict, roll_under_ok: bool = True) -> dict:
    """A validated roll request from the GM's request_roll call (nothing rolled yet).
    roll_under_ok=False forces roll-high, for systems where "at most" is always a mistake."""
    count, sides, mod = parse(str(args.get("dice", "")))
    req = {"id": secrets.token_hex(6), "dice": notation(count, sides, mod),
           "prompt": str(args.get("prompt") or "Roll").strip()[:120]}
    if isinstance(args.get("target"), int):
        req["target"] = args["target"]
        under = args.get("success_if") == "at_most" and roll_under_ok
        req["success_if"] = "at_most" if under else "at_least"
    return req


def roll(dice: str, reason: str = "", target: int | None = None,
         success_if: str = "at_least") -> dict:
    count, sides, mod = parse(dice)
    rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
    total = sum(rolls) + mod
    out = {"dice": notation(count, sides, mod), "reason": reason.strip(),
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


def player_line(r: dict) -> str:
    """The player's turn after an on-screen roll, as the GM reads it."""
    parts = " + ".join(map(str, r["rolls"]))
    mod = f" {'+' if r['modifier'] > 0 else '-'} {abs(r['modifier'])}" if r["modifier"] else ""
    text = f"🎲 {r['reason'] or 'Roll'}: {r['dice']} → {parts}{mod} = {r['total']}"
    if "target" in r:
        text += (f" (needed {r['target']} or {'less' if r['success_if'] == 'at_most' else 'more'}: "
                 f"{'success' if r['success'] else 'failure'})")
    return text


ROLL_UNDER = ("cthulhu", "runequest", "basic roleplaying", "brp", "delta green", "warhammer",
              "gurps", "mothership", "call of")


def roll_under_system(system: str) -> bool:
    """Systems that roll under a skill; everywhere else 'at most' is a model slip."""
    s = (system or "").lower()
    return any(k in s for k in ROLL_UNDER)
