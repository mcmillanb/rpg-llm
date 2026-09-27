"""Cast: every named person the player character has met, as structured records.

The router reads each new exchange after the GM replies and records who appeared: pronouns,
role, looks, where they are, how they stand with the player character, and a few notes. Name,
pronouns, looks and role are written once and then kept (only the player can change them), so a
character can't drift ("Kess" turning from he to she). The people in play ride in the per-turn
GM notes, and the archivist is shown the records when it files a scene.

cast.yaml holds {"until": last transcript id read, "people": [...]}. Each save is snapshotted
against the transcript, so rewinding a turn also rewinds what it did to the cast. A campaign
that predates the cast catches up by reading its transcript in chunks.
"""

import json
import re
import time

import yaml

from rpg_llm import prompts, sheet
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign

FILE = "cast.yaml"
HISTORY = "cast.history.jsonl"
KEEP_HISTORY = 40  # rewinds only ever go back a turn or two
FIELDS = ("name", "pronouns", "role", "look", "where", "standing")
FIXED = ("pronouns", "role", "look")  # set once by the router; later the player's to change
MAX_NOTES = 5
MAX_FIELD = 140
CHUNK_CHARS = 7000
IN_PLAY_MESSAGES = 6  # people named in this many recent messages are "in play"
MAX_IN_PLAY = 10

PERSON = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        **{k: {"type": "string"} for k in FIELDS if k != "name"},
        "note": {"type": "string"},
    },
    "required": ["name", "aliases", "pronouns", "role", "look", "where", "standing", "note"],
    "additionalProperties": False,
}
UPDATE_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": PERSON}},
    "required": ["people"],
    "additionalProperties": False,
}
PERSONAL = {"he", "him", "his", "she", "her", "hers", "they", "them", "their"}
PRONOUNS = {"he", "him", "his", "she", "her", "hers", "they", "them", "their", "it", "its",
            "xe", "xem", "ze", "zir", "hir"}
NOT_PEOPLE = re.compile(r"\b(location|place|town|city|village|planet|world|moon|station|starship|"
                        r"vessel|vehicle|building|organi[sz]ation|company)\b", re.I)
_THINKING = re.compile(r"\b(wait|re-?read|actually|i will|i'll|let me|hmm|not specified|"
                       r"unknown|not mentioned|n/a)\b|\?", re.I)


# ---- storage ------------------------------------------------------------------

def load_all(campaign: Campaign) -> dict:
    text = campaign.read(FILE)
    data = yaml.safe_load(text) if text.strip() else None
    if not isinstance(data, dict):
        return {"until": 0, "people": []}
    return {"until": int(data.get("until") or 0),
            "people": [clean_person(p) for p in data.get("people") or [] if p.get("name")]}


def people(campaign: Campaign) -> list[dict]:
    return load_all(campaign)["people"]


def exists(campaign: Campaign) -> bool:
    return campaign.path(FILE).exists()


