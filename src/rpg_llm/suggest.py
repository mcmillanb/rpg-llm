"""LLM suggestions for setting up a campaign: which game systems the GM model knows, and
starting premises."""

import json
import random
import re
import time
from pathlib import Path

from rpg_llm import prompts
from rpg_llm.llm import NO_THINKING, LLMClient

INITIALS = "ABCDEFGHIJKLMNOPRSTWY"

SYSTEMS_SCHEMA = {
    "type": "object",
    "properties": {"systems": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "genre": {"type": "string"},
                       "blurb": {"type": "string"}},
        "required": ["name", "genre", "blurb"], "additionalProperties": False}}},
    "required": ["systems"], "additionalProperties": False,
}


def _cache_file(vault_root: Path, model: str) -> Path:
    return vault_root / ".cache" / f"systems-{re.sub(r'[^A-Za-z0-9._-]+', '_', model)}.json"


async def systems(dm: LLMClient, vault_root: Path, refresh: bool = False) -> dict:
    """What the GM model says it can run. Cached per model: its knowledge doesn't change."""
    cache = _cache_file(vault_root, dm.slot.model)
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    data = await dm.json([{"role": "user", "content": prompts.SYSTEMS_TASK}],
                         SYSTEMS_SCHEMA, max_tokens=3000)
    seen, items = set(), []
    for s in data.get("systems", []):
        name = (s.get("name") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            items.append({"name": name, "genre": s.get("genre", ""), "blurb": s.get("blurb", "")})
    out = {"model": dm.slot.model, "systems": items, "at": time.time()}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return out


async def premise(dm: LLMClient, system: str, seed: str = "", avoid: list[str] | None = None,
                  tone: str = "", style: str = "") -> str:
    """A starting premise. `seed` is the player's own idea to build on; `avoid` holds earlier
    suggestions so "try another" gives something different."""
    seed_text = f"\nThe player's idea, to build on and keep: {seed.strip()}\n" if seed.strip() else ""
    if not seed.strip():  # models reuse a few names (Vane, Voss, Kaelen...): pick initials for it
        first, last = random.choice(INITIALS), random.choice(INITIALS)
        seed_text += f"\nName the character with the initials {first}. {last}. (a name that fits the setting).\n"
    avoid = [a for a in (avoid or []) if a.strip()][-4:]
    avoid_text = ("\nAlready suggested (write something clearly different: another character, "
                  "place and hook):\n" + "\n".join(f"- {a[:300]}" for a in avoid) + "\n") if avoid else ""
    msg = await dm.chat(
        [{"role": "user", "content": prompts.PREMISE_TASK.format(
            system=system.strip() or "any setting you know well",
            tone=f"Tone and feel: {tone.strip()}\n" if tone.strip() else "",
            mood=prompts.PREMISE_MOODS.get(style, "") + ("\n" if style in prompts.PREMISE_MOODS else ""),
            seed=seed_text,
            avoid=avoid_text)}],
        max_tokens=600, temperature=1.0, extra_body=NO_THINKING)
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    return re.sub(r"^(premise|opening premise)\s*:\s*", "", text, flags=re.I)


def tone_for(vault_root: Path, system: str) -> str:
    """The picker's one-line description of a game system, used as the GM's default tone.
    Looks the system up in any cached list (whichever model wrote it)."""
    name = (system or "").strip().lower()
    if not name:
        return ""
    for f in sorted((vault_root / ".cache").glob("systems-*.json")) if (vault_root / ".cache").exists() else []:
        try:
            for s in json.loads(f.read_text()).get("systems", []):
                if s.get("name", "").strip().lower() == name and s.get("blurb"):
                    return s["blurb"].strip()
        except (OSError, ValueError):
            continue
    return ""
