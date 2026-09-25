"""HTTP API + static UI."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rpg_llm import (admin, arc, compactor, context, dice, images, portrait, router, sheet,
                     suggest, themes, wiki)
from rpg_llm.config import ROLES, NotConfigured, Settings
from rpg_llm.llm import NO_THINKING, LLMClient
from rpg_llm.vault import Campaign, Vault

log = logging.getLogger("rpg_llm")
STATIC = Path(__file__).parent / "static"
MAX_TOOL_ROUNDS = 5
QUIET_SECONDS = 180  # background filing waits until the player has been quiet this long
QUIET_POLL = 5
# Condensing the live tail rewrites the start of the prompt, so the next turn re-reads it once.
# Do it in the background after a reply once the tail passes FOLD_EARLY of the budget, and cut
# it down to FOLD_KEEP of the budget so it happens rarely.
FOLD_EARLY = 0.8
FOLD_KEEP = 0.35


class Runtime:
    """Process-wide state: settings, model clients, per-campaign locks and job status."""

    def __init__(self, settings: Settings, dm=None, router_llm=None, archiver=None):
        self.settings = settings
        self.vault = Vault(settings.vault_path)
        self._fixed = {"dm": dm, "router": router_llm, "archiver": archiver}  # tests inject fakes
        self.locks: dict[str, asyncio.Lock] = {}
        self.status: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}  # admin jobs: imports, wiki rebuilds
        self.seen: dict[str, float] = {}  # last time the player's browser touched a campaign
        self.turns: dict[str, int] = {}  # turns in progress per campaign
        self.tasks: set[asyncio.Task] = set()
        self.reload()

    def reload(self) -> None:
        """(Re)create the model clients from the current config, e.g. after an admin save.
        A role that isn't set up yet gets None."""
        for role in ROLES:
            client = self._fixed[role]
            if client is None:
                try:
                    client = LLMClient(self.settings.app.slot(role))
                except NotConfigured:
                    client = None
            setattr(self, role, client)

    @property
    def configured(self) -> bool:
        return all(getattr(self, role) is not None for role in ROLES)

    def require_configured(self) -> None:
        if not self.configured:
            raise HTTPException(503, "Not set up yet: open /admin to add your model servers.")

    def lock(self, slug: str) -> asyncio.Lock:
        return self.locks.setdefault(slug, asyncio.Lock())

    def st(self, slug: str) -> dict:
        return self.status.setdefault(slug, {"tracking": False, "compacting": False,
                                             "condensing": False,
                                             "last_sheet": None,
                                             "last_verdict": None, "last_compaction": None,
                                             "last_context": None, "error": None})

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def campaign(self, slug: str) -> Campaign:
        try:
            return self.vault.get(slug)
        except KeyError:
            raise HTTPException(404, f"no campaign {slug!r}")

    # ---- background jobs ----------------------------------------------------

    async def run_track(self, c: Campaign) -> None:
        """After each reply, in the background: scene tracking, the character sheet, and
        condensing a long live tail. Each step runs even if an earlier one fails."""
        st = self.st(c.slug)
        st["tracking"] = True
        try:
            try:
                async with self.lock(c.slug):
                    v = await router.track(c, self.router, self.settings.tuning.router_threshold)
                if v is not None:
                    st["last_verdict"] = {**v, "at": time.time()}
            except Exception as e:
                log.exception("scene tracking failed")
                st["error"] = f"scene tracking: {e}"
            try:
                async with self.lock(c.slug):
                    changed = await sheet.update(c, self.router,
                                                 router.router_system(c, c.load_state()))
                if changed:
                    st["last_sheet"] = {**changed, "at": time.time()}
            except Exception as e:
                log.exception("sheet update failed")
                st["error"] = f"character sheet: {e}"
            try:
                budget = await self.tail_budget()
                if context.tail_tokens(c, c.load_state(), c.messages()) > FOLD_EARLY * budget:
                    st["condensing"] = True
                    await compactor.fold_current_scene(c, self.archiver,
                                                       int(budget * FOLD_KEEP), self.lock(c.slug))
            except Exception as e:
                log.exception("condensing failed")
                st["error"] = f"condensing: {e}"
            finally:
                st["condensing"] = False
        finally:
            st["tracking"] = False

    async def tail_budget(self) -> int:
        window = await self.dm.context_window() or 32768
        return int(window * self.settings.tuning.live_tail_pct / 100)

    def touch(self, slug: str) -> None:
        self.seen[slug] = time.time()

    def player_active(self, slug: str) -> bool:
        return self.turns.get(slug, 0) > 0 or time.time() - self.seen.get(slug, 0) < QUIET_SECONDS

    async def wait_until_quiet(self, slug: str) -> None:
        """Background filing never competes with play: hold off while a turn is running or
        the player has been active in the last few minutes."""
        while self.player_active(slug):
            await asyncio.sleep(QUIET_POLL)

    async def run_quietly(self, coro, what: str):
        """Background setup work (starting sheet, story arc): log failures, never raise."""
        try:
            return await coro
        except Exception:
            log.exception("%s failed", what)

    def needs_compaction(self, c: Campaign) -> bool:
        state = c.load_state()
        return any(s.status == "closed_provisional" for s in state.scenes[:-1])

    async def run_compact(self, c: Campaign, background: bool = False) -> dict | None:
        """background=True (idle filing) pauses whenever the player is active; a filing the
        player asked for runs straight through."""
        st = self.st(c.slug)
        if st["compacting"]:
            return None
        st["compacting"] = True
        try:
            pause = (lambda: self.wait_until_quiet(c.slug)) if background else None
            report = await compactor.compact(c, self.router, self.archiver, self.lock(c.slug),
                                             pause)
            st["last_compaction"] = {**report, "at": time.time()}
            return report
        except Exception as e:
            log.exception("compaction failed")
            st["error"] = f"compaction: {e}"
            raise
        finally:
            st["compacting"] = False

    def maybe_compact_idle(self, c: Campaign) -> None:
        """Start background filing once play has stopped for the idle time and nobody is at
        the campaign right now (opening it after a long break doesn't trigger it at once)."""
        last = c.last_activity()
        hours = self.settings.tuning.idle_compact_hours
        idle = last is not None and time.time() - last > hours * 3600
        if (idle and not self.player_active(c.slug) and not self.st(c.slug)["compacting"]
                and self.needs_compaction(c)):
            self.spawn(self.run_compact(c, background=True))

    async def idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if not self.configured:
                continue
            for c in self.vault.campaigns():
                try:
                    self.maybe_compact_idle(c)
                except Exception:
                    log.exception("idle check failed for %s", c.slug)


