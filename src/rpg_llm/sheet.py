"""Character sheet: the player character's mechanical state (money, gear, injuries, companions,
debts) as structured data rather than prose.

The router updates it in the background after each GM reply; the player can edit it; it rides
in the per-turn GM notes (the end of the prompt), so frequent changes never disturb the prompt
cache. Every version is snapshotted against the transcript, so rewinding a turn also rewinds
what that turn did to the sheet.
"""

import json
import re
import time

import yaml

from rpg_llm import packs, prompts
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign

FILE = "character.yaml"
HISTORY = "character.history.jsonl"
LISTS = ("skills", "condition", "gear", "assets", "companions", "loans", "obligations")
TEXTS = ("name", "concept", "appearance", "money")
FIELDS = ("name", "concept", "appearance", "skills", "condition", "money", "loans", "gear",
          "assets", "companions", "obligations")

SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "string"} for k in TEXTS},
        **{k: {"type": "array", "items": {"type": "string"}} for k in LISTS},
    },
    "required": list(FIELDS),
    "additionalProperties": False,
}

PAYMENT = {
    "type": "object",
    "properties": {"amount": {"type": "number"}, "direction": {"type": "string", "enum": ["in", "out"]},
                   "what": {"type": "string"}},
    "required": ["amount", "direction", "what"],
    "additionalProperties": False,
}
UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {"type": "array", "items": {"type": "string"}},
        "changed": {"type": "boolean"},
        "payments": {"type": "array", "items": PAYMENT},
        "sheet": SHEET_SCHEMA,
    },
    "required": ["changes", "changed", "payments", "sheet"],
    "additionalProperties": False,
}


RESOURCE_CHANGE = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "change": {"type": "integer"},
                   "set": {"type": ["integer", "null"]}, "max": {"type": ["integer", "null"]}},
    "required": ["name", "change", "set", "max"],
    "additionalProperties": False,
}


def blank(pack: dict | None = None) -> dict:
    out = {k: ([] if k in LISTS else "") for k in FIELDS}
    if pack:  # a Rules-mode sheet: the system's own fields and counters, at their defaults
        out["money"] = pack.get("start_money") or f"0 {pack.get('currency', '')}".strip()
        out["system"] = clean_system({f["key"]: f["default"] for f in pack.get("sheet") or []
                                      if "default" in f}, pack)
        out["resources"] = clean_resources(pack.get("resources") or [])
    return out


def _int(v) -> int:
    m = re.search(r"-?\d+", str(v if v is not None else ""))
    return int(m.group()) if m else 0


def clean_system(data: dict, pack: dict) -> dict:
    """The system's own sheet fields (pack.yaml 'sheet'), each with the right type."""
    out = {}
    for f in pack.get("sheet") or []:
        v = (data or {}).get(f["key"])
        if f["type"] == "number":
            out[f["key"]] = _int(v)
        elif f["type"] == "list":
            items = v if isinstance(v, list) else ([v] if v else [])
            out[f["key"]] = [str(i).strip() for i in items if str(i).strip()][:20]
        elif f["type"] == "scores":
            got = v if isinstance(v, dict) else {}
            low = {str(k).lower()[:3]: val for k, val in got.items()}
            out[f["key"]] = {n: _int(got.get(n, low.get(n.lower()[:3]))) for n in f["names"]}
        else:
            out[f["key"]] = str(v or "").strip()[:300]
    return out


def clean_resources(items: list) -> list[dict]:
    """Counters: [{name, current, max}] (max 0 = no maximum); 'marks' counters (Stress) count up."""
    out = []
    for r in items or []:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        mx = max(0, _int(r.get("max")))
        cur = _int(r.get("current", mx if not r.get("marks") else 0))
        cur = max(0, min(cur, mx)) if mx else max(0, cur)
        item = {"name": str(r["name"]).strip()[:40], "current": cur, "max": mx}
        if r.get("marks"):
            item["marks"] = True
        out.append(item)
    return out[:16]


def apply_resources(resources: list[dict], changes: list[dict]) -> list[dict]:
    """Counters after the reported changes, worked out here: 'Hit Points -8' takes 8 off,
    clamped between 0 and the maximum. A change to a counter not on the sheet adds it."""
    out = [dict(r) for r in resources or []]
    for ch in changes or []:
        name = str(ch.get("name") or "").strip()
        if not name:
            continue
        r = next((x for x in out if x["name"].lower() == name.lower()), None) or \
            next((x for x in out if x["name"].lower().startswith(name.lower()[:5])), None)
        if r is None:
            if ch.get("set") is None and ch.get("max") is None:
                continue
            r = {"name": name[:40], "current": 0, "max": 0}
            out.append(r)
        if ch.get("max") is not None:
            r["max"] = max(0, int(ch["max"]))
        if ch.get("set") is not None:
            r["current"] = int(ch["set"])
        r["current"] += int(ch.get("change") or 0)
        r["current"] = max(0, min(r["current"], r["max"])) if r["max"] else max(0, r["current"])
    return out


