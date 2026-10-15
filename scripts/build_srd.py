"""Build the rules reference bundled with each system pack from the free System Reference
Documents, as searchable entries for the GM's rules_lookup tool.

    uv run python scripts/build_srd.py --daggerheart PATH --dnd PATH

--daggerheart: a checkout of github.com/JHerrin00/daggerheart-srd-2.0 (Daggerheart SRD 2.0,
  Darrington Press Community Gaming License). One entry per file (class, ancestry, community,
  domain card, weapon, armour, item, adversary, environment...); the core rules split by section.
--dnd: a checkout of github.com/gelatinous-labs/dndsrd5.2_markdown (D&D SRD 5.2, CC-BY-4.0).
  The chapters split by heading (spells, feats, magic items, monsters, rules glossary...).
--cepheus: a checkout of github.com/orffen/cepheus-srd (Cepheus Engine SRD, the open Traveller
  rules, OGL 1.0a), for the Traveller pack. The chapters split by heading; one entry per career.

--datasworn: a checkout of github.com/rsek/datasworn (the Ironsworn and Ironsworn: Starforged rules
  as JSON, CC-BY-4.0), for the Ironsworn and Starforged packs: moves, assets, oracles, setting
  truths, the rules for stats, meters and impacts; and oracles.json, the tables the GM rolls on.

Writes src/rpg_llm/packs/<pack>/reference.jsonl ({title, category, text} per line) and the
licence notice next to it; for Daggerheart also creation_official.md (the SRD's character
creation steps and the options tables).
"""

import argparse
import json
import re
from pathlib import Path

PACKS = Path(__file__).parent.parent / "src" / "rpg_llm" / "packs"
DH_SKIP = {"fantasy_statblocks", ".build", ".git", ".github"}


