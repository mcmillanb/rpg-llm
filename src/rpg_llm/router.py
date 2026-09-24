"""The router/archivist's fast jobs: scene tracking, the per-turn gatekeeper, and the audit.

Every router call shares one system prompt (instructions + gazetteer + filed-scene index) that
only changes after compaction, so the router server can keep it in its prompt cache and only
process the short task-specific part.
"""

import asyncio
import logging

from rpg_llm import prompts, wiki
from rpg_llm.llm import LLMClient
from rpg_llm.vault import Campaign, Scene, State, estimate_tokens

log = logging.getLogger(__name__)

TRACK_SCHEMA = {  # location first: naming where they are before judging makes it more reliable
    "type": "object",
    "properties": {
        "movement_quote": {"type": "string"},
        "location_now": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "transition": {"type": "boolean"},
        "confidence": {"type": "number"},
        "new_location": {"type": ["string", "null"]},
        "scene_title": {"type": ["string", "null"]},
    },
    "required": ["movement_quote", "location_now", "reason", "transition", "confidence", "new_location",
                 "scene_title"],
    "additionalProperties": False,
}

GATE_SCHEMA = {  # reason first so the model decides before it lists
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "scenes": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["reason", "notes", "scenes"],
    "additionalProperties": False,
}

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["keep", "confidence", "reason"],
    "additionalProperties": False,
}

NOTES_TOKEN_CAP = 2000


def router_system(campaign: Campaign, state: State) -> str:
    return prompts.ROUTER_SYSTEM.format(
        brief=campaign.brief.strip() or "(empty)",
        gazetteer=wiki.gazetteer_text(campaign.gazetteer()),
        scenes=wiki.scenes_text(campaign, state),
    )


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n // 3] + " [...] " + text[-(2 * n) // 3:]


# ---- scene tracking ---------------------------------------------------------

async def _verdict(campaign: Campaign, router: LLMClient, state: State, messages: list[dict],
                   user_idx: int) -> dict:
    """Ask whether the exchange starting at messages[user_idx] opens a new scene. `messages`
    may run past that exchange (backfill); only the current scene up to it is shown."""
    current = state.current
    upto = messages[: user_idx + 2]
    recent = [m for m in upto if m["id"] >= current.start][-6:]
    user_id = messages[user_idx]["id"]
    recent_text = "\n\n".join(
        f"{'>>> LATEST ' if m['id'] >= user_id else ''}"
        f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {_clip(m['content'], 1500)}"
        for m in recent)
    return await router.json(
        [{"role": "system", "content": router_system(campaign, state)},
         {"role": "user", "content": prompts.TRACK_TASK.format(
             location=current.location or "unknown", recent=recent_text)}],
        TRACK_SCHEMA, max_tokens=400)


def _apply(state: State, verdict: dict, user_id: int, threshold: float) -> bool:
    """Record a verdict on the state. Returns True if a new scene was opened."""
    if verdict.get("transition") and verdict.get("confidence", 0) >= threshold:
        ending = state.current
        ending.status = "closed_provisional"
        ending.title = ending.title or verdict.get("scene_title")
        state.scenes.append(Scene(
            id=ending.id + 1, start=user_id, location=verdict.get("new_location"),
            confidence=verdict.get("confidence"), reason=verdict.get("reason")))
        state.fold = None
        return True
    if not state.current.location and verdict.get("location_now"):
        state.current.location = verdict["location_now"]
    return False


async def track(campaign: Campaign, router: LLMClient, threshold: float) -> dict | None:
    """Run after each DM reply. Opens a new provisional scene if the router is confident the
    latest exchange moved somewhere new. Returns the verdict (or None if skipped)."""
    state = campaign.load_state()
    messages = campaign.messages()
    if len(messages) < 2 or messages[-1]["role"] != "assistant" or messages[-2]["role"] != "user":
        return None
    user_msg = messages[-2]
    if user_msg["id"] <= state.current.start:
        return None  # this exchange already opened the current scene
    verdict = await _verdict(campaign, router, state, messages, len(messages) - 2)

    fresh = campaign.load_state()  # re-read: the player may have regenerated meanwhile
    if fresh.current.id != state.current.id or campaign.messages()[-1]["id"] != messages[-1]["id"]:
        return verdict
    _apply(fresh, verdict, user_msg["id"], threshold)
    campaign.save_state(fresh)
    return verdict


async def backfill(campaign: Campaign, router: LLMClient, threshold: float,
                   progress=None) -> int:
    """Segment an imported history into scenes by replaying scene tracking over every
    exchange. Resumable: starts after the last exchange already tracked. Returns scenes opened."""
    state = campaign.load_state()
    messages = campaign.messages()
    opened = 0
    done_until = state.tracked_until or 0
    user_idxs = [i for i, m in enumerate(messages[:-1])
                 if m["role"] == "user" and messages[i + 1]["role"] == "assistant"
                 and m["id"] > max(done_until, state.current.start)]
    for n, i in enumerate(user_idxs, 1):
        verdict = await _verdict(campaign, router, state, messages, i)
        opened += _apply(state, verdict, messages[i]["id"], threshold)
        state.tracked_until = messages[i + 1]["id"]
        campaign.save_state(state)
        if progress:
            progress(n, len(user_idxs), verdict)
    return opened


