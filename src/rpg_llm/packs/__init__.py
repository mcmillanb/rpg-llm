"""System packs: what makes a game system play like itself.

Each pack (a folder here) has pack.yaml (name, which system names it matches, the look, the
currency, the dice, the system's own character sheet fields and counters), rules.md (a rules
summary for the GM) and creation.md (how session 0 builds a character).

A campaign in Rules mode uses its pack's sheet and session 0; in Story mode the pack only adds
the dice conventions, and the rules summary for systems the GM model doesn't know.
"""

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

DIR = Path(__file__).parent
# Packs built from licensed books stay on this server: a folder outside git and the image, mounted
# into the container (docker-compose.yml), and looked for beside the source when run directly.
PRIVATE = [Path(p) for p in os.environ.get("PRIVATE_PACKS", "/restricted/packs").split(os.pathsep) if p] + \
    [DIR.parents[2] / "restricted" / "packs"]


def _dirs() -> list[Path]:
    return [DIR] + [d for d in PRIVATE if d.is_dir()]


@lru_cache
def pack_dir(pack_id: str) -> Path:
    return next((d / pack_id for d in _dirs() if (d / pack_id / "pack.yaml").exists()), DIR / pack_id)


@lru_cache
def all_packs() -> dict[str, dict]:
    out = {}
    for d in sorted((p for root in _dirs() for p in root.iterdir() if (p / "pack.yaml").exists()),
                    key=lambda p: p.name):
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
    best, size = None, 0
    for pack in enabled():  # the longest name matched wins: "Ironsworn: Starforged" is Starforged
        for m in pack["matches"]:
            if len(m) > size and re.search(rf"(?<![a-z0-9]){re.escape(m)}(?![a-z0-9])", s):
                best, size = pack, len(m)
    return best


def for_campaign(meta: dict) -> dict | None:
    return get(meta.get("pack")) or match(meta.get("system") or "")


def rules_mode(meta: dict) -> bool:
    return meta.get("mode") == "rules" and for_campaign(meta) is not None


def public(pack: dict | None) -> dict | None:
    """What the play page needs to draw the sheet."""
    if not pack:
        return None
    return {k: pack.get(k) for k in ("id", "name", "sheet", "resources", "hide_core", "currency",
                                     "duality", "ironsworn", "legacy")}


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
    p = pack_dir(pack_id) / "reference.jsonl"
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


def entry(pack_id: str, category: str, name: str) -> dict | None:
    """The reference entry with exactly this name in a category."""
    key = _key(re.split(r"\s*[(:]|\s+[-–]\s+", name)[0])
    return next((e for e in reference(pack_id) if e["category"] == category and _key(e["title"]) == key), None)


def explain(pack: dict, field: str, name: str) -> str:
    """The rules text for a choice, for the GM: the entry itself and, for a class, its level 1
    features (filed under the class's name in the reference)."""
    category = (pack.get("explain") or {}).get(field)
    e = entry(pack["id"], category, name) if category else None
    if not e and category and len(name.split()) > 1:  # "Wood Elf" is filed under "Elf"
        e = entry(pack["id"], category, name.split()[-1]) or entry(pack["id"], category, name.split()[0])
    if not e:
        return ""
    text = e["text"][:2500]
    if category == "classes":  # D&D: level 1 features are their own entries
        feats = [x for x in reference(pack["id"]) if x["category"] == "classes"
                 and x["title"].endswith(f"({e['title']} level 1)")]
        for f in feats:
            text += f"\n\n{f['title']}: {f['text'][:700]}"
    return f"{e['title']}: rules text (use it exactly):\n{text}"


def has_reference(pack: dict | None) -> bool:
    return bool(pack) and (pack_dir(pack["id"]) / "reference.jsonl").exists()


@lru_cache
def _weapon_rows(pack_id: str) -> dict[str, tuple[str, str]]:
    """Weapon name -> (damage, properties) from the reference's weapon tables."""
    out = {}
    for e in reference(pack_id):
        if e["category"] != "equipment" or "weapon" not in e["title"].lower():
            continue
        for line in e["text"].splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and re.match(r"\d+d\d+", cells[1]):
                out[_key(cells[0])] = (cells[1], cells[2])
    return out


