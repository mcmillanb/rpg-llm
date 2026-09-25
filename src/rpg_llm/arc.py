"""The hidden story arc: GM-only guidance (conflict, NPCs, beats, climaxes, secrets) written at
campaign setup and revised between sessions when play diverges. Never shown to the player."""

import re
import time

from rpg_llm import prompts, wiki
from rpg_llm.llm import NO_THINKING, LLMClient
from rpg_llm.vault import Campaign

FILE = "arc.md"
HISTORY_DIR = "arc-history"
STAKES = {"brutal": "brutal (failure, loss and death when earned)",
          "normal": "normal (real setbacks; death only if reckless)",
          "low": "low (cinematic, no lasting harm)"}

REVISE_SCHEMA = {
    "type": "object",
    "properties": {"diverged": {"type": "boolean"}, "reason": {"type": "string"},
                   "arc": {"type": "string"}},
    "required": ["diverged", "reason", "arc"],
    "additionalProperties": False,
}


def is_private(path: str) -> bool:
    """Paths the play page must never serve."""
    p = path.strip().lstrip("./")
    return p == FILE or p.startswith(HISTORY_DIR)


def _strip(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    return re.sub(r"^```(?:markdown)?\n|\n```$", "", text).strip()


def _save(campaign: Campaign, text: str, why: str) -> None:
    old = campaign.read(FILE)
    if old.strip():
        n = len(list(campaign.path(HISTORY_DIR).glob("*.md"))) if campaign.path(HISTORY_DIR).exists() else 0
        campaign.write(f"{HISTORY_DIR}/{n + 1:03d}.md", old)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    campaign.write(FILE, f"{text.strip()}\n\n<!-- {stamp}: {why} -->\n")


async def generate(campaign: Campaign, archiver: LLMClient) -> str:
    """Write the arc. For a campaign already under way, it builds from the brief."""
    meta = campaign.meta
    started = any(s.status == "compacted" for s in campaign.load_state().scenes) or \
        len(campaign.messages()) > 2
    situation = (f"\nThe campaign is already under way. Campaign brief:\n{campaign.brief}\n"
                 if started else f"\nPremise:\n{(meta.get('premise') or '').strip() or '(none given: '
                                 'invent one that suits the game)'}\n")
    msg = await archiver.chat(
        [{"role": "user", "content": prompts.ARC_TASK.format(
            system=meta.get("system") or "unspecified",
            consequences=STAKES.get(meta.get("consequences"), STAKES["normal"]),
            situation=situation)}],
        max_tokens=2500, temperature=0.8, extra_body=NO_THINKING)
    text = _strip(msg.get("content") or "")
    if not text:
        raise RuntimeError("the model returned an empty arc")
    _save(campaign, text, "written" if not started else "written mid-campaign from the brief")
    return text


async def revise(campaign: Campaign, archiver: LLMClient, filed: list[dict]) -> dict | None:
    """After filing scenes: update Progress, and revise the rest if play diverged."""
    arc = campaign.read(FILE)
    if not arc.strip() or not filed:
        return None
    scenes = "\n\n".join(wiki.split_note(campaign.read(f["note"]))[1].strip() for f in filed)
    arc_body = re.sub(r"\n*<!--.*?-->\s*$", "", arc, flags=re.S)
    result = await archiver.json(
        [{"role": "user", "content": prompts.ARC_REVISE_TASK.format(
            arc=arc_body, brief=campaign.brief, scenes=scenes)}],
        REVISE_SCHEMA, max_tokens=3000)
    text = _strip(result.get("arc") or "")
    if len(text) < 0.4 * len(arc_body):  # a truncated or empty answer must not wipe the arc
        return {"diverged": False, "reason": "revision skipped: answer too short"}
    why = ("revised: " if result.get("diverged") else "progress updated: ") + result.get("reason", "")
    _save(campaign, text, why)
    return {"diverged": bool(result.get("diverged")), "reason": result.get("reason", "")}