# ---- the turn ---------------------------------------------------------------

def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def play_turn(rt: Runtime, c: Campaign, supersedes: list[int]):
    """Assumes the player's message is already the last active user message. Streams SSE."""
    st = rt.st(c.slug)
    st["error"] = None
    t_turn = time.time()
    messages = c.messages()
    user = messages[-1]
    last_reply = next((m["content"] for m in reversed(messages[:-1]) if m["role"] == "assistant"), "")

    yield sse({"type": "status", "text": "checking the archive…"})
    gate_router = rt.router if rt.settings.tuning.gatekeeper_enabled else None
    t0 = time.time()
    notes, info = await router.gatekeep(c, gate_router, user["content"], last_reply,
                                        rt.settings.tuning.gatekeeper_timeout)
    info["seconds"] = round(time.time() - t0, 1)
    notes = "\n\n".join(p for p in (sheet.notes_block(sheet.load(c)), notes) if p) or None
    info["notes"] = notes
    st["last_context"] = info
    yield sse({"type": "context", **info})

    state = c.load_state()
    budget = await rt.tail_budget()
    if context.tail_tokens(c, state, messages) > budget:  # fallback: normally done after a reply
        yield sse({"type": "status", "text": "condensing the start of this scene…"})
        await compactor.fold_current_scene(c, rt.archiver, int(budget * FOLD_KEEP),
                                           rt.lock(c.slug))
        state = c.load_state()

    convo = context.build(c, state, messages, notes)
    tools = list(wiki.TOOLS) if c.gazetteer() else []  # nothing to look up yet
    if context.table(c.meta)["dice"] == "auto":
        tools.append(dice.TOOL)
    kwargs = {"tools": tools} if tools else {}
    rolls = []
    if not rt.settings.app.dm.thinking:
        kwargs["extra_body"] = NO_THINKING
    content, reasoning, trace = "", "", []
    stats = {"prompt_tokens": 0, "cached_tokens": 0, "prompt_ms": 0, "completion_tokens": 0,
             "rounds": 0}
    t_dm = time.time()
    yield sse({"type": "status", "text": ""})
    for n in range(MAX_TOOL_ROUNDS + 1):
        if n == MAX_TOOL_ROUNDS:  # still looking things up: make it answer with what it has
            kwargs.pop("tools", None)
        calls = None
        round_content = ""
        async for d in rt.dm.stream(convo, **kwargs):
            if "reasoning" in d:
                reasoning += d["reasoning"]
                yield sse({"type": "reasoning", "text": d["reasoning"]})
            elif "content" in d:
                if "first_word" not in stats:
                    stats["first_word"] = round(time.time() - t_turn, 1)
                round_content += d["content"]
                yield sse({"type": "content", "text": d["content"]})
            elif "tool_calls" in d:
                calls = d["tool_calls"]
            elif "usage" in d:
                # Context size and cache reuse come from the first call of the turn; later
                # calls (after a dice roll or lookup) resend the same prompt plus the tool
                # result, so adding them up would double-count. Time and output do add up.
                u = d["usage"]
                if stats["rounds"] == 0:
                    stats["prompt_tokens"] = u.get("prompt_tokens") or 0
                    stats["cached_tokens"] = u.get("cached_tokens") or 0
                stats["rounds"] += 1
                stats["prompt_ms"] += u.get("prompt_ms") or 0
                stats["completion_tokens"] += u.get("completion_tokens") or 0
        content += round_content
        if not calls:
            break
        convo.append({"role": "assistant", "content": round_content, "tool_calls": [
            {"id": tc["id"] or f"call_{i}", "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for i, tc in enumerate(calls)]})
        for i, tc in enumerate(calls):
            if tc["name"] == "roll_dice":
                try:
                    args = json.loads(tc["arguments"] or "{}")
                    r = dice.roll(str(args.get("dice", "")), str(args.get("reason", "")),
                                  args.get("target"), args.get("success_if") or "at_least")
                    rolls.append(r)
                    result = dice.describe(r)
                    yield sse({"type": "roll", **r})
                except (ValueError, TypeError, json.JSONDecodeError) as e:
                    result = f"Error: {e}"
            else:
                result = wiki.run_tool(c, tc["name"], tc["arguments"])
                yield sse({"type": "tool", "name": tc["name"], "arguments": tc["arguments"]})
            trace.append({"tool": tc["name"], "arguments": tc["arguments"], "result": result[:2000]})
            convo.append({"role": "tool", "tool_call_id": tc["id"] or f"call_{i}", "content": result})

    stats["seconds"] = round(time.time() - t_dm, 1)
    stats["total"] = round(time.time() - t_turn, 1)
    stats["archive_check"] = info["seconds"]
    log.info("DM turn %s: %s", c.slug, stats)
    if user["id"] not in {m["id"] for m in c.messages()}:
        return  # the player unsent their message while the GM was replying
    entry = {"role": "assistant", "content": content.strip(), "reasoning": reasoning.strip(),
             "stats": stats}
    if trace:
        entry["tools"] = trace
    if rolls:
        entry["rolls"] = rolls
    if supersedes:
        entry["supersedes"] = supersedes
    if info.get("injected"):
        entry["injected"] = info["injected"]
    entry = c.append(entry)
    yield sse({"type": "done", "message": entry})
    rt.spawn(rt.run_track(c))


def stream_turn(rt: Runtime, c: Campaign, supersedes: list[int] | None = None) -> StreamingResponse:
    async def gen():
        rt.turns[c.slug] = rt.turns.get(c.slug, 0) + 1
        try:
            async for chunk in play_turn(rt, c, supersedes or []):
                yield chunk
        except Exception as e:
            log.exception("turn failed")
            yield sse({"type": "error", "text": str(e)})
        finally:
            rt.turns[c.slug] -= 1
            rt.touch(c.slug)

    rt.touch(c.slug)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- app --------------------------------------------------------------------

class NewCampaign(BaseModel):
    name: str
    system: str = ""
    premise: str = ""
    dm_instructions: str = ""
    allow_rewind: bool = False
    consequences: str = "normal"
    dice: str = "auto"
    theme: str = "auto"  # "auto" picks from the system and genre
    genre: str = ""
    portrait: str = ""  # token of a portrait made during setup
    appearance: str = ""


class PortraitAsk(BaseModel):
    system: str = ""
    premise: str = ""
    look: str = "auto"
    genre: str = ""
    appearance: str = ""
    avoid: list[str] = []


class NewPortrait(BaseModel):
    appearance: str = ""


class ChoosePortrait(BaseModel):
    name: str


class PremiseAsk(BaseModel):
    system: str = ""
    seed: str = ""
    avoid: list[str] = []


def can_rewind(c: Campaign) -> bool:
    """Rewinding = changing or re-rolling a turn after seeing the GM's reply. Off by default so
    outcomes stick; a campaign can allow it."""
    return bool(c.meta.get("allow_rewind"))


def replied(msgs: list[dict]) -> bool:
    """True if the last exchange has a real GM reply (not missing, not empty)."""
    return bool(msgs) and msgs[-1]["role"] == "assistant" and bool(msgs[-1]["content"].strip())


NO_REWIND = "Rewinds are off for this campaign: the GM's reply stands. (Allow them in the campaign's settings.)"


class Say(BaseModel):
    content: str


def create_app(rt: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rt = app.state.rt if hasattr(app.state, "rt") else Runtime(Settings.load())
        idle = asyncio.create_task(app.state.rt.idle_loop())
        yield
        idle.cancel()

    app = FastAPI(title="rpg-llm", lifespan=lifespan)
    if rt is not None:
        app.state.rt = rt

    def R() -> Runtime:
        return app.state.rt

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin")
    def admin_page():
        return FileResponse(STATIC / "admin.html")

    @app.get("/api/status")
    async def status():
        rt = R()
        return {"configured": rt.configured, "missing": rt.settings.app.missing(),
                "warnings": rt.settings.app.warnings(), "images": rt.settings.app.images.enabled}

    @app.get("/api/campaigns")
    async def list_campaigns():
        return [{"slug": c.slug, "name": c.meta.get("name"), "system": c.meta.get("system"),
                 "last_activity": c.last_activity()} for c in R().vault.campaigns()]

    @app.post("/api/campaigns")
    async def create_campaign(body: NewCampaign):
        c = R().vault.create(body.name, body.premise, body.system, body.dm_instructions)
        t = context.table({"consequences": body.consequences, "dice": body.dice})
        theme = body.theme if body.theme in themes.THEMES or body.theme == themes.PLAIN \
            else themes.default_for(body.system, body.genre)
        c.save_meta({**c.meta, "allow_rewind": body.allow_rewind, **t, "theme": theme,
                     **({"appearance": body.appearance.strip()} if body.appearance.strip() else {})})
        cand = portrait.candidate_path(R().vault.root, body.portrait)
        if cand:
            portrait.add(c, cand.read_bytes())
        rt = R()
        if rt.configured:
            async def setup():
                if body.premise.strip():
                    await rt.run_quietly(sheet.create_start(c, rt.archiver), "starting sheet")
                await rt.run_quietly(arc.generate(c, rt.archiver), "story arc")
            rt.spawn(setup())
        return {"slug": c.slug}

    @app.delete("/api/campaigns/{slug}")
    async def delete_campaign(slug: str):
        rt = R()
        c = rt.campaign(slug)
        if rt.st(slug)["compacting"] or rt.turns.get(slug):
            raise HTTPException(409, "the campaign is busy; try again in a moment")
        return {"ok": True, "moved_to": str(rt.vault.trash(c))}

    @app.get("/api/themes")
    async def list_themes():
        return {**{k: v["label"] for k, v in themes.public().items()}, "plain": "Plain"}

    def resolve_look(look: str, system: str, genre: str) -> str:
        return look if look in themes.THEMES or look == themes.PLAIN else themes.default_for(system, genre)

    @app.post("/api/suggest/portrait")
    async def suggest_portrait(body: PortraitAsk):
        rt = R()
        rt.require_configured()
        cfg = rt.settings.app.images
        if not cfg.enabled:
            raise HTTPException(409, "No image generator is set up (admin → Image generation).")
        d = await portrait.describe(rt.dm, body.system, body.premise, body.appearance, body.avoid)
        try:
            webp = await portrait.paint(cfg, resolve_look(body.look, body.system, body.genre), d["prompt"])
        except images.ImageError as e:
            raise HTTPException(502, str(e))
        token = portrait.save_candidate(rt.vault.root, webp, d)
        return {"token": token, "url": f"/api/portraits/tmp/{token}", **d}

    @app.get("/api/portraits/tmp/{token}")
    async def portrait_candidate(token: str):
        p = portrait.candidate_path(R().vault.root, token)
        if not p:
            raise HTTPException(404, "no such portrait")
        return FileResponse(p, media_type="image/webp")

    @app.get("/api/campaigns/{slug}/portraits")
    async def list_portraits(slug: str):
        c = R().campaign(slug)
        return {"current": portrait.current(c), "all": portrait.history(c),
                "appearance": (sheet.load(c) or {}).get("appearance") or c.meta.get("appearance", ""),
                "enabled": R().settings.app.images.enabled}

    @app.get("/api/campaigns/{slug}/portraits/{name}")
    async def portrait_file(slug: str, name: str):
        c = R().campaign(slug)
        if name not in portrait.history(c):
            raise HTTPException(404, "no such portrait")
        return FileResponse(c.path(f"art/portraits/{name}"), media_type="image/webp",
                            headers={"Cache-Control": "max-age=31536000, immutable"})

    @app.post("/api/campaigns/{slug}/portraits")
    async def new_portrait(slug: str, body: NewPortrait):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        cfg = rt.settings.app.images
        if not cfg.enabled:
            raise HTTPException(409, "No image generator is set up (admin → Image generation).")
        sh = sheet.load(c) or {}
        appearance = body.appearance.strip() or sh.get("appearance") or c.meta.get("appearance", "")
        premise = (f"{sh.get('name', '')}: {sh.get('concept', '')}\n" if sh else "") + \
            (c.meta.get("premise") or c.brief)
        avoid = [c.meta["portrait_prompt"]] if c.meta.get("portrait_prompt") else []
        d = await portrait.describe(rt.dm, c.meta.get("system") or "", premise, appearance, avoid)
        try:
            webp = await portrait.paint(cfg, c.meta.get("theme") or "plain", d["prompt"])
        except images.ImageError as e:
            raise HTTPException(502, str(e))
        name = portrait.add(c, webp, d["prompt"])
        if body.appearance.strip() and sh:  # the player's new description goes on the sheet
            async with rt.lock(slug):
                msgs = c.messages()
                sheet.save(c, {**sh, "appearance": body.appearance.strip()},
                           msgs[-1]["id"] if msgs else 0, "appearance edited")
        return {"current": name, "appearance": d["appearance"]}

    @app.put("/api/campaigns/{slug}/portrait")
    async def choose_portrait(slug: str, body: ChoosePortrait):
        c = R().campaign(slug)
        try:
            portrait.choose(c, body.name)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"current": body.name}

    @app.get("/api/suggest/systems")
    async def suggest_systems(refresh: bool = False):
        rt = R()
        rt.require_configured()
        return await suggest.systems(rt.dm, rt.vault.root, refresh)

    @app.post("/api/suggest/premise")
    async def suggest_premise(body: PremiseAsk):
        rt = R()
        rt.require_configured()
        return {"premise": await suggest.premise(rt.dm, body.system, body.seed, body.avoid)}

    @app.get("/api/campaigns/{slug}")
    async def get_campaign(slug: str):
        rt = R()
        c = rt.campaign(slug)
        rt.touch(slug)
        state = c.load_state()
        return {"slug": c.slug, "meta": c.meta, "can_rewind": can_rewind(c),
                "table": context.table(c.meta),
                "portrait": portrait.current(c),
                "images": rt.settings.app.images.enabled,
                "theme": c.meta.get("theme") or themes.default_for(c.meta.get("system") or ""),
                "themes": themes.public(),
                "messages": c.messages(),
                "live_start": state.live_start(), "scenes": [vars(s) for s in state.scenes],
                "status": rt.st(c.slug)}

    @app.get("/api/campaigns/{slug}/status")
    async def get_status(slug: str):
        rt = R()
        rt.touch(slug)
        c = rt.campaign(slug)
        state = c.load_state()
        return {**rt.st(slug), "scenes": [vars(s) for s in state.scenes],
                "live_start": state.live_start(), "gazetteer_size": len(c.gazetteer())}

    @app.get("/api/campaigns/{slug}/file")
    async def get_file(slug: str, path: str):
        c = R().campaign(slug)
        if not path.endswith((".md", ".yaml")):
            raise HTTPException(400, "markdown or yaml only")
        if arc.is_private(path):
            raise HTTPException(403, "that's for the GM's eyes only")
        try:
            return {"path": path, "text": c.read(path)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/campaigns/{slug}/chat")
    async def chat(slug: str, body: Say):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        if not body.content.strip():
            raise HTTPException(400, "empty message")
        c.append({"role": "user", "content": body.content.strip()})
        return stream_turn(rt, c)

    @app.post("/api/campaigns/{slug}/regenerate")
    async def regenerate(slug: str):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        msgs = c.messages()
        if not msgs:
            raise HTTPException(400, "nothing to regenerate")
        if replied(msgs) and not can_rewind(c):
            raise HTTPException(403, NO_REWIND)
        dead = [msgs[-1]["id"]] if msgs[-1]["role"] == "assistant" else []
        user = msgs[-2] if dead else msgs[-1]
        router.undo_scenes_from(c, user["id"])
        sheet.rewind(c, user["id"])
        if dead:
            # hide the old reply immediately; the new one also records what it replaced
            c.append({"role": "system", "content": "", "supersedes": dead})
        return stream_turn(rt, c, dead)

    @app.post("/api/campaigns/{slug}/edit")
    async def edit_last(slug: str, body: Say):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        msgs = c.messages()
        last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if last_user is None:
            raise HTTPException(400, "no player message to edit")
        if replied(msgs) and not can_rewind(c):
            raise HTTPException(403, NO_REWIND)
        dead = [m["id"] for m in msgs if m["id"] >= last_user["id"]]
        router.undo_scenes_from(c, last_user["id"])
        sheet.rewind(c, last_user["id"])
        c.append({"role": "user", "content": body.content.strip(), "supersedes": dead})
        return stream_turn(rt, c)

    @app.get("/api/campaigns/{slug}/character")
    async def get_character(slug: str):
        c = R().campaign(slug)
        return {"sheet": sheet.load(c), "fields": list(sheet.FIELDS), "lists": list(sheet.LISTS),
                "history": [{k: h[k] for k in ("after", "ts", "what")}
                            for h in sheet.history(c)[-10:]][::-1]}

    @app.put("/api/campaigns/{slug}/character")
    async def put_character(slug: str, body: dict):
        rt = R()
        c = rt.campaign(slug)
        msgs = c.messages()
        async with rt.lock(slug):
            saved = sheet.save(c, body, msgs[-1]["id"] if msgs else 0, "edited by the player")
        return {"sheet": saved}

    @app.post("/api/campaigns/{slug}/unsend")
    async def unsend(slug: str):
        """Withdraw the player's last message (and any reply to it), e.g. sent by mistake.
        Nothing is deleted: the transcript records it as superseded. Returns the text so the
        player can finish it."""
        rt = R()
        c = rt.campaign(slug)
        msgs = c.messages()
        last_user = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if last_user is None:
            raise HTTPException(400, "nothing to unsend")
        if replied(msgs) and not can_rewind(c):
            raise HTTPException(409, "Too late: the GM had already replied.")
        dead = [m["id"] for m in msgs if m["id"] >= last_user["id"]]
        router.undo_scenes_from(c, last_user["id"])
        sheet.rewind(c, last_user["id"])
        c.append({"role": "system", "content": "", "supersedes": dead, "unsent": True})
        return {"content": last_user["content"]}

    @app.post("/api/campaigns/{slug}/compact")
    async def compact_now(slug: str):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        report = await rt.run_compact(c)
        if report is None:
            raise HTTPException(409, "compaction already running")
        return report

    admin.register(app, R)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings.load()
    if not s.app.configured:
        log.warning("Not set up yet: open http://<this host>:%s/admin to configure models.",
                    s.env.port)
    uvicorn.run(create_app(Runtime(s)), host=s.env.host, port=s.env.port)