def write(pack: str, entries: list[dict], notice: str) -> None:
    out = PACKS / pack
    with (out / "reference.jsonl").open("w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    (out / "REFERENCE-LICENSE.md").write_text(notice.strip() + "\n")
    print(f"{pack}: {len(entries)} entries, {sum(len(e['text']) for e in entries) // 1024} KB")


def split_sections(text: str, title: str, category: str, level: int = 2) -> list[dict]:
    """A long document split at headings of `level` (and above): one entry per section."""
    parts = re.split(rf"(?m)^(#{{1,{level}}}) +(.+)$", text)
    entries, head = [], parts[0].strip()
    if head:
        entries.append({"title": title, "category": category, "text": head})
    for i in range(1, len(parts) - 2, 3):
        name = re.sub(r"[*_]", "", parts[i + 1]).strip()
        body = parts[i + 2].strip()
        if body:
            entries.append({"title": f"{title}: {name}" if name.lower() != title.lower() else title,
                            "category": category, "text": f"{name}\n\n{body}"})
    return entries


def daggerheart(root: Path) -> None:
    entries = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in DH_SKIP):
        for f in sorted(folder.glob("*.md")):
            text = f.read_text().strip()
            m = re.match(r"#\s+(.+)", text)
            title = (m.group(1) if m else f.stem).strip()
            if folder.name in ("core_rules", "campaign_frames"):
                entries += split_sections(text, title, folder.name, 3)
            else:
                entries.append({"title": title, "category": folder.name, "text": text})
    write("daggerheart", entries, (root / "LICENSE.md").read_text())

    # session 0: the SRD's own creation steps, plus the options with their numbers
    rows = []
    for f in sorted((root / "classes").glob("*.md")):
        t = f.read_text()
        get = lambda label: (re.search(rf"\*\*{label}\s*[–-]\*\*\s*(.+)", t) or [None, "?"])[1].strip()
        subs = re.search(r"Choose either the\*?\s*\*{0,3}(.+?)\*{0,3}\s*\*?or\*?\s*\*{0,3}(.+?)\*{0,3}\s*\*?subclass", t)
        sub = f"{subs.group(1).strip('* ')} or {subs.group(2).strip('* ')}" if subs else "?"
        rows.append(f"| {f.stem} | {get('DOMAINS')} | {get('STARTING EVASION')} | {get('STARTING HIT POINTS')} | {sub} | {get('CLASS ITEMS')} |")
    names = lambda d: ", ".join(sorted(p.stem for p in (root / d).glob("*.md")))
    plain = lambda t: re.sub(r"[*_]", "", t)

    def feature(t: str) -> str:
        m = re.search(r"### FEATURE\s*\n+(.+)", t)
        f = plain(m.group(1)).strip() if m else "—"
        return f if len(f) <= 110 else f[:107] + "…"

    weapons, armor, cards = [], [], {}
    for f in sorted((root / "weapons").glob("*.md")):
        t = f.read_text()
        lines = [plain(x).strip() for x in t.splitlines() if x.strip()]
        if len(lines) > 2 and lines[1].startswith("Tier 1"):
            kind = "secondary" if "Secondary" in lines[1] else "primary"
            weapons.append(f"| {f.stem} | {kind} | {lines[2].replace(' · ', ' | ')} | {feature(t)} |")
    for f in sorted((root / "armor").glob("*.md")):
        t = plain(f.read_text())
        if "Tier 1" in t:
            th = re.search(r"Base Thresholds:\s*([\d ]+/[\d ]+)", t)
            sc = re.search(r"Base Score:\s*(\d+)", t)
            armor.append(f"| {f.stem} | {th.group(1).strip() if th else '?'} | {sc.group(1) if sc else '?'} | {feature(f.read_text())} |")
    for f in sorted((root / "abilities").glob("*.md")):
        t = plain(f.read_text())
        m = re.search(r"Level 1 (\w+) (Ability|Spell|Grimoire)", t)
        if m:
            body = re.split(r"Recall Cost \d+\.?", t, 1)[-1].strip().replace("\n", " ")
            cards.setdefault(m.group(1), []).append(f"{f.stem} ({m.group(2).lower()}): {body[:150]}{'…' if len(body) > 150 else ''}")
    tables = ("\n\nTier 1 weapons (damage at Proficiency 1):\n\n| Weapon | Kind | Trait | Range | Damage | Burden | Feature |\n|---|---|---|---|---|---|---|\n"
              + "\n".join(weapons)
              + "\n\nTier 1 armour (add the level, 1, to both thresholds; Armor Slots = Armor Score):\n\n| Armour | Base thresholds | Armor Score | Feature |\n|---|---|---|---|\n"
              + "\n".join(armor)
              + "\n\nLevel 1 domain cards:\n" + "\n".join(f"\n{d}:\n" + "\n".join(f"- {c}" for c in cs) for d, cs in sorted(cards.items())))
    official = (root / "core_rules" / "Character Creation.md").read_text().strip()
    (PACKS / "daggerheart" / "creation_official.md").write_text(
        "## Options (Daggerheart SRD 2.0)\n\n"
        "| Class | Domains | Evasion | Hit Points | Subclasses | Class items |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + f"\n\nAncestries: {names('ancestries')}.\n\nCommunities: {names('communities')}." + tables + "\n\n"
        "Look each one up (rules_lookup) for its features, background questions and domain cards before "
        "offering or recording them; record features exactly as written.\n\n"
        "## The official steps\n\n" + official + "\n")