def undo_scenes_from(campaign: Campaign, first_dead_id: int) -> None:
    """After a regenerate/edit supersedes messages, drop any scene that began at or after them."""
    state = campaign.load_state()
    changed = False
    while len(state.scenes) > 1 and state.current.start >= first_dead_id \
            and state.scenes[-2].status != "compacted":
        state.scenes.pop()
        state.scenes[-1].status = "open"
        changed = True
    if changed:
        state.fold = None
        campaign.save_state(state)


# ---- gatekeeper -------------------------------------------------------------

async def gatekeep(campaign: Campaign, router: LLMClient | None, message: str,
                   last_reply: str, timeout: float) -> tuple[str | None, dict]:
    """Before each DM turn: pick wiki extracts for this message. Name/alias matching always
    runs; the router call (if given) catches indirect references. Returns (notes, info)."""
    gazetteer = campaign.gazetteer()
    state = campaign.load_state()
    info: dict = {"alias_hits": [], "router": None}
    if not gazetteer:
        return None, info

    # names in the GM's last reply are already in play, so matching them adds nothing
    in_play = {e["path"] for e in wiki.mentioned(gazetteer, last_reply)}
    paths = [e["path"] for e in wiki.mentioned(gazetteer, message) if e["path"] not in in_play]
    info["alias_hits"] = list(paths)
    scene_ids: list[int] = []
    if router is not None:
        try:
            pick = await asyncio.wait_for(router.json(
                [{"role": "system", "content": router_system(campaign, state)},
                 {"role": "user", "content": prompts.GATEKEEP_TASK.format(
                     last_reply=_clip(last_reply, 1200) or "(none)", message=message)}],
                GATE_SCHEMA, max_tokens=300), timeout)
            info["router"] = pick
            known = {e["path"] for e in gazetteer}
            paths += [p for p in pick.get("notes", [])[:3] if p in known and p not in paths]
            filed = {s.id for s in state.scenes if s.status == "compacted"}
            scene_ids = [i for i in pick.get("scenes", []) if i in filed][:1]
        except (TimeoutError, Exception) as e:  # never block play on the router
            info["router"] = f"skipped: {type(e).__name__}"
            log.warning("gatekeeper router call failed: %r", e)

    parts, used = [], 0
    by_path = {e["path"]: e for e in gazetteer}
    for p in paths:
        e = by_path[p]
        block = f"## {e['name']} ({e['type']}; wiki: {p})\n{wiki.current_state(campaign, p)}"
        _, body = wiki.split_note(campaign.read(p))
        last_visit = body.rsplit("### ", 1)[-1].strip() if "### " in body else ""
        if last_visit:
            block += f"\nMost recent: {last_visit}"
        used += estimate_tokens(block)
        if used > NOTES_TOKEN_CAP:
            break
        parts.append(block)
    for sid in scene_ids:
        s = next(s for s in state.scenes if s.id == sid)
        _, body = wiki.split_note(campaign.read(s.note or ""))
        block = f"## Scene {sid}: {s.title}\n{body.strip()}"
        used += estimate_tokens(block)
        if used > NOTES_TOKEN_CAP:
            break
        parts.append(block)
    info["injected"] = paths + [f"scene {i}" for i in scene_ids]
    return ("\n\n".join(parts) or None), info


# ---- audit ------------------------------------------------------------------

async def audit(campaign: Campaign, router: LLMClient) -> list[dict]:
    """Before compaction: re-check each unaudited provisional boundary with hindsight and merge
    scenes back together where the router now thinks the split was wrong."""
    results = []
    state = campaign.load_state()
    messages = campaign.messages()
    i = 1
    while i < len(state.scenes):
        prev, scene = state.scenes[i - 1], state.scenes[i]
        if prev.status != "closed_provisional" or scene.audited:
            i += 1
            continue
        before = [m for m in messages if m["id"] < scene.start][-4:]
        after = [m for m in messages if m["id"] >= scene.start][:8]
        excerpt = "\n\n".join(
            f"{'>>> ' if m['id'] == scene.start else ''}"
            f"{'PLAYER' if m['role'] == 'user' else 'GM'}: {_clip(m['content'], 1200)}"
            for m in before + after)
        verdict = await router.json(
            [{"role": "system", "content": router_system(campaign, state)},
             {"role": "user", "content": prompts.AUDIT_TASK.format(
                 old_location=prev.location or "unknown",
                 new_location=scene.location or "unknown", excerpt=excerpt)}],
            AUDIT_SCHEMA, max_tokens=300)
        results.append({"scene": scene.id, **verdict})
        if not verdict.get("keep", True) and verdict.get("confidence", 0) >= 0.6:
            del state.scenes[i]  # merge into prev; prev keeps its status and title
            if i == len(state.scenes):
                prev.status = "open"
            continue
        scene.audited = True
        i += 1
    for n, s in enumerate(state.scenes, 1):  # ids stay positional after merges
        s.id = n
    campaign.save_state(state)
    return results
