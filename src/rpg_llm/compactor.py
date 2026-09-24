"""Between-session compaction: audit boundaries, then file every closed scene (except the
current one) into the wiki, gazetteer, timeline and campaign brief.

Only files things elsewhere. The transcript is never touched, so any mistake can be rebuilt.
"""

import asyncio
import logging
import re
import time

from rpg_llm import prompts, router, wiki
from rpg_llm.llm import NO_THINKING, LLMClient
from rpg_llm.vault import Campaign, State, slugify

log = logging.getLogger(__name__)

_ENTITY = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["location", "npc", "ship", "vehicle", "organisation",
                                            "item", "other"]},
        "role": {"type": "string"},
        "visit": {"type": "string"},
        "current_state": {"type": "string"},
    },
    "required": ["name", "aliases", "visit", "current_state"],
    "additionalProperties": False,
}

ARCHIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "timeline": {"type": "string"},
        "location": {"anyOf": [_ENTITY, {"type": "null"}]},
        "npcs": {"type": "array", "items": _ENTITY},
        "others": {"type": "array", "items": _ENTITY},
    },
    "required": ["title", "summary", "timeline", "location", "npcs", "others"],
    "additionalProperties": False,
}

TRANSCRIPT_CHAR_LIMIT = 250_000  # hard ceiling even for huge context windows


async def transcript_limit(archiver: LLMClient) -> int:
    """How much transcript (in characters) fits in the archiver's context, leaving ~40% for
    the prompt, the known-entity list and a long JSON answer. Unknown window: assume 32k."""
    window = await archiver.context_window() or 32768
    return min(TRANSCRIPT_CHAR_LIMIT, int(window * 0.6 * 3))  # ~3 chars/token, conservative


def _link(entry: dict) -> str:
    return f"[[{entry['path'][:-3]}|{entry['name']}]]"


async def file_scene(campaign: Campaign, archiver: LLMClient, state: State, scene_id: int,
                     lock: asyncio.Lock) -> dict:
    scene = next(s for s in state.scenes if s.id == scene_id)
    msgs = wiki.scene_messages(campaign, state, scene_id)
    transcript = wiki.format_transcript(msgs, limit_chars=await transcript_limit(archiver))
    gazetteer = campaign.gazetteer()
    known = wiki.mentioned(gazetteer, transcript)
    known_text = "\n".join(f"- {e['name']}: {wiki.current_state(campaign, e['path'])}"
                           for e in known) or "(none)"
    data = await archiver.json(
        [{"role": "system", "content": prompts.ARCHIVE_SYSTEM},
         {"role": "user", "content": prompts.ARCHIVE_SCENE_TASK.format(
             campaign=campaign.meta.get("name", campaign.slug), brief=campaign.brief.strip(),
             known=known_text,
             scene_id=scene_id, location=scene.location or "unknown", transcript=transcript)}],
        ARCHIVE_SCHEMA, max_tokens=4000)

    async with lock:
        gazetteer = campaign.gazetteer()
        title = (data.get("title") or scene.title or f"Scene {scene_id}").strip()
        note_path = f"scenes/{scene_id:03d}-{slugify(title)[:50]}.md"
        link = note_path[:-3]
        label = f"Scene {scene_id}"
        loc_entry = None
        if data.get("location"):
            loc_entry = wiki.upsert_entity(campaign, gazetteer, "location", data["location"], link, label)
        people = [wiki.upsert_entity(campaign, gazetteer, "npc", n, link, label)
                  for n in data.get("npcs") or [] if n.get("name")]
        others = [wiki.upsert_entity(campaign, gazetteer, o.get("kind"), o, link, label)
                  for o in data.get("others") or [] if o.get("name")]
        campaign.save_gazetteer(gazetteer)

        lines = [f"# Scene {scene_id}: {title}", "", data.get("summary", "").strip(), ""]
        if loc_entry:
            lines.append(f"**Location:** {_link(loc_entry)}")
        if people:
            lines.append(f"**People:** {', '.join(_link(e) for e in people)}")
        if others:
            lines.append(f"**Also:** {', '.join(_link(e) for e in others)}")
        fm = (f"---\nscene: {scene_id}\ntitle: {title!r}\n"
              f"messages: [{msgs[0]['id'] if msgs else scene.start}, "
              f"{msgs[-1]['id'] if msgs else scene.start}]\n"
              f"filed: {time.strftime('%Y-%m-%d')}\n---\n")
        campaign.write(note_path, fm + "\n".join(lines) + "\n")
        where = f" at {_link(loc_entry)}" if loc_entry else ""
        campaign.write("timeline.md", campaign.read("timeline.md").rstrip() +
                       f"\n- [[{link}|Scene {scene_id}: {title}]]{where}: "
                       f"{data.get('timeline', '').strip()}\n")

        fresh = campaign.load_state()
        s = next(s for s in fresh.scenes if s.id == scene_id)
        s.status, s.title, s.note = "compacted", title, note_path
        if loc_entry:
            s.location = loc_entry["name"]
        campaign.save_state(fresh)
    return {"scene": scene_id, "title": title, "note": note_path,
            "entities": [e["name"] for e in ([loc_entry] if loc_entry else []) + people + others]}