def resources_text(resources: list[dict]) -> str:
    return "; ".join(f"{r['name']} {'marked ' if r.get('marks') else ''}{r['current']}"
                     + (f"/{r['max']}" if r["max"] else "") for r in resources or [])


def clean(data: dict, pack: dict | None = None) -> dict:
    """Keep only known fields, in order, with the right types; drop empty list entries."""
    out = blank()
    for k in FIELDS:
        v = data.get(k)
        if k in LISTS:
            items = v if isinstance(v, list) else ([v] if v else [])
            out[k] = [str(i).strip() for i in items if str(i).strip()]
        else:
            out[k] = str(v or "").strip()
    if pack or data.get("system"):
        out["system"] = clean_system(data.get("system") or {}, pack) if pack else data["system"]
    if data.get("resources") or pack:
        out["resources"] = clean_resources(data.get("resources") or [])
    return out


def update_schema(pack: dict | None) -> dict:
    """The update's JSON shape: counters always as changes; the system's fields when in Rules mode."""
    sheet_schema = SHEET_SCHEMA
    if pack:
        props = {}
        for f in pack.get("sheet") or []:
            props[f["key"]] = ({"type": "integer"} if f["type"] == "number" else
                               {"type": "array", "items": {"type": "string"}} if f["type"] == "list" else
                               {"type": "object", "properties": {n: {"type": "integer"} for n in f["names"]},
                                "required": f["names"], "additionalProperties": False}
                               if f["type"] == "scores" else {"type": "string"})
        sheet_schema = {**SHEET_SCHEMA,
                        "properties": {**SHEET_SCHEMA["properties"],
                                       "system": {"type": "object", "properties": props,
                                                  "required": list(props), "additionalProperties": False}},
                        "required": [*SHEET_SCHEMA["required"], "system"]}
    return {**UPDATE_SCHEMA,
            "properties": {**UPDATE_SCHEMA["properties"], "sheet": sheet_schema,
                           "resources": {"type": "array", "items": RESOURCE_CHANGE}},
            "required": [*UPDATE_SCHEMA["required"], "resources"]}


def system_text(pack: dict | None, current: dict | None) -> str:
    """For the update prompt: the system's fields and the counters."""
    out = []
    if pack:
        out.append(f"This is a {pack['name']} character. The sheet's \"system\" holds its own "
                   "fields; keep them to what the story establishes:")
        out += [f"- {f['key']}: {f['label']}" for f in pack.get("sheet") or []]
    res = (current or {}).get("resources") or []
    out.append("Counters (" + (resources_text(res) or "none yet") + ") are changed only through "
               "\"resources\": [{name, change, set, max}]: change is +/- (damage taken: Hit Points "
               "-8; 1 Hope spent: Hope -1; Stress marked: Stress +1), set/max only to set a value "
               "outright (null otherwise). Don't do the arithmetic, and leave the sheet's own "
               "counter list as it is.")
    return "\n".join(out)


MAX_ITEMS = 12
MAX_ENTRY = 160
_THINKING = re.compile(r"\b(wait|re-?read|actually|i will|i'll|let me|however|hmm|so no)\b|\?", re.I)


def tidy_changes(changes: list) -> list[str]:
    """Keep the change log to short notes. A small model sometimes pours its reasoning into this
    field ('Wait, re-reading the GM...'); drop anything that reads like thinking or runs long."""
    out = []
    for c in changes:
        c = " ".join(str(c).split())
        if c and len(c) <= 120 and not _THINKING.search(c):
            out.append(c)
    return out[:6]


def amount(money: str) -> float | None:
    m = re.search(r"-\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?", money or "")
    return float(m.group().replace(",", "").replace(" ", "")) if m else None


