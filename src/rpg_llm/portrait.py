"""Player-character portraits: the GM model describes the character, the image generator paints
them small (512x512, a few seconds locally). Candidates made during campaign setup wait in the
cache until the campaign is created; campaign portraits keep a history in art/portraits/."""

import json
import re
import secrets
from pathlib import Path

from rpg_llm import images, prompts
from rpg_llm.config import ImageGen
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign

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
