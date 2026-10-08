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
            system[field] = {**old, **v} if isinstance(v, dict) and isinstance(old, dict) else v
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
    rejected = []
    for field, value in list(system.items()):  # names must exist in the rules
        bad = packs.unknown_names(pack, field, value)
        if bad:
            rejected.append((field, bad))
            if isinstance(value, list):
                system[field] = [v for v in value if not any(str(v).startswith(b) for b in bad)]
            else:
                system[field] = (sh.get("system") or {}).get(field, "")
                if packs.unknown_names(pack, field, system[field]):
                    system[field] = ""
    sh["system"] = system
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
    for field, bad in rejected:
        out += (f"\n\nNot recorded: {', '.join(bad)} isn't in the {pack['name']} rules ({field}). "
                "Tell the player, then offer real options from the lists (look them up with "
                "rules_lookup to check).")
    return out


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
