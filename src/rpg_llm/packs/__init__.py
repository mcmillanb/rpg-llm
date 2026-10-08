"""System packs: what makes a game system play like itself.

Each pack (a folder here) has pack.yaml (name, which system names it matches, the look, the
currency, the dice, the system's own character sheet fields and counters), rules.md (a rules
summary for the GM) and creation.md (how session 0 builds a character).

A campaign in Rules mode uses its pack's sheet and session 0; in Story mode the pack only adds
the dice conventions, and the rules summary for systems the GM model doesn't know.
"""

import re
from functools import lru_cache
from pathlib import Path

import yaml

DIR = Path(__file__).parent


@lru_cache
def all_packs() -> dict[str, dict]:
    out = {}
    for d in sorted(p for p in DIR.iterdir() if (p / "pack.yaml").exists()):
        pack = yaml.safe_load((d / "pack.yaml").read_text())
        pack["rules"] = (d / "rules.md").read_text().strip() if (d / "rules.md").exists() else ""
        pack["creation"] = "\n\n".join(
            (d / f).read_text().strip() for f in ("creation.md", "creation_official.md") if (d / f).exists())
        out[pack["id"]] = pack
    return out


def get(pack_id: str | None) -> dict | None:
    return all_packs().get(pack_id or "")


def enabled() -> list[dict]:
    return [p for p in all_packs().values() if p.get("enabled", True)]


def match(system: str) -> dict | None:
    """The pack for a game system name ("D&D 5e (2024 rules)", "Daggerheart"), if any."""
    s = f" {(system or '').lower()} "
    for pack in enabled():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", s) for m in pack["matches"]):
            return pack
    return None


def for_campaign(meta: dict) -> dict | None:
    return get(meta.get("pack")) or match(meta.get("system") or "")


def rules_mode(meta: dict) -> bool:
    return meta.get("mode") == "rules" and for_campaign(meta) is not None


def public(pack: dict | None) -> dict | None:
    """What the play page needs to draw the sheet."""
    if not pack:
        return None
    return {k: pack.get(k) for k in ("id", "name", "sheet", "resources", "hide_core", "currency",
                                     "duality")}


def picker_entries() -> list[dict]:
    """Packs for the game-system picker (the GM model may not list them itself)."""
    return [{"name": p["name"], "genre": p.get("genre", ""), "blurb": p.get("blurb", ""),
             "pack": p["id"]} for p in enabled()]


# ---- the rules reference (reference.jsonl, built by scripts/build_srd.py) ----------------------

LOOKUP_TOOL = {"type": "function", "function": {
    "name": "rules_lookup",
    "description": "Look up the game's official rules text: a class, subclass, ancestry, community, "
                   "domain card, spell, feature, weapon, armour, item, adversary, environment or rule "
                   "(e.g. 'Faun', 'Sneak Attack', 'Rogue', 'Minor Health Potion', 'death move'). Use it "
                   "instead of guessing, and quote features exactly.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"]}}}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("’", "").replace("'", ""))


@lru_cache
def reference(pack_id: str) -> list[dict]:
    p = DIR / pack_id / "reference.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            e = __import__("json").loads(line)
            e["_title"] = set(_words(e["title"]))
            e["_body"] = e["text"].lower()
            out.append(e)
    return out


def search(pack_id: str, query: str, n: int = 5) -> list[dict]:
    """Best matches: the title first (exact, then words), then the text."""
    q = query.strip().lower()
    words = [w for w in _words(q) if len(w) > 1]
    if not words:
        return []
    scored = []
    for e in reference(pack_id):
        title = e["title"].lower()
        bare = title.split(": ", 1)[-1]  # "Core Mechanics: HOPE & FEAR" -> "hope & fear"
        score = 100 if q in (title, bare) else 40 if (title.startswith(q + " ") or bare.startswith(q + " ")) else 0
        score += 12 * sum(w in e["_title"] for w in words)
        if q in e["_body"]:
            score += 8 + (10 if e["category"] in ("core_rules", "rules glossary") else 0)  # rules over monsters
        score += sum(min(e["_body"].count(w), 5) for w in words) * 0.4
        if any(w.rstrip("s") in e["category"] for w in words):
            score += 5
        if score > 2:
            scored.append((score, -len(e["text"]), e))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e for _, _, e in scored[:n]]


def lookup(pack_id: str, query: str) -> str:
    """What the GM reads back from rules_lookup."""
    hits = search(pack_id, query)
    if not hits:
        return f"Nothing in the rules reference matches {query!r}. Don't invent it: ask the player, or describe it in general terms."
    best, rest = hits[0], hits[1:]
    text = best["text"] if len(best["text"]) <= 3500 else best["text"][:3500] + "\n[…]"
    more = f"\n\nOther matches: {'; '.join(e['title'] for e in rest)}" if rest else ""
    return f"{best['title']} ({best['category']}):\n\n{text}{more}"


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("’", "'"))


@lru_cache
def _titles(pack_id: str, category: str) -> dict[str, str]:
    return {_key(e["title"]): e["title"] for e in reference(pack_id) if e["category"] == category}


def unknown_names(pack: dict, field: str, value) -> list[str]:
    """Names in a sheet field that the rules reference doesn't have (pack.yaml 'checks'):
    'Mist Walk' as a domain card, say. The name is what comes before any '(', ':' or ' - '."""
    category = (pack.get("checks") or {}).get(field)
    if not category or not has_reference(pack):
        return []
    known = _titles(pack["id"], category)
    out = []
    for v in value if isinstance(value, list) else [value]:
        name = re.split(r"\s*[(:]|\s+[-–]\s+", str(v or ""))[0].strip()
        if name and _key(name) not in known:
            out.append(name)
    return out


def options(pack: dict, field: str) -> list[str]:
    category = (pack.get("checks") or {}).get(field)
    return sorted(_titles(pack["id"], category).values()) if category else []


def has_reference(pack: dict | None) -> bool:
    return bool(pack) and (DIR / pack["id"] / "reference.jsonl").exists()
