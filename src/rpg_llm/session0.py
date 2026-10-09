"""Session 0: in Rules mode the campaign starts by building the character at the table, as a
real group would: the GM walks the player through the system's character creation (pack
creation.md), filling the sheet with update_character as each choice is made, asks about their
backstory, offers openings that grow from it, and calls finish_session0 to begin the adventure.
"""

import re
import time

from rpg_llm import packs, prompts, sheet
from rpg_llm.vault import Campaign

UPDATE_TOOL = {"type": "function", "function": {
    "name": "update_character",
    "description": "Write choices onto the player's character sheet as they're made in session 0: "
                   "any of the general fields (name, concept, appearance, money, gear, condition, "
                   "assets, companions, loans, obligations), the system's own fields under 'system', "
                   "and counters (Hit Points, Hope, spell slots...) under 'resources'. Only what "
                   "changed; lists replace the old list.",
    "parameters": {"type": "object", "properties": {
        "fields": {"type": "object", "description": "general fields, e.g. {\"name\": \"Brenna\", \"gear\": [\"longsword\", \"shield\"]}"},
        "system": {"type": "object", "description": "the system's fields, e.g. {\"class\": \"Fighter (level 1)\", \"abilities\": {\"STR\": 16}}"},
        "resources": {"type": "array", "description": "counters to set, e.g. [{\"name\": \"Hit Points\", \"current\": 12, \"max\": 12}]",
                      "items": {"type": "object", "properties": {
                          "name": {"type": "string"}, "current": {"type": "integer"},
                          "max": {"type": "integer"}}, "required": ["name", "current", "max"]}},
    }}}}

FINISH_TOOL = {"type": "function", "function": {
    "name": "finish_session0",
    "description": "Session 0 is done: the character is complete and the player has chosen how the "
                   "adventure opens. Then describe the opening scene.",
    "parameters": {"type": "object", "properties": {
        "premise": {"type": "string", "description": "2-4 paragraphs: who the character is, their "
                    "backstory and bonds as the player told them, and the situation the adventure opens on"},
        "appearance": {"type": "string", "description": "how the character looks, 1-2 sentences"},
    }, "required": ["premise"]}}}

TOOLS = [UPDATE_TOOL, FINISH_TOOL]


def active(meta: dict) -> bool:
    return bool(meta.get("session0")) and packs.rules_mode(meta)


def prompt(meta: dict) -> str:
    pack = packs.for_campaign(meta)
    fields = (f"General: {', '.join(sheet.FIELDS)}.\nSystem (under \"system\"): "
              + "; ".join(f"{f['key']} ({f['label']}" + (f": {', '.join(f['names'])}" if f["type"] == "scores" else "")
                          + ")" for f in pack.get("sheet") or [])
              + f".\nCounters (under \"resources\"): {', '.join(r['name'] for r in pack.get('resources') or [])}.")
    return prompts.SESSION0.format(system=pack["name"], creation=pack["creation"], fields=fields)


def dice_note(campaign: Campaign) -> str:
    """For the per-turn notes in session 0: the real dice results so far."""
    totals = rolled(campaign)
    return ("Session 0 dice: results rolled on screen so far: " + ", ".join(map(str, totals)) +
            ". Only these count; never use numbers the player types as rolls.") if totals else ""


def fix_dice(meta: dict, dice: str) -> str:
    """A bare ability-score roll in session 0 keeps the best three (pack 'session0_dice')."""
    pack = packs.for_campaign(meta)
    return ((pack or {}).get("session0_dice") or {}).get(dice.upper().replace(" ", ""), dice)


_norm = lambda k: re.sub(r"[^a-z]", "", str(k).lower())
# names a model reaches for, and the field they mean ("abilities" -> the traits block)
_SCORES = {"abilities", "abilityscores", "scores", "stats", "attributes", "traits", "characteristics"}
_APPEND = {"subclass": "class", "classfeature": "features", "classfeatures": "features",
           "ancestryfeature": "features", "ancestryfeatures": "features", "communityfeature": "features",
           "communityfeatures": "features", "subclassfeature": "features", "foundationfeature": "features",
           "hopefeature": "features", "feature": "features"}


def _route(pack: dict, key: str):
    """Where a key the GM used belongs: ('core', field), ('system', field), ('append', field),
    ('resource', name) or (None, key)."""
    n = _norm(key)
    for f in sheet.FIELDS:
        if n == _norm(f):
            return "core", f
    fields = pack.get("sheet") or []
    for f in fields:
        if n in (_norm(f["key"]), _norm(f["label"]), _norm(f["key"]).rstrip("s"), _norm(f["label"]).rstrip("s")):
            return "system", f["key"]
    if n in _SCORES:
        score = next((f["key"] for f in fields if f["type"] == "scores"), None)
        if score:
            return "system", score
    if n in _APPEND and any(f["key"] == _APPEND[n] for f in fields):
        return "append", _APPEND[n]
    for f in fields:  # "experience" -> experiences, "domaincard" -> domain_cards, "weapon" -> weapons
        if _norm(f["key"]).startswith(n) and len(n) >= 4:
            return "system", f["key"]
    for r in pack.get("resources") or []:
        if n in (_norm(r["name"]), _norm(r["name"]).rstrip("s")):
            return "resource", r["name"]
    return None, key


