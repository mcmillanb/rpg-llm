"""Build the rules reference bundled with each system pack from the free System Reference
Documents, as searchable entries for the GM's rules_lookup tool.

    uv run python scripts/build_srd.py --daggerheart PATH --dnd PATH

--daggerheart: a checkout of github.com/JHerrin00/daggerheart-srd-2.0 (Daggerheart SRD 2.0,
  Darrington Press Community Gaming License). One entry per file (class, ancestry, community,
  domain card, weapon, armour, item, adversary, environment...); the core rules split by section.
--dnd: a checkout of github.com/gelatinous-labs/dndsrd5.2_markdown (D&D SRD 5.2, CC-BY-4.0).
  The chapters split by heading (spells, feats, magic items, monsters, rules glossary...).

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
    files = sorted(src.glob("[0-9][0-9]_*.md")) + sorted((src / "03_Classes").glob("[0-9][0-9]_*.md"))
    entries = []
    for f in files:
        if "Artificer" in f.name or f.name.startswith("00_Legal"):
            continue  # not part of the 2024 rules
        text = f.read_text()
        m = re.match(r"#\s+(.+)", text)
        chapter = (m.group(1) if m else f.stem).strip()
        # spells, feats, magic items and monsters are smaller headings: split deeper there
        level = 4 if any(k in chapter for k in ("Spells", "Feats", "Magic Items", "Monsters", "Animals",
                                                 "Glossary", "Equipment", "Origins")) else 3
        for e in split_sections(text, chapter, chapter.lower(), level):
            # a spell or monster under its own name, not "Spells: Fireball"
            if level == 4 and ": " in e["title"]:
                e["title"] = e["title"].split(": ", 1)[1]
            entries.append(e)
    write("dnd5e2024", entries, "D&D System Reference Document 5.2, © Wizards of the Coast LLC, "
          "licensed under the Creative Commons Attribution 4.0 International License "
          "(https://creativecommons.org/licenses/by/4.0/legalcode). Markdown conversion from "
          "github.com/gelatinous-labs/dndsrd5.2_markdown.\n\n" + (root / "License.md").read_text()[:400])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--daggerheart", type=Path)
    ap.add_argument("--dnd", type=Path)
    a = ap.parse_args()
    if a.daggerheart:
        daggerheart(a.daggerheart)
    if a.dnd:
        dnd(a.dnd)