async def rewrite_brief(campaign: Campaign, archiver: LLMClient, filed: list[dict]) -> None:
    name = campaign.meta.get("name", campaign.slug)
    scenes = "\n\n".join(
        wiki.split_note(campaign.read(f["note"]))[1].strip() for f in filed)
    msg = await archiver.chat(
        [{"role": "system", "content": prompts.ARCHIVE_SYSTEM},
         {"role": "user", "content": prompts.BRIEF_TASK.format(
             brief=campaign.brief, scenes=scenes, campaign=name)}],
        max_tokens=2000, temperature=0.3, extra_body=NO_THINKING)
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
    text = re.sub(r"^```(?:markdown)?\n|\n```$", "", text)
    if text:
        campaign.write("brief.md", text + "\n")


async def compact(campaign: Campaign, router_llm: LLMClient, archiver: LLMClient,
                  lock: asyncio.Lock) -> dict:
    report: dict = {"started": time.time()}
    async with lock:
        report["audit"] = await router.audit(campaign, router_llm)
    state = campaign.load_state()
    todo = [s.id for s in state.scenes[:-1] if s.status == "closed_provisional"]
    filed = []
    for sid in todo:
        log.info("filing %s scene %s", campaign.slug, sid)
        filed.append(await file_scene(campaign, archiver, campaign.load_state(), sid, lock))
    if filed:
        await rewrite_brief(campaign, archiver, filed)
    async with lock:
        state = campaign.load_state()
        if state.fold and state.fold["until"] < state.live_start():
            state.fold = None  # everything it condensed is filed now
            campaign.save_state(state)
    report["filed"] = filed
    report["seconds"] = round(time.time() - report.pop("started"), 1)
    return report


async def fold_current_scene(campaign: Campaign, archiver: LLMClient, keep_tokens: int,
                             lock: asyncio.Lock) -> None:
    """The current scene alone is over budget: summarise its older part into state.fold,
    keeping the most recent `keep_tokens` of it live."""
    from rpg_llm.vault import estimate_tokens

    state = campaign.load_state()
    live = [m for m in campaign.messages() if m["id"] >= state.live_start()]
    kept, total = [], 0
    for m in reversed(live):
        total += estimate_tokens(m["content"])
        if total > keep_tokens and len(kept) >= 2:
            break
        kept.append(m)
    cut = [m for m in live if m["id"] < kept[-1]["id"]]
    prev_fold = state.fold["summary"] if state.fold else ""
    cut = [m for m in cut if not state.fold or m["id"] > state.fold["until"]]
    if not cut:
        return
    msg = await archiver.chat(
        [{"role": "system", "content": prompts.ARCHIVE_SYSTEM},
         {"role": "user", "content": prompts.FOLD_TASK.format(
             previous=f"\nSummary so far of this scene:\n{prev_fold}\n" if prev_fold else "",
             transcript=wiki.format_transcript(cut, limit_chars=await transcript_limit(archiver)))}],
        max_tokens=1500, temperature=0.3, extra_body=NO_THINKING)
    async with lock:
        fresh = campaign.load_state()
        fresh.fold = {"until": cut[-1]["id"], "summary": (msg.get("content") or "").strip()}
        campaign.save_state(fresh)


def reset_wiki(campaign: Campaign) -> None:
    """Throw away everything derived from the transcript (notes, gazetteer, timeline, brief) and
    mark filed scenes as closed again, so the next compaction rebuilds the wiki from scratch.
    Scene boundaries are kept."""
    import shutil

    for d in ("scenes", "locations", "npcs", "things"):
        shutil.rmtree(campaign.path(d), ignore_errors=True)
    meta = campaign.meta
    campaign.save_gazetteer([])
    campaign.write("timeline.md", f"# Timeline: {meta.get('name', campaign.slug)}\n\n")
    campaign.write("brief.md", f"# {meta.get('name', campaign.slug)}\n\n## Premise\n\n"
                               f"{(meta.get('premise') or '').strip() or '(not set)'}\n")
    state = campaign.load_state()
    for s in state.scenes:
        if s.status == "compacted":
            s.status, s.note = "closed_provisional", None
    campaign.save_state(state)