def great_weapon_dice(pack: dict | None, sh: dict | None) -> list[tuple[int, int]]:
    """D&D: with Great Weapon Fighting on the sheet, the damage dice (count, sides) of the
    sheet's two-handed melee weapons (Two-Handed, or a Versatile weapon's two-handed die)."""
    system = (sh or {}).get("system") or {}
    if not pack or not any("great weapon fighting" in str(f).lower() for f in system.get("features") or []):
        return []
    rows, out = _weapon_rows(pack["id"]), []
    for a in system.get("attacks") or system.get("weapons") or []:
        row = rows.get(_key(re.split(r"\s*[(:,]", str(a))[0]))
        if not row or "ammunition" in row[1].lower():
            continue
        versatile = re.search(r"Versatile \((\d+)d(\d+)\)", row[1])
        die = versatile.groups() if versatile else (
            re.match(r"(\d+)d(\d+)", row[0]).groups() if "two-handed" in row[1].lower() else None)
        if die:
            out.append((int(die[0]), int(die[1])))
    return out


# ---- oracles (Ironsworn, Starforged: oracles.json, built by scripts/build_srd.py) -------------

ORACLE_TOOL = {"type": "function", "function": {
    "name": "ask_oracle",
    "description": "Roll on one of the game's oracle tables, or ask the oracle a yes/no question, "
                   "instead of inventing the answer: names, places, planets, characters' looks and "
                   "goals, derelicts, twists, 'is anyone there?'. The app rolls d100 for real; build "
                   "on what it gives. A table by name, e.g. 'Action', 'Theme', 'Descriptor', "
                   "'Focus', 'Given Name', 'Settlement Name', 'Pay the Price', 'Planet: Desert "
                   "World: Atmosphere'; or a yes/no question with odds.",
    "parameters": {"type": "object", "properties": {
        "table": {"type": "string", "description": "the oracle table's name (for a table roll)"},
        "question": {"type": "string", "description": "a yes/no question (with odds)"},
        "odds": {"type": "string", "enum": ["small chance", "unlikely", "50/50", "likely", "almost certain"]},
    }}}}

ODDS = {"small chance": 10, "unlikely": 25, "50/50": 50, "likely": 75, "almost certain": 90}


@lru_cache
def oracles(pack_id: str) -> list[dict]:
    p = pack_dir(pack_id) / "oracles.json"
    return __import__("json").loads(p.read_text()) if p.exists() else []


def find_oracle(pack_id: str, name: str) -> dict | None:
    """The table a name means: the full path, its last part ('Action'), or most of its words."""
    want = _words(name)
    if not want:
        return None
    best, score = None, 0
    for t in oracles(pack_id):
        parts = [_words(x) for x in t["name"].split(" / ")]
        words = set(w for p in parts for w in p)
        s = 100 if parts[-1] == want else 50 if _words(t["name"]) == want else 0
        s += sum(w in words or w.rstrip("s") in words for w in want) * 10 - len(words) * 0.1
        s += sum(w in parts[-1] for w in want) * 3
        if s > score:
            best, score = t, s
    return best if score >= 10 * len(want) * 0.6 else None


def _match(n: int) -> bool:
    return n == 100 or (n > 10 and n % 11 == 0)


def ask_oracle(pack_id: str, args: dict) -> tuple[str, dict | None]:
    """The oracle's answer for the GM, and a record of the roll for the page (None if there was
    nothing to roll)."""
    import secrets
    n = secrets.randbelow(100) + 1
    twist = " A match (both digits the same): an extreme result or a twist." if _match(n) else ""
    if args.get("question") or (args.get("odds") and not args.get("table")):
        odds = str(args.get("odds") or "50/50").lower()
        need = ODDS.get(odds, 50)
        answer = "Yes" if n <= need else "No"
        q = str(args.get("question") or "").strip()
        return (f"Ask the Oracle ({odds}, yes on {need} or less){': ' + q if q else ''}: rolled {n}: "
                f"{answer}.{twist} Build on this answer.",
                {"dice": "1D100", "reason": f"Oracle ({odds}): {q[:60]} → {answer}".strip(), "rolls": [n],
                 "modifier": 0, "total": n, "sides": [100], "oracle": answer})
    t = find_oracle(pack_id, str(args.get("table") or ""))
    if not t:
        return (f"No oracle table called {args.get('table')!r}. Try a shorter name (Action, Theme, "
                f"Descriptor, Focus, Given Name...), look tables up with rules_lookup ('Oracle: ...'), or "
                f"ask a yes/no question with odds.", None)
    row = next((r for r in t["rows"] if r[0] <= n <= r[1]), None)
    result = row[2] if row else "(nothing on that roll)"
    return (f"Oracle {t['name']}: rolled {n}: {result}.{twist} Build on this; roll again for more.",
            {"dice": "1D100", "reason": f"Oracle {t['name'].split(' / ')[-1]} → {result}"[:120],
             "rolls": [n], "modifier": 0, "total": n, "sides": [100], "oracle": result})
