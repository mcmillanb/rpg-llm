"""Real dice for the GM: the model asks, the app rolls."""

import hashlib
import re
import secrets

TOOL = {"type": "function", "function": {
    "name": "roll_dice",
    "description": "Roll dice for an uncertain action. The app rolls real dice and returns the "
                   "result; narrate from it. Examples: dice='2D6+1', target=8 (need 8 or more); "
                   "dice='d100', target=45, success_if='at_most' (roll under a skill).",
    "parameters": {"type": "object", "properties": {
        "dice": {"type": "string", "description": "e.g. 2D6, 2D6+1, d20-2, 2D20KH1+5 (advantage), 4D6KH3, d100"},
        "reason": {"type": "string", "description": "what the roll is for, a few words"},
        "target": {"type": "integer", "description": "number to compare against (optional)"},
        "success_if": {"type": "string", "enum": ["at_least", "at_most"],
                       "description": "at_least (default): total >= target succeeds"},
    }, "required": ["dice", "reason"]}}}

REQUEST_TOOL = {"type": "function", "function": {
    "name": "request_roll",
    "description": "Ask the player to roll on-screen dice for an uncertain action or for damage, then end your "
                   "reply without narrating the outcome. Include the character's modifier in "
                   "dice; the prompt is a few words. Call it as a tool: never write its "
                   "arguments into the story.",
    "parameters": {"type": "object", "properties": {
        "dice": {"type": "string", "description": "e.g. 2D6+1, d20+3, 2D20KH1+3 (advantage), 2D20KL1+3 (disadvantage), 2D12+2 (Daggerheart), d100"},
        "prompt": {"type": "string", "description": "a few words, e.g. 'Roll to attack the goblin'"},
        "target": {"type": "integer", "description": "number to compare against (optional)"},
        "success_if": {"type": "string", "enum": ["at_least", "at_most"],
                       "description": "at_least (default, roll high): total >= target "
                                      "succeeds; at_most only for roll-under systems"},
    }, "required": ["dice", "prompt"]}}}

# Dice notation: terms added or subtracted, e.g. "2D6+1", "d20+5", "2D20KH1+5" (advantage: roll
# two, keep the higher), "2D20KL1" (disadvantage), "4D6KH3" (ability scores), "2D12+1D6+2"
# (Daggerheart with advantage), "d100".
_TERM = re.compile(r"\s*([+-]?)\s*(?:(\d*)\s*[dD]\s*(\d+|%)(?:\s*[kK]\s*([hHlL])\s*(\d+))?|(\d+))\s*")


def terms(dice: str) -> tuple[list[dict], int]:
    """'2D20KH1+5' -> ([{count: 2, sides: 20, keep: ('h', 1), sign: 1}], 5). Raises ValueError."""
    text = (dice or "").strip()
    pos, out, mod, n_dice = 0, [], 0, 0
    while pos < len(text):
        m = _TERM.match(text, pos)
        if not m or m.end() == pos or (pos and not m.group(1)):
            raise ValueError(f"can't read dice {dice!r}; use a form like 2D6+1 or 2D20KH1+5")
        sign = -1 if m.group(1) == "-" else 1
        if m.group(6) is not None:
            mod += sign * int(m.group(6))
        else:
            count = int(m.group(2) or 1)
            sides = 100 if m.group(3) == "%" else int(m.group(3))
            keep = (m.group(4).lower(), int(m.group(5))) if m.group(4) else None
            if not (1 <= count <= 20 and 2 <= sides <= 1000) or (keep and not 1 <= keep[1] <= count):
                raise ValueError("between 1 and 20 dice of 2 to 1000 sides, keeping no more than rolled")
            n_dice += count
            out.append({"count": count, "sides": sides, "keep": keep, "sign": sign})
        pos = m.end()
    if not out or n_dice > 24:
        raise ValueError(f"can't read dice {dice!r}; use a form like 2D6+1")
    return out, mod


def canon(dice: str) -> str:
    """The notation written the standard way: 'd20 + 5' -> '1D20+5'."""
    ts, mod = terms(dice)
    text = ""
    for t in ts:
        keep = f"K{t['keep'][0].upper()}{t['keep'][1]}" if t["keep"] else ""
        text += f"{'-' if t['sign'] < 0 else '+' if text else ''}{t['count']}D{t['sides']}{keep}"
    return text + (f"{mod:+d}" if mod else "")