def update(campaign: Campaign, args: dict, after: int) -> str:
    """Apply update_character; returns what the GM reads back, including anything that didn't
    fit the sheet (so it can try again rather than believe it was recorded)."""
    pack = packs.for_campaign(campaign.meta)
    sh = sheet.load(campaign) or sheet.blank(pack)
    system = dict(sh.get("system") or {})
    res = list(sh.get("resources") or [])
    unknown = []
    given = {**(args.get("fields") or {}), **(args.get("system") or {})}
    for k, v in given.items():
        where, field = _route(pack, k)
        if where == "core":
            if field == "money" and re.fullmatch(r"\s*\d+(\.\d+)?\s*", str(v)):
                v = f"{v} {pack.get('currency', '')}".strip()  # a bare number: keep the unit
            sh[field] = v
        elif where == "system":
            old = system.get(field)
            if isinstance(v, dict) and isinstance(old, dict):
                # "Dex", "dexterity" or "DEX": the sheet's own score names
                names = {n.lower()[:3]: n for n in old}
                v = {names.get(str(k).lower()[:3], k): x for k, x in v.items()}
                system[field] = {**old, **v}
            else:
                system[field] = v
        elif where == "append":
            old = system.get(field)
            if isinstance(old, list):
                add = v if isinstance(v, list) else [v]
                system[field] = old + [x for x in add if x and x not in old]
            elif str(v) and str(v).lower() not in str(old or "").lower():
                system[field] = f"{old} ({v})" if old else str(v)
        elif where == "resource":
            args.setdefault("resources", []).append({"name": field, "current": v, "max": None})
        else:
            unknown.append(k)
    rejected, unconfirmed = [], []
    for field, value in list(system.items()):  # names must exist in the rules
        bad = packs.unknown_names(pack, field, value)
        if bad and pack.get("check_mode") == "warn":  # the player's own book may have it
            unconfirmed.append((field, bad))
            continue
        if bad:
            rejected.append((field, bad))
            if isinstance(value, list):
                system[field] = [v for v in value if not any(str(v).startswith(b) for b in bad)]
            else:
                system[field] = (sh.get("system") or {}).get(field, "")
                if packs.unknown_names(pack, field, system[field]):
                    system[field] = ""
    sh["system"] = system
    # the rules text for what was just chosen, once per choice
    seen = set(campaign.meta.get("session0_explained") or [])
    explained = []
    for field in (pack.get("explain") or {}):
        for value in [(args.get("system") or {}).get(field), (args.get("fields") or {}).get(field)]:
            name = re.split(r"\s*[(:]|\s+[-–]\s+", str(value or ""))[0].strip()
            if name and f"{field}:{name.lower()}" not in seen:
                text = packs.explain(pack, field, name)
                if text:
                    explained.append(text)
                    seen.add(f"{field}:{name.lower()}")
    if explained:
        campaign.save_meta({**campaign.meta, "session0_explained": sorted(seen)})
    for r in args.get("resources") or []:
        name = str(r.get("name") or "").strip()
        old = next((x for x in res if x["name"].lower() == name.lower()), None)
        if old:
            old.update({k: r[k] for k in ("current", "max") if r.get(k) is not None})
        elif name:
            res.append({"name": name, "current": r.get("current", 0), "max": r.get("max", 0)})
    sh["resources"] = res
    saved = sheet.save(campaign, sh, after, "session 0")
    out = "Sheet updated. It now reads:\n" + sheet.notes_block(saved, pack)
    if unknown:
        out += (f"\n\nNot recorded (no such field): {', '.join(unknown)}. The fields are: general "
                f"{', '.join(sheet.FIELDS)}; system {', '.join(f['key'] for f in pack.get('sheet') or [])}.")
    if pack["id"] == "dnd5e2024":
        out += "\n\n" + dnd_numbers(pack, saved, rolled(campaign))
    for text in explained:
        out += "\n\n" + text
    for field, bad in unconfirmed:
        out += (f"\n\nRecorded, but not in the free rules reference: {', '.join(bad)} ({field}). If it's "
                "from the player's own book that's fine: check its details with them.")
    for field, bad in rejected:
        out += (f"\n\nNot recorded: {', '.join(bad)} isn't in the {pack['name']} rules ({field}). "
                "Tell the player, then offer real options from the lists (look them up with "
                "rules_lookup to check).")
    return out


CASTING = {"Cleric": "WIS", "Druid": "WIS", "Ranger": "WIS", "Wizard": "INT", "Bard": "CHA",
           "Paladin": "CHA", "Sorcerer": "CHA", "Warlock": "CHA"}
ABILITY = {"strength": "STR", "dexterity": "DEX", "constitution": "CON", "intelligence": "INT",
           "wisdom": "WIS", "charisma": "CHA"}