def dnd(root: Path) -> None:
    src = root / "src"
    entries = []
    for f in sorted(src.glob("[0-9][0-9]_*.md")):
        if f.name.startswith("00_Legal"):
            continue
        text = f.read_text()
        m = re.match(r"#\s+(.+)", text)
        chapter = (m.group(1) if m else f.stem).strip()
        # spells, feats, magic items and monsters are smaller headings: split deeper there
        level = 4 if any(k in chapter for k in ("Spells", "Feats", "Magic Items", "Monsters", "Animals",
                                                 "Glossary", "Equipment", "Origins")) else 3
        for e in split_sections(text, chapter, chapter.lower(), level):
            if level == 4 and ": " in e["title"]:  # a spell or monster under its own name
                e["title"] = e["title"].split(": ", 1)[1]
            entries.append(e)
    classes = []
    for f in sorted((src / "03_Classes").glob("[0-9][0-9]_*.md")):
        if "Artificer" in f.name or f.stem.endswith("_Classes"):
            continue  # not part of the 2024 core rules
        name = f.stem.split("_", 1)[1]
        text = f.read_text()
        classes.append((name, text))
        for e in split_sections(text, name, "classes", 4):
            # "Rogue: Level 1: Sneak Attack" -> "Sneak Attack (Rogue level 1)"
            t = e["title"]
            m = re.match(rf"{name}: Level (\d+): (.+)", t)
            if m:
                e["title"] = f"{m.group(2)} ({name} level {m.group(1)})"
            elif t.startswith(f"{name}: {name} Subclass: "):
                e["title"] = t.split(": ", 2)[2] + f" ({name} subclass)"
            entries.append(e)
    write("dnd5e2024", entries, "D&D System Reference Document 5.2, © Wizards of the Coast LLC, "
          "licensed under the Creative Commons Attribution 4.0 International License "
          "(https://creativecommons.org/licenses/by/4.0/legalcode). Markdown conversion from "
          "github.com/gelatinous-labs/dndsrd5.2_markdown.\n\n" + (root / "License.md").read_text()[:400])

    # session 0: the options, from the SRD
    tidy = lambda t: re.sub(r" {2,}", " ", re.sub(r"(\w) (\w{1,3}) (\w)", lambda m: m.group(0), t))
    out = ["## Options (D&D SRD 5.2; the player's own Player's Handbook has more)"]
    out.append("\n### Classes\n")
    for name, text in classes:
        traits = dict(re.findall(r"^\|\s*([A-Z][^|]+?)\s*\|\s*(.+?)\s*\|\s*$", text, re.M))
        l1 = re.findall(r"^#### Level 1: (.+)$", text, re.M)
        sub = re.search(r"^### \w+ Subclass: (.+)$", text, re.M)
        out.append(f"**{name}**: hit die {traits.get('Hit Point Die', '?').split(' per')[0]}; primary "
                   f"{traits.get('Primary Ability', '?')}; saves {traits.get('Saving Throw Proficiencies', '?')}; "
                   f"skills {traits.get('Skill Proficiencies', '?')}; armour {traits.get('Armor Training', '?')}; "
                   f"weapons {traits.get('Weapon Proficiencies', '?')}; level 1 features: {', '.join(l1)}; "
                   f"subclass at level 3 (SRD): {sub.group(1) if sub else '?'}. Starting equipment: "
                   f"{traits.get('Starting Equipment', '?')}")
    origins = (src / "04_CharacterOrigins.md").read_text()
    out.append("\n### Backgrounds\n")
    for m in re.finditer(r"^#{2,4} \**(Acolyte|Criminal|Sage|Soldier)\**\s*\n(.*?)(?=^#{2,4} )", origins, re.M | re.S):
        body = " ".join(x.strip() for x in m.group(2).strip().splitlines() if x.strip())
        out.append(f"**{m.group(1)}**: {re.sub(r'[*]', '', body)}")
    out.append("\n### Species\n")
    species = origins[origins.index("### Species Descriptions"):]
    for m in re.finditer(r"^#### (\w+)\s*\n(.*?)(?=^#### |\Z)", species, re.M | re.S):
        body = m.group(2)
        size = re.search(r"\*\*Size:\*\*\s*(.+)", body)
        speed = re.search(r"\*\*Speed:\*\*\s*(.+)", body)
        feats = re.findall(r"^\*\*\*?([A-Z][^.*]+?)\.\*?\*\*", body, re.M)
        out.append(f"**{m.group(1)}**: {size.group(1).strip() if size else '?'}; speed "
                   f"{speed.group(1).strip() if speed else '?'}; traits: {', '.join(feats)}")
    feats = (src / "05_Feats.md").read_text()
    origin_feats = feats[feats.index("### Origin Feats"):feats.index("### General Feats")]
    out.append("\n### Origin feats\n")
    for m in re.finditer(r"^#### ([^\n]+)\n(.*?)(?=^#### |\Z)", origin_feats, re.M | re.S):
        body = " ".join(x.strip() for x in m.group(2).strip().splitlines() if x.strip())
        out.append(f"**{m.group(1)}**: {re.sub(r'[*]', '', body)[:260]}…")
    eq = (src / "06_Equipment.md").read_text()
    out.append("\n### Weapons and armour\n")
    for cap in ("Simple Melee Weapons", "Simple Ranged Weapons", "Martial Melee Weapons", "Martial Ranged Weapons",
                "Light Armor", "Medium Armor", "Heavy Armor", "Shield"):
        m = re.search(rf"^Table: {cap}.*\n\n((?:\|.*\n)+)", eq, re.M)
        if m:
            out.append(f"{cap}:\n\n" + re.sub(r" {2,}", " ", m.group(1)))
    spells = (src / "07_Spells.md").read_text()
    lists: dict[str, dict[str, list]] = {}
    for m in re.finditer(r"^#### (.+)\n\n\*(Level 1 \w+|\w+ Cantrip) \(([^)]+)\)", spells, re.M):
        lvl = "cantrips" if "Cantrip" in m.group(2) else "level 1"
        for cls in m.group(3).split(","):
            lists.setdefault(cls.strip(), {}).setdefault(lvl, []).append(m.group(1).strip())
    out.append("\n### Spells by class (cantrips; level 1)\n")
    for cls, d in sorted(lists.items()):
        out.append(f"**{cls}**: cantrips: {', '.join(d.get('cantrips', [])) or '—'}. Level 1: {', '.join(d.get('level 1', [])) or '—'}.")
    out.append("\nLook each one up (rules_lookup) for the exact text before recording it.")
    (PACKS / "dnd5e2024" / "creation_official.md").write_text("\n".join(out) + "\n")


