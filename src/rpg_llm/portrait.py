"""Player-character portraits: the GM model describes the character, the image generator paints
them small (512x512, a few seconds locally). Candidates made during campaign setup wait in the
cache until the campaign is created; campaign portraits keep a history in art/portraits/."""

import json
import re
import secrets
from pathlib import Path

import yaml

from rpg_llm import images, prompts
from rpg_llm.config import ImageGen
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign, slugify

SIZE = 512
DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {"appearance": {"type": "string"}, "prompt": {"type": "string"}},
    "required": ["appearance", "prompt"],
    "additionalProperties": False,
}


async def describe(dm: LLMClient, system: str, premise: str, appearance: str = "",
                   avoid: list[str] | None = None) -> dict:
    """Appearance + image prompt. A given appearance (the player's edit) is kept as is."""
    appearance_text = (f"\nThe player has described the look; keep it exactly: {appearance.strip()}\n"
                       if appearance.strip() else "")
    avoid_text = ("\nEarlier attempts (vary the pose, expression, framing and background):\n"
                  + "\n".join(f"- {a[:200]}" for a in (avoid or [])[-3:]) + "\n") if avoid else ""
    d = await dm.json([{"role": "user", "content": prompts.PORTRAIT_TASK.format(
        system=system or "any setting", premise=premise.strip() or "(none: invent a fitting character)",
        appearance=appearance_text, avoid=avoid_text)}], DESCRIBE_SCHEMA, max_tokens=500)
    if appearance.strip():
        d["appearance"] = appearance.strip()
    return d


async def paint(cfg: ImageGen, look: str, prompt: str) -> bytes:
    return await images.generate(cfg, images.portrait_prompt(look, prompt), SIZE, SIZE)


# ---- candidates during setup -------------------------------------------------

def _cache(vault_root: Path) -> Path:
    p = vault_root / ".cache" / "portraits"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_candidate(vault_root: Path, webp: bytes, info: dict) -> str:
    token = secrets.token_hex(8)
    (_cache(vault_root) / f"{token}.webp").write_bytes(webp)
    (_cache(vault_root) / f"{token}.json").write_text(json.dumps(info))
    return token


def candidate_path(vault_root: Path, token: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        return None
    p = _cache(vault_root) / f"{token}.webp"
    return p if p.exists() else None


# ---- campaign portraits ----------------------------------------------------------

def history(campaign: Campaign) -> list[str]:
    d = campaign.path("art/portraits")
    return sorted(p.name for p in d.glob("*.webp")) if d.exists() else []


def current(campaign: Campaign) -> str | None:
    name = campaign.meta.get("portrait")
    return name if name and name in history(campaign) else None


def add(campaign: Campaign, webp: bytes, prompt: str = "") -> str:
    """Store a new portrait and make it the current one. Returns its file name."""
    name = f"{len(history(campaign)) + 1:03d}.webp"
    campaign.path("art/portraits").mkdir(parents=True, exist_ok=True)
    campaign.path(f"art/portraits/{name}").write_bytes(webp)
    choose(campaign, name, prompt)
    return name


def choose(campaign: Campaign, name: str, prompt: str | None = None) -> None:
    if name not in history(campaign):
        raise ValueError(f"no portrait {name}")
    meta = {**campaign.meta, "portrait": name}
    if prompt:
        meta["portrait_prompt"] = prompt
    campaign.save_meta(meta)


# ---- the people the character meets ---------------------------------------------------------
# Painted from the cast record (role, looks, pronouns) with no model call in between. Kept apart
# from cast.yaml, whose versions are rewound with the story: a portrait isn't undone by a rewind.

PEOPLE_DIR = "art/people"
PEOPLE_INDEX = "art/people/index.yaml"
GENDER = {"he": "man", "she": "woman", "they": "person"}


def people_index(campaign: Campaign) -> dict[str, str]:
    text = campaign.read(PEOPLE_INDEX)
    data = yaml.safe_load(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def person_file(index: dict[str, str], names: list[str]) -> str | None:
    for n in names:
        if n.lower() in index:
            return index[n.lower()]
    return None


NOT_HUMAN = re.compile(r"\b(ai|a\.i\.|computer|machine|robot|android|droid|drone|synth|entity|"
                       r"creature|spirit|ghost|construct|intelligence|beast|alien)\b", re.I)


MACHINE_MIND = re.compile(r"\b(ai|a\.i\.|computer|intelligence|mind)\b", re.I)


def not_human(p: dict) -> bool:
    return ((p.get("pronouns") or "").startswith("it")
            or bool(NOT_HUMAN.search(p.get("role") or "")))


async def paint_person(cfg: ImageGen, look: str, p: dict, system: str) -> bytes:
    prompt = person_prompt(p, system)
    if not_human(p):
        return await images.generate(cfg, images.presence_prompt(look, prompt), SIZE, SIZE,
                                     negative=images.PRESENCE_NEGATIVE)
    return await paint(cfg, look, prompt)


def person_prompt(p: dict, system: str) -> str:
    role, look = (p.get("role") or "").strip(), (p.get("look") or "").strip()
    pron = (p.get("pronouns") or "").split("/")[0]
    if not_human(p):  # the ship's AI or a strange entity: painted as a presence, no face
        text = f"{role or 'a strange presence'}."
        if look:
            text += f" It appears as: {look}."
        if MACHINE_MIND.search(role):
            text += " Shown as a glowing core of light or a screen interface, not a robot body."
    else:
        who = GENDER.get(pron, "")
        text = ", ".join(b for b in (f"A {who}" if who else "A character", role) if b) + "."
        if look:
            text += f" Appearance: {look}."
        text += " Dressed for their work and place, not in armour unless described."
    return f"{text} From a {system or 'role-playing'} story."


def add_person(campaign: Campaign, names: list[str], webp: bytes) -> str:
    index = people_index(campaign)
    base = slugify(names[0])[:40] or "person"
    n = 1
    while campaign.path(f"{PEOPLE_DIR}/{base}-{n:02d}.webp").exists():
        n += 1
    name = f"{base}-{n:02d}.webp"
    campaign.path(PEOPLE_DIR).mkdir(parents=True, exist_ok=True)
    campaign.path(f"{PEOPLE_DIR}/{name}").write_bytes(webp)
    for key in names:
        index[key.lower()] = name
    campaign.write(PEOPLE_INDEX, yaml.safe_dump(index, sort_keys=True, allow_unicode=True))
    return name


def people_files(campaign: Campaign) -> set[str]:
    return set(people_index(campaign).values())