def parse(dice: str) -> tuple[int, int, int]:
    """Single-term dice only: '2D6+1' -> (count, sides, modifier)."""
    ts, mod = terms(dice)
    if len(ts) != 1 or ts[0]["keep"] or ts[0]["sign"] < 0:
        raise ValueError("not a single plain dice term")
    return ts[0]["count"], ts[0]["sides"], mod


def notation(count: int, sides: int, mod: int) -> str:
    return f"{count}D{sides}{f'{mod:+d}' if mod else ''}"


def request(args: dict, roll_under_ok: bool = True) -> dict:
    """A validated roll request from the GM's request_roll call (nothing rolled yet).
    roll_under_ok=False forces roll-high, for systems where "at most" is always a mistake."""
    req = {"id": secrets.token_hex(6), "dice": canon(str(args.get("dice", ""))),
           "prompt": str(args.get("prompt") or "Roll").strip()[:120]}
    if isinstance(args.get("target"), int):
        req["target"] = args["target"]
        under = args.get("success_if") == "at_most" and roll_under_ok
        req["success_if"] = "at_most" if under else "at_least"
    return req


def roll(dice: str, reason: str = "", target: int | None = None,
         success_if: str = "at_least", duality: bool = False) -> dict:
    """Roll it. `duality`: Daggerheart, where a roll starting 2D12 is the Hope die and the Fear
    die: which is higher decides "with Hope" or "with Fear", and matching dice are a critical."""
    ts, mod = terms(dice)
    rolls, kept, parts, total = [], [], [], mod
    for t in ts:
        r = [secrets.randbelow(t["sides"]) + 1 for _ in range(t["count"])]
        keep = [True] * len(r)
        if t["keep"]:
            order = sorted(range(len(r)), key=lambda i: r[i], reverse=t["keep"][0] == "h")
            keep = [i in order[:t["keep"][1]] for i in range(len(r))]
        total += t["sign"] * sum(v for v, k in zip(r, keep) if k)
        rolls += r
        kept += keep
        parts.append({"sides": t["sides"], "rolls": r, "kept": keep, "sign": t["sign"]})
    out = {"dice": canon(dice), "reason": reason.strip(), "rolls": rolls, "modifier": mod,
           "total": total, "sides": [p["sides"] for p in parts for _ in p["rolls"]]}
    if not all(kept):
        out["kept"] = kept
    if any(p["sign"] < 0 for p in parts):
        out["signs"] = [p["sign"] for p in parts for _ in p["rolls"]]
    if duality and ts[0]["count"] == 2 and ts[0]["sides"] == 12 and not ts[0]["keep"] and ts[0]["sign"] > 0:
        hope, fear = parts[0]["rolls"]
        out["duality"] = {"hope": hope, "fear": fear,
                          "outcome": "critical" if hope == fear else "with Hope" if hope > fear else "with Fear"}
    if target is not None:
        ok = total <= target if success_if == "at_most" else total >= target
        if out.get("duality", {}).get("outcome") == "critical":
            ok = True  # matching Duality Dice always succeed
        out.update(target=target, success_if=success_if, success=ok)
    return out


def _dice_text(r: dict) -> str:
    """'17 + [6]' (a dropped die in brackets), or 'Hope 7, Fear 11' for Duality Dice."""
    kept = r.get("kept") or [True] * len(r["rolls"])
    signs = r.get("signs") or [1] * len(r["rolls"])
    items = [(f"{v}" if k else f"[{v}]", sg) for v, k, sg in zip(r["rolls"], kept, signs)]
    if r.get("duality"):
        head, items = f"Hope {r['duality']['hope']}, Fear {r['duality']['fear']}", items[2:]
    else:
        (first, sg), items = items[0], items[1:]
        head = first if sg > 0 else f"-{first}"
    return head + "".join(f" {'+' if sg > 0 else '-'} {x}" for x, sg in items)


