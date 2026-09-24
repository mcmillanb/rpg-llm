"""Campaign wiki: gazetteer matching, note reading/writing, and the DM's read-only tools."""

import json
import re
import time

import yaml

from rpg_llm.vault import Campaign, State, slugify

KIND_DIRS = {"location": "locations", "npc": "npcs"}  # anything else goes to "things"
_KIND_ALIASES = {
    "npc": {"npc", "person", "character", "people", "individual", "crew", "contact"},
    "location": {"location", "place", "planet", "world", "system", "station", "city", "town",
                 "starport", "highport", "downport", "bar", "building", "venue", "moon",
                 "settlement", "outpost", "site", "base", "facility", "shop", "cantina"},
}


def normalise_kind(kind: str | None) -> str:
    k = (kind or "thing").strip().lower()
    for canon, words in _KIND_ALIASES.items():
        if k in words:
            return canon
    return k if k in ("ship", "vehicle", "organisation", "organization", "item", "faction") \
        else "thing"

TOOLS = [
    {"type": "function", "function": {
        "name": "lookup",
        "description": "Search the campaign wiki index for a person, place, ship, organisation or "
                       "thing by name or alias. Returns matching entries with a short summary and "
                       "the note path.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "read_note",
        "description": "Read a full wiki note (history of every visit or encounter) by its path, "
                       "as returned by lookup.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_scene",
        "description": "Read a filed scene by id. exact=true returns the original transcript, for "
                       "when exact wording matters (what someone said, the terms of a deal).",
        "parameters": {"type": "object", "properties": {
            "scene_id": {"type": "integer"}, "exact": {"type": "boolean"}},
            "required": ["scene_id"]}}},
]


# ---- gazetteer --------------------------------------------------------------

def _names(entry: dict) -> list[str]:
    return [n for n in [entry["name"], *(entry.get("aliases") or [])] if n and len(n) >= 3]


def _bare(name: str) -> str:
    """Name without bracketed extras, quotes or a leading "the": "Quantum Core (QC-9)" ->
    "quantum core"."""
    n = re.sub(r"\([^)]*\)|[\"'“”‘’]", "", name).strip().lower()
    return re.sub(r"^the\s+", "", re.sub(r"\s+", " ", n))


def mentioned(gazetteer: list[dict], *texts: str) -> list[dict]:
    """Entries whose name or alias appears as a whole phrase in any of the texts."""
    blob = "\n".join(texts)
    hits = []
    for e in gazetteer:
        if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", blob, re.I) for n in _names(e)):
            hits.append(e)
    return hits


def find(gazetteer: list[dict], name: str, kind: str | None = None) -> dict | None:
    """Exact name/alias match, else (people only) a unique NPC whose name contains all of this
    name's words or vice versa ("Brandt" <-> "Oskar Brandt"). Places are never loosely matched:
    "Efate" and "Efate Startown" are different notes."""
    key = name.strip().lower()
    for e in gazetteer:
        if key in (n.lower() for n in _names(e)):
            return e
    bare = _bare(name)
    for e in gazetteer:
        if bare and bare in (_bare(n) for n in _names(e)):
            return e
    words = set(re.findall(r"\w+", key))
    if not words or kind != "npc":
        return None
    loose = [e for e in gazetteer if e["type"] == "npc" and
             any(words <= set(re.findall(r"\w+", n.lower())) or
                    set(re.findall(r"\w+", n.lower())) <= words for n in _names(e))]
    return loose[0] if len(loose) == 1 else None


def search(gazetteer: list[dict], query: str) -> list[dict]:
    q = query.strip().lower()
    exact = [e for e in gazetteer if q in (n.lower() for n in _names(e))]
    if exact:
        return exact
    words = [w for w in re.findall(r"\w+", q) if len(w) >= 3]
    return [e for e in gazetteer
            if any(w in " ".join(_names(e)).lower() or w in (e.get("summary") or "").lower()
                   for w in words)][:8]


def gazetteer_text(gazetteer: list[dict]) -> str:
    if not gazetteer:
        return "(empty)"
    lines = []
    for e in gazetteer:
        aka = f" (aka {', '.join(e['aliases'])})" if e.get("aliases") else ""
        lines.append(f"- {e['path']} | {e['type']}: {e['name']}{aka} - {e.get('summary', '')}")
    return "\n".join(lines)


def scenes_text(campaign: Campaign, state: State) -> str:
    done = [s for s in state.scenes if s.status == "compacted"]
    if not done:
        return "(none yet)"
    return "\n".join(f"- scene {s.id}: {s.title or 'untitled'} @ {s.location or '?'}" for s in done)