CEPHEUS_BOOKS = ("book1", "book2", "book3", "vds")


def _plain_links(text: str) -> str:
    """[Gun Combat-0](skills.md#gun-combat) -> Gun Combat-0; drop the HTML spacers."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"(?m)^<br>\s*$\n?", "", text).replace("\\[", "[").replace("\\]", "]")


def cepheus_careers(text: str) -> list[dict]:
    """The career tables (six careers side by side) turned round: one entry per career."""
    blurbs = dict(re.findall(r"^\*\*([^*]+?):\*\* (.+)$", text.split("### Qualifying")[0], re.M))
    tables = text.split("## Career Tables", 1)[1].split("## Final Details", 1)[0]
    careers = {}
    for table in re.split(r"\n\s*\n", tables):
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table.splitlines() if r.startswith("|")]
        if not rows or rows[0][0] != "Career":
            continue
        names, section = rows[0][1:], ""
        for name in names:
            careers.setdefault(name, {})
        for r in rows[1:]:
            if set(r[0]) <= set("-: ") or len(r) == 1:
                continue
            if not r[0].isdigit():  # a section heading row ("Material Benefits | Athlete | ...") or a throw
                if r[1:] and r[1] == names[0]:
                    section = r[0]
                    continue
                for name, v in zip(names, r[1:]):
                    careers[name].setdefault("Throws", []).append(f"{r[0]} {v}")
                continue
            for name, v in zip(names, r[1:]):
                if v and v != "—":
                    careers[name].setdefault(section, []).append(f"{r[0]}: {v}")
    out = []
    for name, parts in careers.items():
        blurb = next((b for k, b in blurbs.items() if k.split()[0] == name.split()[0]), "")
        lines = [name, "", blurb] if blurb else [name]
        lines.append("Throws: " + "; ".join(parts.pop("Throws", [])))
        lines.append("The tables below are rolled on 1D6 (a benefit roll with a DM can reach 7); ranks go "
                     "by rank number, with the skill a rank gives in brackets.")
        for section, items in parts.items():
            label = {"Ranks and Skills": "Ranks", "Adv Education": "Advanced Education (Edu 8+ only)",
                     "Specialist": "Specialist Skills"}.get(section, section)
            lines.append(f"{label}: " + "; ".join(items))
        out.append({"title": name, "category": "careers", "text": "\n".join(lines)})
    return out


def cepheus(root: Path) -> None:
    """Cepheus Engine SRD: the open (OGL) rules descended from the Mongoose Traveller SRD."""
    src = root / "src"
    entries = split_sections(_plain_links((src / "introduction.md").read_text()), "Core Rules", "core rules", 3)
    creation = (src / "book1" / "character-creation.md").read_text()
    for book in CEPHEUS_BOOKS:
        for f in sorted((src / book).glob("*.md")):
            text = _plain_links(f.read_text())
            if f.name == "character-creation.md":
                text = re.sub(r"## Career Tables.*?(?=## Final Details)", "", text, flags=re.S)
            m = re.match(r"#\s+(.+)", text)
            chapter = re.sub(r"^Chapter \d+: |^Appendix \w: ", "", (m.group(1) if m else f.stem).strip())
            level = 4 if f.stem in ("equipment", "skills", "psionics") else 3
            for e in split_sections(text, chapter, chapter.lower(), level):
                if f.stem == "skills" and ": " in e["title"]:  # a skill under its own name
                    e["title"] = e["title"].split(": ", 1)[1].replace(" (Cascade Skill)", "")
                entries.append(e)
    entries += cepheus_careers(_plain_links(creation))
    legal = (src / "legal.md").read_text()
    ogl = legal[legal.index("## OPEN GAME LICENSE"):].strip()
    write("traveller", entries,
          "The rules reference in this pack is Open Game Content from the Cepheus Engine System Reference "
          "Document, Copyright © 2016 Samardan Press; Author Jason \"Flynn\" Kemp, converted to HTML/Markdown "
          "at github.com/orffen/cepheus-srd. It is derived from the Traveller System Reference Document, "
          "Copyright © 2008 Mongoose Publishing. All of it is used under the Open Game License v1.0a, "
          "reproduced below; the whole of reference.jsonl is Open Game Content.\n\n"
          "This project is unofficial and not affiliated with Far Future Enterprises, Mongoose Publishing, "
          "Samardan Press or Jason \"Flynn\" Kemp. Traveller is a trademark of Far Future Enterprises; "
          "Cepheus Engine and Samardan Press are the trademarks of Jason \"Flynn\" Kemp.\n\n" + ogl)

    # session 0: the options at a glance (the full career tables are in the reference)
    careers = cepheus_careers(_plain_links(creation))
    out = ["## Options (Cepheus Engine SRD)", "", "### Careers", "",
           "| Career | Qualify | Survival | Commission | Advancement | Re-enlist |", "|---|---|---|---|---|---|"]
    for c in careers:
        throws = dict(t.split(" ", 1) for t in re.search(r"Throws: (.+)", c["text"]).group(1).split("; "))
        out.append(f"| {c['title']} | " + " | ".join(throws.get(k, "—") for k in
                   ("Qualifications", "Survival", "Commission", "Advancement", "Re-enlistment")) + " |")
    out += ["", "Commission and Advancement are \"—\" for careers without ranks: those roll twice on the "
            "skill tables each term instead. Look the chosen career up (rules_lookup, e.g. 'Merchant') for "
            "its ranks, skill tables and benefits before each term's rolls.", ""]
    bg = creation[creation.index("## Background Skills"):creation.index("## Careers")]
    out.append(_plain_links(bg).replace("## Background Skills", "### Background skills").strip())
    skills = (src / "book1" / "skills.md").read_text()
    names = re.findall(r"^### (.+)$", skills[skills.index("## Skill Descriptions"):], re.M)
    out += ["", "### Skills", "", ", ".join(n.replace(" (Cascade Skill)", " (cascade)") for n in names) + ".",
            "Cascade skills need a speciality as soon as they're gained (Gun Combat → Slug Pistol, say); "
            "the other specialities of it then count as level 0."]
    eq = _plain_links((src / "book1" / "equipment.md").read_text())
    for cap, head in (("Armor", "| Armor |"), ("Melee weapons", "| Weapon | TL | Cost | Wgt | Range | Damage |"),
                      ("Ranged weapons", "| Weapon | TL | Cost | Wgt | RoF |")):
        m = re.search(rf"^{re.escape(head)}.*\n(?:\|.*\n)+", eq, re.M)
        if m:
            out += ["", f"### {cap}", "", m.group(0).strip()]
    out += ["", "Other gear: look it up (rules_lookup) for the price before buying."]
    (PACKS / "traveller" / "creation_official.md").write_text("\n".join(out) + "\n")


# ---- Ironsworn and Starforged (Datasworn JSON) ----------------------------------------------

def _ds_tables(node, out: dict) -> dict:
    """Every rollable table in the data, by id ('starforged/oracles/core/action')."""
    if isinstance(node, dict):
        if node.get("_id") and isinstance(node.get("rows"), list):
            out[node["_id"]] = node
        for v in node.values():
            _ds_tables(v, out)
    elif isinstance(node, list):
        for v in node:
            _ds_tables(v, out)
    return out


def _ds_row(row: dict) -> str:
    text = " / ".join(str(row[k]) for k in ("text", "text2", "text3") if row.get(k))
    return text


def _ds_table_text(t: dict, plain) -> str:
    rows = []
    for r in t["rows"]:
        if r.get("min") is None:
            continue
        roll = f"{r['min']}" if r["min"] == r["max"] else f"{r['min']}-{r['max']}"
        rows.append(f"{roll}: {plain(_ds_row(r))}")
    return "\n".join(rows)


def first_sentence(text: str) -> str:
    """A truth's summary, where the data has none (classic Ironsworn): its opening sentence."""
    m = re.match(r"(.{20,240}?[.!?])(\s|$)", text, re.S)
    return (m.group(1) if m else text[:240]).strip()