def _duality_note(r: dict) -> str:
    d = r.get("duality")
    if not d:
        return ""
    return {"critical": " Critical (matching dice): a success whatever the total; the player gains 1 Hope and clears 1 Stress.",
            "with Hope": " With Hope: the player gains 1 Hope.",
            "with Fear": " With Fear: the GM gains 1 Fear, and on a failure the GM makes a move."}[d["outcome"]]


def describe(r: dict) -> str:
    """What the GM reads back."""
    mod = f" {'+' if r['modifier'] > 0 else '-'} {abs(r['modifier'])}" if r["modifier"] else ""
    text = f"{r['dice']} for {r['reason'] or 'the action'}: rolled {_dice_text(r)}{mod} = {r['total']}."
    if "target" in r:
        need = f"{r['target']} or {'less' if r['success_if'] == 'at_most' else 'more'}"
        text += f" Needed {need}: {'SUCCESS' if r['success'] else 'FAILURE'}"
        margin = abs(r["total"] - r["target"])
        text += f" (by {margin})." if margin else " (exactly)."
    return text + _duality_note(r) + " Narrate from this result."


def player_line(r: dict) -> str:
    """The player's turn after an on-screen roll, as the GM reads it."""
    mod = f" {'+' if r['modifier'] > 0 else '-'} {abs(r['modifier'])}" if r["modifier"] else ""
    text = f"🎲 {r['reason'] or 'Roll'}: {r['dice']} → {_dice_text(r)}{mod} = {r['total']}"
    if "target" in r:
        text += (f" (needed {r['target']} or {'less' if r['success_if'] == 'at_most' else 'more'}: "
                 f"{'success' if r['success'] else 'failure'})")
    if r.get("duality"):
        text += f", {r['duality']['outcome']}." + _duality_note(r)
    return text


ROLL_UNDER = ("cthulhu", "runequest", "basic roleplaying", "brp", "delta green", "warhammer",
              "gurps", "mothership", "call of")


def roll_under_system(system: str) -> bool:
    """Systems that roll under a skill; everywhere else 'at most' is a model slip."""
    s = (system or "").lower()
    return any(k in s for k in ROLL_UNDER)


_TEXT_REQUEST = re.compile(
    r"\broll\s+(?:a\s+|an\s+)?(\d*\s*d\s*(?:\d+|%)(?:\s*[+-]\s*\d+)?)"  # dice
    r"\s*(?:\(([^)]*)\))?"                                                  # (Target 8+)
    r"(?:\s*(?:to|for)\s+([^.\n]{3,200}))?", re.I)


# The GM pasted the tool's arguments into its reply instead of calling it, e.g.
#   dice='2D6+2', prompt='Roll to fit the valve', target=8
#   request_roll(dice="d20+3", prompt="Roll to climb")   or   {"dice": "2D6", "target": 8}
_ARGS_LINE = re.compile(r"""^[ \t>*`]*(?:request_roll\s*\(?\s*)?\{?\s*["']?dice["']?\s*[=:]\s*"""
                        r"""["']([^"'\n]+)["'][^\n]*$\n?""", re.M | re.I)


def _arg(line: str, name: str) -> str | None:
    m = re.search(rf"""["']?{name}["']?\s*[=:]\s*(?:["']([^"'\n]*)["']|(\d+))""", line, re.I)
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