# ---- notes ------------------------------------------------------------------

def split_note(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        _, fm, body = text.split("---\n", 2)
        return yaml.safe_load(fm) or {}, body
    return {}, text


def current_state(campaign: Campaign, path: str) -> str:
    _, body = split_note(campaign.read(path))
    m = re.search(r"## Current state\n\n(.*?)(?=\n## |\Z)", body, re.S)
    return m.group(1).strip() if m else body.strip()[:600]


def upsert_entity(campaign: Campaign, gazetteer: list[dict], kind: str, data: dict,
                  scene_link: str, scene_label: str) -> dict:
    """Create or update an entity note: regenerate "Current state", append a dated visit.
    Returns the gazetteer entry (mutating `gazetteer` in place)."""
    name = data["name"].strip()
    kind = normalise_kind(kind)
    entry = find(gazetteer, name, kind)
    aliases = [a for a in (data.get("aliases") or []) if a and a.lower() != name.lower()]
    if entry is not None and entry["name"].lower() != name.lower():
        # the fuller name wins; the other becomes an alias
        if len(name) > len(entry["name"]):
            aliases.append(entry["name"])
            entry["name"] = name
        else:
            aliases.append(name)
    if entry is None:
        folder = KIND_DIRS.get(kind, "things")
        entry = {"name": name, "aliases": [], "type": kind, "path": f"{folder}/{slugify(name)}.md"}
        gazetteer.append(entry)
    entry["aliases"] = sorted({*entry.get("aliases", []), *aliases} - {entry["name"]})
    state_text = (data.get("current_state") or "").strip()
    entry["summary"] = state_text.split("\n")[0][:200]

    fm, body = split_note(campaign.read(entry["path"]))
    history = ""
    m = re.search(r"## History\n\n(.*)", body, re.S)
    if m:
        history = m.group(1).rstrip() + "\n\n"
    detail = data.get("role") or data.get("kind")
    visit = (data.get("visit") or "").strip()
    history += f"### {scene_label}: [[{scene_link}]]\n\n{visit}\n"
    fm = {**fm, "type": entry["type"], "name": entry["name"], "aliases": entry["aliases"],
          **({"detail": detail} if detail else {}), "updated": time.strftime("%Y-%m-%d")}
    campaign.write(entry["path"], "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                   + f"---\n# {entry['name']}\n\n## Current state\n\n{state_text}\n\n"
                   + f"## History\n\n{history}")
    return entry


# ---- scene ranges -----------------------------------------------------------

def scene_messages(campaign: Campaign, state: State, scene_id: int,
                   messages: list[dict] | None = None) -> list[dict]:
    messages = messages if messages is not None else campaign.messages()
    idx = next(i for i, s in enumerate(state.scenes) if s.id == scene_id)
    start = state.scenes[idx].start
    end = state.scenes[idx + 1].start if idx + 1 < len(state.scenes) else None
    return [m for m in messages if m["id"] >= start and (end is None or m["id"] < end)]


def format_transcript(messages: list[dict], limit_chars: int | None = None) -> str:
    text = "\n\n".join(f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {m['content']}"
                       for m in messages)
    if limit_chars and len(text) > limit_chars:
        text = "[...earlier lines cut...]\n" + text[-limit_chars:]
    return text


# ---- DM tools ---------------------------------------------------------------

def run_tool(campaign: Campaign, name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "Error: arguments were not valid JSON."
    gazetteer = campaign.gazetteer()
    try:
        if name == "lookup":
            hits = search(gazetteer, str(args.get("name", "")))
            if not hits:
                return f"No wiki entry matches {args.get('name')!r}. It has not been recorded."
            return "\n".join(f"- {e['name']} ({e['type']}), path {e['path']}: "
                             f"{current_state(campaign, e['path'])}" for e in hits)
        if name == "read_note":
            path = str(args.get("path", ""))
            text = campaign.read(path) if path.endswith(".md") else ""
            return text or f"No note at {path!r}."
        if name == "read_scene":
            state = campaign.load_state()
            sid = int(args.get("scene_id", 0))
            scene = next((s for s in state.scenes if s.id == sid), None)
            if scene is None:
                return f"No scene {sid}."
            if args.get("exact") or not scene.note:
                return format_transcript(scene_messages(campaign, state, sid), limit_chars=24000)
            return campaign.read(scene.note)
    except Exception as e:  # a bad tool call must never kill the turn
        return f"Error: {e}"
    return f"Unknown tool {name!r}."