def datasworn(path: Path, pack: str) -> None:
    """Ironsworn or Starforged from Datasworn: one entry per move, asset, oracle table, truth,
    impact, stat and sample NPC; and oracles.json, every table for the GM's oracle tool."""
    d = json.loads(path.read_text())
    tables = _ds_tables(d, {})

    def plain(text: str, depth: int = 0) -> str:
        text = re.sub(r"\[([^\]]+)\]\(id:[^)]*\)", r"\1", str(text or ""))
        def table(m):
            t = tables.get(m.group(1))
            return ("\n" + _ds_table_text(t, lambda x: plain(x, depth + 1)) + "\n") if t and depth < 2 else ""
        text = re.sub(r"\{\{table[^:]*:([^}]+)\}\}", table, text)
        text = re.sub(r"\{\{[^}]*\}\}", "", text)
        return text.replace("__", "").strip()

    entries = []
    rolls = {"action_roll": "action roll", "progress_roll": "progress roll", "no_roll": "no roll",
             "special_track": "special track roll"}
    for cat in d["moves"].values():
        for m in cat["contents"].values():
            entries.append({"title": m["name"], "category": "moves",
                            "text": f"{m['name']} ({cat['name']} move; {rolls.get(m.get('roll_type'), m.get('roll_type'))})\n\n{plain(m['text'])}"})
    for cat in d["assets"].values():
        for a in cat["contents"].values():
            lines = [f"{a['name']} ({a['category']} asset)"]
            if a.get("requirement"):
                lines.append(f"Requirement: {plain(a['requirement'])}")
            for k, o in (a.get("options") or {}).items():
                lines.append(f"Write in: {o.get('label') or k}")
            for k, c in (a.get("controls") or {}).items():
                if c.get("field_type") == "condition_meter":
                    lines.append(f"{(c.get('label') or k).capitalize()}: a meter from 0 to {c.get('max')}")
            for i, ab in enumerate(a.get("abilities") or [], 1):
                lines.append(f"{i}. {'(marked at the start) ' if ab.get('enabled') else ''}{plain(ab['text'])}")
            entries.append({"title": a["name"], "category": "assets", "text": "\n".join(lines)})

    oracles = []
    def walk(node, trail):
        for t in (node.get("contents") or {}).values():
            name = " / ".join(trail + [t["name"]])
            if isinstance(t.get("rows"), list):
                rows = [[r["min"], r["max"], plain(_ds_row(r))] for r in t["rows"] if r.get("min") is not None]
                oracles.append({"name": name, "dice": t.get("dice", "1d100"), "rows": rows})
                entries.append({"title": f"Oracle: {name}", "category": "oracles",
                                "text": (plain(t.get("summary") or "") + "\n\n" + _ds_table_text(t, plain)).strip()})
            else:  # a group of tables rolled together (a planet's atmosphere, life...)
                walk(t, trail + [t["name"]])
        for c in (node.get("collections") or {}).values():
            walk(c, trail + [c["name"]])
    for c in d["oracles"].values():
        walk(c, [c["name"]])

    for t in d.get("truths", {}).values():
        opts = []
        for o in t.get("options") or []:
            roll = f"{o['min']}-{o['max']}" if o.get("min") else ""
            opts.append(f"{roll}: {plain(o.get('summary') or '')}\n{plain(o.get('description') or '')}"
                        + (f"\nQuest starter: {plain(o['quest_starter'])}" if o.get("quest_starter") else ""))
        entries.append({"title": f"Truth: {t['name']}", "category": "truths", "text": "\n\n".join(opts)})
        oracles.append({"name": f"Truths / {t['name']}", "dice": "1d100",
                        "rows": [[o["min"], o["max"], plain(o.get("summary") or "") or first_sentence(plain(o.get("description") or ""))]
                                 for o in t.get("options") or [] if o.get("min")]})

    rules = d["rules"]
    for k, st in rules["stats"].items():
        entries.append({"title": f"Stat: {k}", "category": "rules", "text": f"{k}: {st['description']}"})
    for k, m in rules["condition_meters"].items():
        entries.append({"title": f"Condition meter: {k}", "category": "rules",
                        "text": f"{k} (0 to {m['max']}, starts at {m['value']}{', shared by the party' if m.get('shared') else ''}): {m['description']}"})
    for group in rules["impacts"].values():
        entries.append({"title": f"Impacts: {group['label']}", "category": "rules", "text": plain(group.get("description") or "")})
        for k, imp in group["contents"].items():
            stops = f" While marked, {', '.join(imp['prevents_recovery'])} can't be raised." if imp.get("prevents_recovery") else ""
            entries.append({"title": imp["label"].capitalize(), "category": "impacts",
                            "text": f"{imp['label']} ({group['label']}{', permanent' if imp.get('permanent') else ''}): {plain(imp['description'])}{stops}"})
    for k, tr in rules["special_tracks"].items():
        entries.append({"title": f"Legacy track: {tr['label']}" if "legacy" in k else f"Track: {tr['label']}",
                        "category": "rules", "text": f"{tr['label']}: {tr['description']}"})
    for cat in (d.get("npcs") or {}).values():
        for n in (cat.get("contents") or {}).values():
            text = [f"{n['name']} ({cat['name']}; rank {n.get('rank', '?')}, {n.get('nature', '')})", plain(n.get("summary") or ""),
                    plain(n.get("description") or ""), "Features: " + "; ".join(plain(x) for x in n.get("features") or []),
                    "Drives: " + "; ".join(plain(x) for x in n.get("drives") or []),
                    "Tactics: " + "; ".join(plain(x) for x in n.get("tactics") or [])]
            entries.append({"title": n["name"], "category": "npcs", "text": "\n".join(t for t in text if t.strip(": "))})

    title = d["title"]
    write(pack, entries,
          f"The rules reference and oracle tables in this pack are from {title} by Shawn Tomkin "
          f"(ironswornrpg.com), used under the Creative Commons Attribution 4.0 International license "
          f"(https://creativecommons.org/licenses/by/4.0/), by way of the Datasworn data "
          f"(github.com/rsek/datasworn). They have been reformatted as searchable text and tables; the "
          f"rules summary and session 0 steps are our own summary of the same rules.\n\n"
          f"This work is based on {title} (found at www.ironswornrpg.com), created by Shawn Tomkin, and "
          f"licensed for our use under the Creative Commons Attribution 4.0 International license. This "
          f"project is unofficial and not affiliated with Shawn Tomkin or Tomkin Press.")
    (PACKS / pack / "oracles.json").write_text(json.dumps(oracles, ensure_ascii=False) + "\n")
    print(f"{pack}: {len(oracles)} oracle tables")

    # session 0: the options at a glance (each asset's text is in the reference)
    out = ["## Options (from the rules data)", ""]
    for cat in d["assets"].values():
        out.append(f"### {re.sub(r' Assets$', '', cat['name'])} assets\n\n" + ", ".join(a["name"] for a in cat["contents"].values()) + ".\n")
    launch = d["oracles"].get("campaign_launch")
    bg = (launch or {}).get("contents", {}).get("background_assets")
    if bg:
        out += ["### Backgrounds (two paths each)", "", _ds_table_text(bg, plain), ""]
    (PACKS / pack / "creation_official.md").write_text("\n".join(out).strip() + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daggerheart", type=Path)
    ap.add_argument("--dnd", type=Path)
    ap.add_argument("--cepheus", type=Path, help="a checkout of github.com/orffen/cepheus-srd")
    ap.add_argument("--datasworn", type=Path, help="a checkout of github.com/rsek/datasworn")
    a = ap.parse_args()
    if a.daggerheart:
        daggerheart(a.daggerheart)
    if a.dnd:
        dnd(a.dnd)
    if a.cepheus:
        cepheus(a.cepheus)
    if a.datasworn:
        datasworn(a.datasworn / "datasworn" / "starforged" / "starforged.json", "starforged")
        datasworn(a.datasworn / "datasworn" / "classic" / "classic.json", "ironsworn")