def request_from_args_text(text: str, key: str) -> tuple[dict, str] | None:
    """Fallback for a GM that writes the request_roll arguments as text. Returns the request and
    the reply with that line taken out."""
    matches = list(_ARGS_LINE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    line = m.group(0)
    args = {"dice": m.group(1), "prompt": _arg(line, "prompt") or ""}
    target = _arg(line, "target")
    if target and target.isdigit():
        args["target"] = int(target)
    if (_arg(line, "success_if") or "") == "at_most":
        args["success_if"] = "at_most"
    try:
        req = request(args)
    except ValueError:
        return None
    req["id"] = "t" + hashlib.sha1(f"{key}:{line}".encode()).hexdigest()[:11]
    req["from_text"] = True
    if not req["prompt"] or req["prompt"] == "Roll":
        req["prompt"] = f"Roll {req['dice']}"
    cleaned = (text[:m.start()] + text[m.end():]).strip()
    return req, cleaned


def request_from_text(text: str, key: str) -> dict | None:
    """Fallback for a GM that writes 'Roll 2D6+1 (Target 8+) to break the concrete' instead of
    calling request_roll: build the same request from its words. `key` makes the id stable."""
    matches = list(_TEXT_REQUEST.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    try:
        dice_text = canon(re.sub(r"\s+", "", m.group(1)))
    except ValueError:
        return None
    req = {"id": "t" + hashlib.sha1(f"{key}:{m.group(0)}".encode()).hexdigest()[:11],
           "dice": dice_text, "from_text": True}
    goal = (m.group(3) or "").strip().rstrip(",;:")
    if len(goal) > 70:  # keep it short, cut at a word
        goal = goal[:70].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    req["prompt"] = f"Roll to {goal}" if goal else f"Roll {req['dice']}"
    target = re.search(r"(\d+)", m.group(2) or "")
    if target:
        req["target"] = int(target.group(1))
        req["success_if"] = "at_most" if re.search(r"or less|or under|at most|below", m.group(2) or "", re.I) \
            else "at_least"
    return req


# The GM asked for a roll in words only ("Roll to snag the canister."), with no dice at all.
_ASK = re.compile(r"^\W*(?:please\s+|now,?\s+|go ahead and\s+)?(?:roll\b|make\s+an?\b.{0,40}\b(?:roll|check|"
                  r"test|save)\b|give me\s+an?\b.{0,40}\b(?:roll|check)\b|time for\s+an?\b.{0,40}\broll\b)",
                  re.I)


def asked_roll(text: str) -> str | None:
    """The closing paragraph, if it asks the player to roll: 'Roll to snag the canister.'"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    if not paras:
        return None
    last = paras[-1].strip("*_ ")
    return last if len(last) <= 250 and _ASK.search(last) else None


def standard_dice(system: str) -> dict:
    """The system's usual task roll, for when the GM didn't say which dice."""
    from rpg_llm import packs
    pack = packs.match(system)
    if pack and pack.get("check"):
        return {"dice": pack["check"]}
    s = (system or "").lower()
    if any(k in s for k in ("traveller", "cepheus", "2d6")):
        return {"dice": "2D6", "target": 8, "success_if": "at_least"}
    if "gurps" in s:
        return {"dice": "3D6"}
    if roll_under_system(s):
        return {"dice": "1D100"}
    if any(k in s for k in ("blades", "forged", "powered by the apocalypse", "apocalypse", "dungeon world")):
        return {"dice": "2D6"}
    return {"dice": "1D20"}


def standard_request(ask: str, system: str, key: str) -> dict:
    """A request built from the GM's words and the system's usual roll."""
    goal = re.sub(r"\s+", " ", ask).strip().rstrip(".!:")
    if len(goal) > 80:
        goal = goal[:80].rsplit(" ", 1)[0] + "…"
    req = {"id": "t" + hashlib.sha1(f"{key}:{ask}".encode()).hexdigest()[:11],
           "prompt": goal if goal.lower().startswith("roll") else f"Roll: {goal}",
           "from_text": True, "standard": True, **standard_dice(system)}
    return req


# Qwen sometimes writes its tool call into the reply as text instead of making it:
#   <tool_call> <function=rules_lookup> <parameter=query> Deft Deceiver </parameter> </function> </tool_call>
_XML_CALL = re.compile(r"<tool_call>\s*<function=([\w.-]+)>(.*?)</function>\s*(?:</tool_call>)?", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)>(.*?)</parameter>", re.S)


def tool_calls_from_text(text: str) -> tuple[list[dict], str]:
    """Tool calls written as text, as real calls, and the text without them."""
    import json as _json
    calls = []
    for i, m in enumerate(_XML_CALL.finditer(text or "")):
        args = {}
        for p in _XML_PARAM.finditer(m.group(2)):
            raw = p.group(2).strip()
            try:
                args[p.group(1)] = _json.loads(raw)
            except ValueError:
                args[p.group(1)] = raw
        calls.append({"id": f"text_{i}", "name": m.group(1), "arguments": _json.dumps(args)})
    cleaned = _XML_CALL.sub("", text or "")
    cleaned = re.sub(r"</?tool_call>", "", cleaned).strip()
    return calls, cleaned