def dnd_numbers(pack: dict, sh: dict, rolls: list[int] | None = None) -> str:
    """The numbers the sheet implies, worked out here for the GM to check its own against."""
    system = sh.get("system") or {}
    ab = system.get("abilities") or {}
    mods = {k: (v - 10) // 2 for k, v in ab.items() if v}
    out = "Worked out from the sheet: " + ", ".join(f"{k} {v} ({mods[k]:+d})" for k, v in ab.items() if v)
    if any(v > 20 for v in ab.values()):
        out += ". A score above 20 isn't possible at level 1: recheck the assignment and background bonuses"
    # rolled scores: the final ones are the rolls plus the background's +2/+1 (or +1/+1/+1) on its
    # three abilities only
    bg = packs.entry(pack["id"], "character origins", str(system.get("background") or "").split(" (")[0])
    raised = {ABILITY[w.lower()] for w in re.findall(r"[A-Za-z]+", (re.search(r"Ability Scores:\**\s*([^\n]+)", bg["text"]) or [None, ""])[1])
              if w.lower() in ABILITY} if bg else set()
    if rolls and len(rolls) >= 6 and all(ab.get(k) for k in ("STR", "DEX", "CON", "INT", "WIS", "CHA")):
        extra = sum(ab.values()) - sum(sorted(rolls)[-6:])
        if extra != 3:
            out += (f". The scores add up to {extra:+d} over the six rolled ({', '.join(map(str, sorted(rolls)[-6:]))}); "
                    "the background adds exactly 3 (+2 and +1, or +1 to three)")
        if raised:
            odd = [k for k, v in ab.items() if k not in raised and v not in rolls]
            if odd:
                out += (f". {', '.join(odd)} isn't one of the rolls, and {bg['title']} only raises "
                        f"{', '.join(sorted(raised))}")
    cls = re.split(r"\s*[(:]|\s+", str(system.get("class") or ""))[0]
    e = packs.entry(pack["id"], "classes", cls) if cls else None
    die = re.search(r"Hit Point Die\s*\|\s*D(\d+)", e["text"]) if e else None
    if die and "CON" in mods:
        hp = int(die.group(1)) + mods["CON"]
        out += (f". Level 1 Hit Points for a {e['title']}: d{die.group(1)} maximum {die.group(1)} + CON "
                f"{mods['CON']:+d} = {hp}, plus any species or feat bonus (a Dwarf's Dwarven Toughness: +1)")
    if "STR" in mods and "DEX" in mods:
        out += (f". Weapon attacks with a proficient weapon: melee {mods['STR'] + 2:+d} to hit, damage die "
                f"{mods['STR']:+d}; finesse or ranged {mods['DEX'] + 2:+d} to hit, damage die {mods['DEX']:+d}")
    cast = CASTING.get(e["title"]) if e else None
    if cast and cast in mods:
        out += (f". {e['title']} spellcasting ({cast}): spell save DC 8 + {mods[cast]} + 2 = {10 + mods[cast]}, "
                f"spell attack {mods[cast] + 2:+d}")
    return out + "."


def rolled(campaign: Campaign) -> list[int]:
    """Totals of the dice the player actually rolled during session 0 (the only ones that count)."""
    return [m["roll"]["total"] for m in campaign.messages() if m.get("roll")]


def missing(campaign: Campaign) -> list[str]:
    """Required sheet fields (pack.yaml 'required') that are still empty."""
    pack = packs.for_campaign(campaign.meta)
    sh = sheet.load(campaign) or {}
    system = sh.get("system") or {}
    labels = {f["key"]: f["label"] for f in pack.get("sheet") or []}
    out = []
    for k in pack.get("required") or []:
        v = system.get(k)
        if v in (None, "", 0, []) or (isinstance(v, dict) and not any(v.values())):
            out.append(labels.get(k, k))
    return out


def finish(campaign: Campaign, args: dict, after: int) -> str | None:
    """Close session 0: the premise and brief from the player's own story; the arc follows.
    Returns None (not finished) with the gaps in args['_refusal'] if the sheet isn't complete;
    a second try goes through regardless, so it can't loop."""
    gaps = missing(campaign)
    if gaps and not campaign.meta.get("session0_nudged"):
        campaign.save_meta({**campaign.meta, "session0_nudged": True})
        args["_refusal"] = (f"Not finished yet: the sheet still needs {', '.join(gaps)}. Work them out "
                            "with the player (or from what's already agreed), record them with "
                            "update_character, then call finish_session0 again.")
        return None
    premise = str(args.get("premise") or "").strip()
    meta = {**campaign.meta, "session0": False, "premise": premise or campaign.meta.get("premise", ""),
            "session0_done": time.time()}
    if args.get("appearance"):
        meta["appearance"] = str(args["appearance"]).strip()
        sh = sheet.load(campaign)
        if sh:
            sheet.save(campaign, {**sh, "appearance": meta["appearance"]}, after, "session 0")
    campaign.save_meta(meta)
    campaign.write("brief.md", f"# {meta.get('name')}\n\n## Premise\n\n{premise or '(not set)'}\n")
    return ("Session 0 is complete and the sheet is saved. Now begin the adventure: describe the "
            "opening scene the player chose, in play, and end where they can act.")