def save(campaign: Campaign, data: dict, what: str) -> dict:
    data = {"until": int(data.get("until") or 0),
            "people": [clean_person(p) for p in data["people"] if p.get("name", "").strip()]}
    campaign.write(FILE, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    hist = history(campaign)[-(KEEP_HISTORY - 1):]
    hist.append({"after": data["until"], "ts": time.time(), "what": what, "cast": data})
    campaign.path(HISTORY).write_text(
        "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hist))
    return data


def history(campaign: Campaign) -> list[dict]:
    p = campaign.path(HISTORY)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def rewind(campaign: Campaign, first_dead_id: int) -> bool:
    """A turn from first_dead_id on was taken back: restore the cast as it was before it."""
    hist = history(campaign)
    if not hist or hist[-1]["after"] < first_dead_id:
        return False
    keep = [h for h in hist if h["after"] < first_dead_id]
    edits = [h for h in hist if h["after"] >= first_dead_id and h["what"] == "edited by the player"]
    if edits:  # the player's own corrections survive; the dead turns get read again
        last = edits[-1]
        keep.append({**last, "after": first_dead_id - 1,
                     "cast": {**last["cast"], "until": min(last["cast"]["until"], first_dead_id - 1)}})
    if keep:
        campaign.write(FILE, yaml.safe_dump(keep[-1]["cast"], sort_keys=False,
                                            allow_unicode=True, width=100))
    else:
        # older than the history we keep: forget the dead turns, re-read them next time
        data = load_all(campaign)
        data["until"] = min(data["until"], first_dead_id - 1)
        campaign.write(FILE, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    campaign.path(HISTORY).write_text(
        "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in keep))
    return True


# ---- records ------------------------------------------------------------------

def _bare_name(v) -> str:
    """'B. Kess (aka Kess)' or 'Marta (she)' -> the name alone: a model sometimes copies the
    list's formatting back into a name."""
    n = str(v or "")
    n = re.split(r"\s*\((?:aka|also|a\.k\.a)\b", n, flags=re.I)[0]
    n = re.sub(r"\s*\((?:he|she|they|it)(?:/\w+)*\)\s*$", "", n, flags=re.I)
    return n.strip(" ,;:")


def _text(v, limit: int = MAX_FIELD) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= limit else v[:limit - 1] + "…"


def pronouns(v) -> str:
    """"He/Him" -> "he/him"; anything that isn't a third-person pronoun ("I", "unknown") -> ""."""
    words = re.findall(r"[a-z]+", str(v or "").lower())
    return "/".join(w for w in words if w in PRONOUNS) if words and all(
        w in PRONOUNS for w in words) else ""


def good_alias(a: str, name: str) -> bool:
    a = a.strip()
    return (2 <= len(a) <= 40 and a[0].isupper() and a.lower() != name.lower()
            and a.lower() not in PRONOUNS)


def clean_person(p: dict) -> dict:
    name = _text(_bare_name(p.get("name")), 80)
    out = {"name": name,
           "aliases": sorted({_text(_bare_name(a), 80) for a in p.get("aliases") or []
                              if good_alias(_bare_name(a), name)})}
    for k in FIELDS[1:]:
        out[k] = _text(p.get(k))
    out["pronouns"] = pronouns(out["pronouns"]) if p.get("pronouns") else ""
    out["notes"] = [_text(n) for n in p.get("notes") or [] if str(n).strip()][-MAX_NOTES:]
    out["first"] = int(p.get("first") or 0)
    out["last"] = int(p.get("last") or 0)
    return out


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[\w'-]+", name.lower()) if len(w) > 1 or name.strip() == w}


def names_of(p: dict) -> list[str]:
    return [p["name"], *p.get("aliases", [])]


def find(cast: list[dict], name: str) -> dict | None:
    """Exact name or alias, else the one person whose name has all of this name's words or
    vice versa ("Kess" <-> "B. Kess")."""
    key = name.strip().lower()
    for p in cast:
        if key in (n.lower() for n in names_of(p)):
            return p
    words = _words(name) - {"the"}
    if not words:
        return None
    loose = [p for p in cast if any(words <= _words(n) or (_words(n) and _words(n) <= words)
                                    for n in names_of(p))]
    return loose[0] if len(loose) == 1 else None


def player_name(campaign: Campaign) -> str:
    """The player character's name: from the sheet, else the premise's opening words
    ("Calvin is a scrappy young scavenger..." -> "Calvin")."""
    name = ((sheet.load(campaign) or {}).get("name") or "").strip()
    if name:
        return name
    m = re.match(r"\s*([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)\s+(is|was|has|had)\b",
                 campaign.meta.get("premise") or "")
    return m.group(1) if m else ""


def _is_player(name: str, pc: str) -> bool:
    w, pcw = _words(name), _words(pc)
    return bool(w and pcw and w <= pcw)


def _usable(v: str) -> str:
    v = _text(v)
    return "" if not v or _THINKING.search(v) else v


def _named_in(text: str, names: list[str]) -> bool:
    """A real name appears as written, capitalised ("Kess"); a job title the model capitalised
    ("Clerk" for "the clerk") doesn't."""
    return any(n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text) for n in names)


def merge(cast: list[dict], found: list[dict], msg_id: int, pc_name: str = "",
          source: str | None = None) -> list[str]:
    """Apply what the router found to the cast (in place). Returns short change notes.
    `source` is the text it read: a new person must be named in it."""
    changes = []
    for f in found:
        name = _text(_bare_name(f.get("name")), 80)
        f = {**f, "aliases": [_bare_name(a) for a in f.get("aliases") or []]}
        if (len(name) < 2 or not name[0].isupper() or _is_player(name, pc_name)
                or (NOT_PEOPLE.search(f.get("role") or "")
                    and not set(pronouns(f.get("pronouns")).split("/")) & PERSONAL)):
            continue  # unnamed ("the guard"), the player character, or not a person at all
        p = find(cast, name)
        for a in f.get("aliases") or []:
            p = p or find(cast, str(a))
        if p is None:
            if source is not None and not _named_in(source, [name, *(f.get("aliases") or [])]):
                continue
            p = clean_person({"name": name, "first": msg_id})
            cast.append(p)
            changes.append(f"Met {name}")
        elif name.lower() not in (n.lower() for n in names_of(p)):
            if len(name) > len(p["name"]) and (source is None or _named_in(source, [name])):
                # the fuller name wins; the other becomes an alias
                p["aliases"].append(p["name"])
                p["name"] = name
            else:
                p["aliases"].append(name)
        p["aliases"] = sorted({a for a in {*p["aliases"], *(_text(a, 80) for a in f.get("aliases") or [])}
                               if good_alias(a, p["name"])})
        for k in FIXED:
            v = pronouns(f.get(k)) if k == "pronouns" else _usable(f.get(k))
            if v and not p[k]:
                p[k] = v
        for k in ("where", "standing"):
            v = _usable(f.get(k))
            if v and v != p[k]:
                p[k] = v
        note = _usable(f.get("note"))
        if note and not any(note.lower() in n.lower() or n.lower() in note.lower()
                            for n in p["notes"]):
            p["notes"] = (p["notes"] + [note])[-MAX_NOTES:]
        p["last"] = max(p["last"], msg_id)
        if not p["first"]:
            p["first"] = msg_id
    return changes


# ---- reading the transcript -------------------------------------------------------

def _chunks(messages: list[dict]) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for m in messages:
        n = min(len(m["content"]), 3000)
        if cur and size + n > CHUNK_CHARS and cur[-1]["role"] == "assistant":
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        out.append(cur)
    return out


def cast_text(cast: list[dict]) -> str:
    """One line per person, in a shape that doesn't look like a name when copied back."""
    return "\n".join(f"- name: {p['name']}" + (f"; also called: {', '.join(p['aliases'])}" if p["aliases"] else "")
                     + (f"; pronouns: {p['pronouns']}" if p["pronouns"] else "")
                     + (f"; role: {p['role']}" if p["role"] else "") for p in cast) or "(nobody yet)"


async def catch_up(campaign: Campaign, router: LLMClient, lock, pc_name: str = "",
                   on_progress=None) -> list[str]:
    """Read every exchange the cast hasn't seen yet (normally just the latest) and record the
    people in it. Chunked, so an old campaign catches up from its whole transcript."""
    changes = []
    while True:
        data = load_all(campaign)
        pending = [m for m in campaign.messages()
                   if m["id"] > data["until"] and m["role"] in ("user", "assistant")
                   and m.get("content", "").strip()]
        if pending and pending[-1]["role"] == "user":
            pending = pending[:-1]  # wait for the GM's reply
        if not pending:
            return changes
        chunk = _chunks(pending)[0]
        exchange = "\n\n".join(f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {m['content'][:3000]}"
                               for m in chunk)
        result = await router.json(
            [{"role": "user", "content": prompts.CAST_TASK.format(
                player=pc_name or "(the player character)", cast=cast_text(data["people"]),
                exchange=exchange)}],
            UPDATE_SCHEMA, max_tokens=1500)
        async with lock:
            live = {m["id"] for m in campaign.messages()}
            if chunk[-1]["id"] not in live:
                return changes  # taken back meanwhile
            fresh = load_all(campaign)
            if fresh["until"] != data["until"]:
                continue  # someone else moved it on (a player edit): read again
            new = merge(fresh["people"], result.get("people") or [], chunk[-1]["id"], pc_name,
                        source=exchange)
            fresh["until"] = chunk[-1]["id"]
            save(campaign, fresh, "; ".join(new) or "updated")
            changes += new
        if on_progress:
            on_progress(chunk[-1]["id"], pending[-1]["id"])


# ---- for the GM --------------------------------------------------------------------

def mentioned(cast: list[dict], text: str) -> list[dict]:
    hits = []
    for p in cast:
        for n in names_of(p):  # proper names only: "the quartermaster" would match anyone's
            if len(n) >= 3 and n[0].isupper() and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.I):
                hits.append(p)
                break
    return hits


def line(p: dict, notes: int = 2) -> str:
    bits = [f"{p['name']}" + (f" ({p['pronouns']})" if p["pronouns"] else "")
            + (f": {p['role']}" if p["role"] else "")]
    if p["look"]:
        bits.append(f"Looks: {p['look']}")
    if p["where"]:
        bits.append(f"Where: {p['where']}")
    if p["standing"]:
        bits.append(f"Attitude: {p['standing']}")
    if notes and p["notes"]:
        bits.append("; ".join(n.rstrip(".") for n in p["notes"][-notes:]))
    return "- " + ". ".join(b.rstrip(".") for b in bits) + "."


GENDERED = {"he": ("he", "him", "his", "himself"), "she": ("she", "her", "hers", "herself")}


def slipped(p: dict, cast_list: list[dict], replies: list[str]) -> str | None:
    """If the GM's recent replies call this person by the other gender's pronouns (sentences
    naming only them, with only the wrong pronouns), return the wrong pronoun."""
    mine = p["pronouns"].split("/")[0] if p["pronouns"] else ""
    if mine not in GENDERED:
        return None
    other = "she" if mine == "he" else "he"
    others = [n for q in cast_list if q is not p for n in names_of(q) if len(n) >= 3]
    hits = 0
    for text in replies:
        for sent in re.split(r"(?<=[.!?\"”])\s+", text):
            if not mentioned([p], sent) or any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", sent)
                                              for n in others):
                continue
            words = set(re.findall(r"[a-z]+", sent.lower()))
            if words & set(GENDERED[other]) and not words & set(GENDERED[mine]):
                hits += 1
    return other if hits >= 2 else None


def notes_block(campaign: Campaign, messages: list[dict]) -> str:
    """The people named in the last few messages, for the per-turn GM notes."""
    cast = people(campaign)
    if not cast:
        return ""
    recent = "\n".join(m["content"] for m in messages[-IN_PLAY_MESSAGES:])
    hits = sorted(mentioned(cast, recent), key=lambda p: -p["last"])[:MAX_IN_PLAY]
    if not hits:
        return ""
    replies = [m["content"] for m in messages[-IN_PLAY_MESSAGES:] if m["role"] == "assistant"]
    fixes = []
    for p in hits:
        wrong = slipped(p, cast, replies)
        if wrong:
            fixes.append(f"Correction: {p['name']} is {p['pronouns']}. Recent replies wrongly used "
                         f"\"{wrong}\" for {p['name']}; from now on use {p['pronouns']}, without "
                         f"remarking on the change.")
    return ("## People in play (this record is correct: use these names, pronouns, looks and "
            "roles, even where earlier replies slipped)\n" + "\n".join(line(p) for p in hits)
            + ("\n" + "\n".join(fixes) if fixes else ""))
