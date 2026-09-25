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

from rpg_llm import prompts
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign

FILE = "character.yaml"
HISTORY = "character.history.jsonl"
LISTS = ("skills", "condition", "gear", "assets", "companions", "obligations")
TEXTS = ("name", "concept", "money")
FIELDS = ("name", "concept", "skills", "condition", "money", "gear", "assets", "companions",
          "obligations")

SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "string"} for k in TEXTS},
        **{k: {"type": "array", "items": {"type": "string"}} for k in LISTS},
    },
    "required": list(FIELDS),
    "additionalProperties": False,
}

UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {"type": "array", "items": {"type": "string"}},
        "changed": {"type": "boolean"},
        "sheet": SHEET_SCHEMA,
    },
    "required": ["changes", "changed", "sheet"],
    "additionalProperties": False,
}


def blank() -> dict:
    return {k: ([] if k in LISTS else "") for k in FIELDS}


def clean(data: dict) -> dict:
    """Keep only known fields, in order, with the right types; drop empty list entries."""
    out = blank()
    for k in FIELDS:
        v = data.get(k)
        if k in LISTS:
            items = v if isinstance(v, list) else ([v] if v else [])
            out[k] = [str(i).strip() for i in items if str(i).strip()]
        else:
            out[k] = str(v or "").strip()
    return out


MAX_ITEMS = 12


def guard(old: dict | None, new: dict) -> dict:
    """Protect the sheet from a small model's slips: a known amount of money never turns into
    one without a number, and lists stay short."""
    if old and re.search(r"\d", old.get("money", "")) and not re.search(r"\d", new.get("money", "")):
        new["money"] = old["money"]
    for k in LISTS:
        new[k] = new[k][:MAX_ITEMS]
    return new


def load(campaign: Campaign) -> dict | None:
    text = campaign.read(FILE)
    return clean(yaml.safe_load(text) or {}) if text.strip() else None


def save(campaign: Campaign, sheet: dict, after: int, what: str) -> dict:
    sheet = clean(sheet)
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


def notes_block(sheet: dict | None) -> str:
    if not sheet:
        return ""
    lines = [f"## Character sheet (current; keep to it)"]
    for k in FIELDS:
        v = sheet.get(k)
        if not v:
            continue
        label = k.replace("_", " ").capitalize()
        lines.append(f"{label}: {'; '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines)


def rewind(campaign: Campaign, first_dead_id: int) -> bool:
    """A turn from first_dead_id on was taken back: restore the sheet as it was before it."""
    hist = history(campaign)
    if not hist or hist[-1]["after"] < first_dead_id:
        return False
    keep = [h for h in hist if h["after"] < first_dead_id]
    if keep:
        campaign.write(FILE, yaml.safe_dump(keep[-1]["sheet"], sort_keys=False,
                                            allow_unicode=True, width=100))
    else:
        campaign.path(FILE).unlink(missing_ok=True)
    campaign.path(HISTORY).write_text(
        "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in keep))
    return True


async def update(campaign: Campaign, router: LLMClient, router_system: str) -> dict | None:
    """After a GM reply: apply what the latest exchange concretely changed. Creates the sheet
    from the brief the first time. Returns {"changes": [...]} if something changed."""
    messages = campaign.messages()
    if len(messages) < 2 or messages[-1]["role"] != "assistant":
        return None
    current = load(campaign)
    exchange = "\n\n".join(f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {m['content'][:3000]}"
                           for m in messages[-2:])
    task = prompts.SHEET_TASK.format(
        sheet=yaml.safe_dump(current, sort_keys=False, allow_unicode=True) if current
        else "(no sheet yet: create it from the brief and this exchange)",
        exchange=exchange)
    result = await router.json([{"role": "system", "content": router_system},
                                {"role": "user", "content": task}],
                               UPDATE_SCHEMA, max_tokens=1500)
    if campaign.messages()[-1]["id"] != messages[-1]["id"]:
        return None  # the turn was taken back meanwhile
    new = guard(current, clean(result.get("sheet") or {}))
    if current is not None and (not result.get("changed") or new == current):
        return None
    if not new.get("name") and current is None:
        return None  # nothing usable yet
    what = "; ".join(result.get("changes") or []) or ("created" if current is None else "updated")
    save(campaign, new, messages[-1]["id"], what)
    return {"changes": result.get("changes") or [what]}


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
    return save(campaign, data, 0, "starting sheet from the premise")