def apply_payments(money: str, payments: list[dict]) -> str | None:
    """The new money string: the old amount plus what came in, minus what went out, in the old
    string's format ("Cr 450", "1,204 gp"). None if the old money has no number to add to.
    The model reports payments; the adding up is done here, not by the model."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", money or "")
    if not m:
        return None
    total = float(m.group().replace(",", ""))
    for p in payments:
        try:
            amt = abs(float(p.get("amount") or 0))
        except (TypeError, ValueError):
            continue
        total += amt if p.get("direction") == "in" else -amt
    decimals = "." in m.group() or total != int(total)
    text = f"{total:,.2f}" if decimals else f"{int(total):,}"
    if "," not in m.group() and abs(total) < 10000:
        text = text.replace(",", "")
    return money[:m.start()] + text + money[m.end():]


def guard(old: dict | None, new: dict) -> dict:
    """Protect the sheet from a model's slips: a known amount of money never turns into one
    without a number or goes below zero (the character can't spend money they don't have: a
    negative balance is a misread, like '1,800' for 180), and lists stay short. Returns the
    sheet; a refused money change is reported in new['_refused']."""
    if old and re.search(r"\d", old.get("money", "")) and not re.search(r"\d", new.get("money", "")):
        new["money"] = old["money"]
    was, now = amount(old.get("money", "")) if old else None, amount(new.get("money", ""))
    if was is not None and now is not None and now < 0 <= was:
        new["_refused"] = new["money"]
        new["money"] = old["money"]
    for k in LISTS:
        new[k] = [e if len(e) <= MAX_ENTRY else e[:MAX_ENTRY - 1] + "…" for e in new[k][:MAX_ITEMS]]
    return new


def load(campaign: Campaign) -> dict | None:
    text = campaign.read(FILE)
    if not text.strip():
        return None
    pack = packs.for_campaign(campaign.meta) if packs.rules_mode(campaign.meta) else None
    return clean(yaml.safe_load(text) or {}, pack)


def save(campaign: Campaign, sheet: dict, after: int, what: str) -> dict:
    pack = packs.for_campaign(campaign.meta) if packs.rules_mode(campaign.meta) else None
    sheet = clean(sheet, pack)
    campaign.write(FILE, yaml.safe_dump(sheet, sort_keys=False, allow_unicode=True, width=100))
    with campaign.path(HISTORY).open("a") as f:
        f.write(json.dumps({"after": after, "ts": time.time(), "what": what, "sheet": sheet},
                           ensure_ascii=False) + "\n")
    return sheet


def history(campaign: Campaign) -> list[dict]:
    p = campaign.path(HISTORY)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def notes_block(sheet: dict | None, pack: dict | None = None) -> str:
    if not sheet:
        return ""
    lines = [f"## Character sheet (current; keep to it)"]
    for k in FIELDS:
        v = sheet.get(k)
        if not v:
            continue
        label = k.replace("_", " ").capitalize()
        lines.append(f"{label}: {'; '.join(v) if isinstance(v, list) else v}")
    labels = {f["key"]: f["label"] for f in (pack or {}).get("sheet") or []}
    for k, v in (sheet.get("system") or {}).items():
        if v in ("", 0, [], None) or (isinstance(v, dict) and not any(v.values())):
            continue
        shown = ("; ".join(v) if isinstance(v, list) else
                 ", ".join(f"{n} {x:+d}" if (pack and any(f.get("mods") == "plain" for f in pack["sheet"] if f["key"] == k)) else f"{n} {x}"
                           for n, x in v.items()) if isinstance(v, dict) else v)
        lines.append(f"{labels.get(k, k)}: {shown}")
    if sheet.get("resources"):
        lines.append(f"Counters: {resources_text(sheet['resources'])}")
    return "\n".join(lines)


PLAYER_EDIT = "edited by the player"


def rewind(campaign: Campaign, first_dead_id: int) -> bool:
    """A turn from first_dead_id on was taken back: restore the sheet as it was before it.
    A correction the player made by hand since then isn't something the turn did: it stays."""
    hist = history(campaign)
    if not hist or hist[-1]["after"] < first_dead_id:
        return False
    keep = [h for h in hist if h["after"] < first_dead_id]
    edits = [h for h in hist if h["after"] >= first_dead_id and h["what"] == PLAYER_EDIT]
    if edits:
        keep.append({**edits[-1], "after": first_dead_id - 1})
    if keep:
        campaign.write(FILE, yaml.safe_dump(keep[-1]["sheet"], sort_keys=False,
                                            allow_unicode=True, width=100))
    else:
        campaign.path(FILE).unlink(missing_ok=True)
    campaign.path(HISTORY).write_text(
        "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in keep))
    return True


SYSTEM = "You keep the character records for a solo tabletop role-playing campaign."
# Money is only touched when the exchange talks about money at all: a reply about something
# else can't re-apply an earlier payment.
MONEY_WORDS = re.compile(
    r"\b(credits?|cr|cash|coins?|gold|silver|copper|gp|sp|cp|dollars?|pounds?|quid|marks?|"
    r"crowns?|shillings?|pence|thalers?|scrip|money|funds|pay|paid|pays|paying|payment|price|"
    r"cost|costs|fee|fees|bill|rent|rental|wage|wages|reward|bribe|change|debt|loan|owe|owed|"
    r"buy|bought|sell|sold|purchase|tip)\b|[$£€¥]", re.I)


async def update(campaign: Campaign, router: LLMClient, router_system: str = "") -> dict | None:
    """After a GM reply: apply what the latest exchange concretely changed. Creates the sheet
    from the brief the first time. Returns {"changes": [...]} if something changed.

    The model sees only the sheet and the exchange (plus the brief when there's no sheet yet):
    with the campaign brief and wiki index in front of it, the model (9B or 27B) mixed their
    money figures and story into the update (a quote booked as a payment, an earlier payment
    applied again). `router_system` is accepted for older callers and ignored."""
    messages = campaign.messages()
    if len(messages) < 2 or messages[-1]["role"] != "assistant":
        return None
    current = load(campaign)
    pack = packs.for_campaign(campaign.meta) if packs.rules_mode(campaign.meta) else None
    schema = update_schema(pack)
    fmt = lambda ms: "\n\n".join(f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {m['content'][:3000]}"
                                  for m in ms)
    exchange = fmt(messages[-2:])
    # the exchange before, for context: a correction ("that's 211, not 200") only makes sense
    # next to what it corrects
    earlier = (f"\nPrevious exchange (already on the sheet; context only, don't apply it again):\n"
               f"{fmt(messages[-4:-2])}\n" if len(messages) >= 4 else "")
    task = prompts.SHEET_TASK.format(
        sheet=yaml.safe_dump(current, sort_keys=False, allow_unicode=True) if current
        else "(no sheet yet: create it from the brief and this exchange)",
        exchange=exchange, earlier=earlier, system=system_text(pack, current))
    system = SYSTEM if current else f"{SYSTEM}\n\nCampaign brief:\n{campaign.brief.strip()}"
    ask = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    rechecked = None
    money_talk = bool(MONEY_WORDS.search(exchange))

    def read(res: dict) -> dict:
        sh = clean(res.get("sheet") or {}, pack)
        if current:  # counters: changes applied here, never the model's own list
            changed = apply_resources(current.get("resources") or [], res.get("resources") or [])
            if changed or "resources" in current:
                sh["resources"] = changed
        if current:  # money = the old amount plus the reported payments, added up here
            computed = apply_payments(current["money"], res.get("payments") or [] if money_talk else [])
            if computed is not None:
                sh["money"] = computed
        return guard(current, sh)

    result = await router.json(ask, schema, max_tokens=1800)
    new = read(result)
    if new.get("_refused"):  # below zero: one more look before giving up on the change
        again = await router.json(ask + [
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
            {"role": "user", "content": prompts.SHEET_RECHECK.format(
                new=new["_refused"], old=current["money"],
                history=fmt(messages[-12:-4]) or "(none)")}], schema, max_tokens=1800)
        second = read(again)
        if not second.get("_refused"):
            # accepted, but say so: the first reading was impossible, the second may be a guess
            rechecked = f"⚠ first read gave {new['_refused']}; rechecked to {second['money']}, check it"
            result, new = again, second
    if campaign.messages()[-1]["id"] != messages[-1]["id"]:
        return None  # the turn was taken back meanwhile
    refused = new.pop("_refused", None)
    flag = (f"⚠ money would have gone to {refused}: left at {current['money']}, check it"
            if refused else None)
    if current is not None and (not result.get("changed") or new == current):
        return {"changes": [flag], "refused": refused, "money": current["money"]} if refused else None
    if not new.get("name") and current is None:
        return None  # nothing usable yet
    changes = tidy_changes(result.get("changes") or [])
    if flag or rechecked:
        changes.append(flag or rechecked)
    what = "; ".join(changes) or ("created" if current is None else "updated")
    save(campaign, new, messages[-1]["id"], what)
    out = {"changes": changes or [what]}
    if refused:
        out.update(refused=refused, money=new["money"])
    return out


def money_note(refused: dict | None) -> str:
    """For the next turn's GM notes, after a payment the sheet couldn't accept."""
    if not refused:
        return ""
    return prompts.MONEY_NOTE.format(what=f"the sheet would have gone to {refused['refused']}",
                                     money=refused["money"])


async def create_start(campaign: Campaign, archiver: LLMClient) -> dict | None:
    """At campaign creation: a starting sheet from the premise, with concrete money and kit, so
    the GM has real numbers from the first turn."""
    premise = (campaign.meta.get("premise") or "").strip()
    if not premise or load(campaign) is not None:
        return None
    data = await archiver.json(
        [{"role": "user", "content": prompts.SHEET_START_TASK.format(
            system=campaign.meta.get("system") or "unspecified", premise=premise)}],
        SHEET_SCHEMA, max_tokens=1200)
    if load(campaign) is not None:  # the first turn's update got there first
        return None
    if campaign.meta.get("appearance"):  # chosen with the portrait during setup
        data["appearance"] = campaign.meta["appearance"]
    return save(campaign, data, 0, "starting sheet from the premise")
