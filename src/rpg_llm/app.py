"""HTTP API + static UI."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rpg_llm import admin, compactor, context, router, wiki
from rpg_llm.config import ROLES, NotConfigured, Settings
from rpg_llm.llm import NO_THINKING, LLMClient
from rpg_llm.vault import Campaign, Vault

log = logging.getLogger("rpg_llm")
STATIC = Path(__file__).parent / "static"
MAX_TOOL_ROUNDS = 5


class Runtime:
    """Process-wide state: settings, model clients, per-campaign locks and job status."""

    def __init__(self, settings: Settings, dm=None, router_llm=None, archiver=None):
        self.settings = settings
        self.vault = Vault(settings.vault_path)
        self._fixed = {"dm": dm, "router": router_llm, "archiver": archiver}  # tests inject fakes
        self.locks: dict[str, asyncio.Lock] = {}
        self.status: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}  # admin jobs: imports, wiki rebuilds
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
        st = self.st(c.slug)
        st["tracking"] = True
        try:
            async with self.lock(c.slug):
                v = await router.track(c, self.router, self.settings.tuning.router_threshold)
            if v is not None:
                st["last_verdict"] = {**v, "at": time.time()}
        except Exception as e:
            log.exception("scene tracking failed")
            st["error"] = f"scene tracking: {e}"
        finally:
            st["tracking"] = False

    def needs_compaction(self, c: Campaign) -> bool:
        state = c.load_state()
        return any(s.status == "closed_provisional" for s in state.scenes[:-1])

    async def run_compact(self, c: Campaign) -> dict | None:
        st = self.st(c.slug)
        if st["compacting"]:
            return None
        st["compacting"] = True
        try:
            report = await compactor.compact(c, self.router, self.archiver, self.lock(c.slug))
            st["last_compaction"] = {**report, "at": time.time()}
            return report
        except Exception as e:
            log.exception("compaction failed")
            st["error"] = f"compaction: {e}"
            raise
        finally:
            st["compacting"] = False

    def maybe_compact_idle(self, c: Campaign) -> None:
        last = c.last_activity()
        hours = self.settings.tuning.idle_compact_hours
        idle = last is not None and time.time() - last > hours * 3600
        if idle and not self.st(c.slug)["compacting"] and self.needs_compaction(c):
            self.spawn(self.run_compact(c))

    async def idle_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
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
    messages = c.messages()
    user = messages[-1]
    last_reply = next((m["content"] for m in reversed(messages[:-1]) if m["role"] == "assistant"), "")

    yield sse({"type": "status", "text": "checking the archive…"})
    gate_router = rt.router if rt.settings.tuning.gatekeeper_enabled else None
    t0 = time.time()
    notes, info = await router.gatekeep(c, gate_router, user["content"], last_reply,
                                        rt.settings.tuning.gatekeeper_timeout)
    info["seconds"] = round(time.time() - t0, 1)
    info["notes"] = notes
    st["last_context"] = info
    yield sse({"type": "context", **info})

    state = c.load_state()
    window = await rt.dm.context_window() or 32768
    budget = int(window * rt.settings.tuning.live_tail_pct / 100)
    if context.tail_tokens(c, state, messages) > budget:
        yield sse({"type": "status", "text": "condensing the start of this scene…"})
        await compactor.fold_current_scene(c, rt.archiver, budget // 2, rt.lock(c.slug))
        state = c.load_state()

    convo = context.build(c, state, messages, notes)
    kwargs = {"tools": wiki.TOOLS} if c.gazetteer() else {}  # nothing to look up yet
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
                round_content += d["content"]
                yield sse({"type": "content", "text": d["content"]})
            elif "tool_calls" in d:
                calls = d["tool_calls"]
            elif "usage" in d:
                stats["rounds"] += 1
                for k in ("prompt_tokens", "cached_tokens", "prompt_ms", "completion_tokens"):
                    stats[k] += d["usage"].get(k) or 0
        content += round_content
        if not calls:
            break
        convo.append({"role": "assistant", "content": round_content, "tool_calls": [
            {"id": tc["id"] or f"call_{i}", "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for i, tc in enumerate(calls)]})
        for i, tc in enumerate(calls):
            result = wiki.run_tool(c, tc["name"], tc["arguments"])
            trace.append({"tool": tc["name"], "arguments": tc["arguments"], "result": result[:2000]})
            yield sse({"type": "tool", "name": tc["name"], "arguments": tc["arguments"]})
            convo.append({"role": "tool", "tool_call_id": tc["id"] or f"call_{i}", "content": result})

    stats["seconds"] = round(time.time() - t_dm, 1)
    log.info("DM turn %s: %s", c.slug, stats)
    entry = {"role": "assistant", "content": content.strip(), "reasoning": reasoning.strip(),
             "stats": stats}
    if trace:
        entry["tools"] = trace
    if supersedes:
        entry["supersedes"] = supersedes
    if info.get("injected"):
        entry["injected"] = info["injected"]
    entry = c.append(entry)
    yield sse({"type": "done", "message": entry})
    rt.spawn(rt.run_track(c))


def stream_turn(rt: Runtime, c: Campaign, supersedes: list[int] | None = None) -> StreamingResponse:
    async def gen():
        try:
            async for chunk in play_turn(rt, c, supersedes or []):
                yield chunk
        except Exception as e:
            log.exception("turn failed")
            yield sse({"type": "error", "text": str(e)})
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- app --------------------------------------------------------------------

class NewCampaign(BaseModel):
    name: str
    system: str = ""
    premise: str = ""
    dm_instructions: str = ""


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
                "warnings": rt.settings.app.warnings()}

    @app.get("/api/campaigns")
    async def list_campaigns():
        return [{"slug": c.slug, "name": c.meta.get("name"), "system": c.meta.get("system"),
                 "last_activity": c.last_activity()} for c in R().vault.campaigns()]

    @app.post("/api/campaigns")
    async def create_campaign(body: NewCampaign):
        c = R().vault.create(body.name, body.premise, body.system, body.dm_instructions)
        return {"slug": c.slug}

    @app.get("/api/campaigns/{slug}")
    async def get_campaign(slug: str):
        rt = R()
        c = rt.campaign(slug)
        if rt.configured:
            rt.maybe_compact_idle(c)  # resuming after a long gap files the old scenes
        state = c.load_state()
        return {"slug": c.slug, "meta": c.meta, "messages": c.messages(),
                "live_start": state.live_start(), "scenes": [vars(s) for s in state.scenes],
                "status": rt.st(c.slug)}

    @app.get("/api/campaigns/{slug}/status")
    async def get_status(slug: str):
        rt = R()
        c = rt.campaign(slug)
        state = c.load_state()
        return {**rt.st(slug), "scenes": [vars(s) for s in state.scenes],
                "live_start": state.live_start(), "gazetteer_size": len(c.gazetteer())}

    @app.get("/api/campaigns/{slug}/file")
    async def get_file(slug: str, path: str):
        c = R().campaign(slug)
        if not path.endswith((".md", ".yaml")):
            raise HTTPException(400, "markdown or yaml only")
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
        dead = [msgs[-1]["id"]] if msgs[-1]["role"] == "assistant" else []
        user = msgs[-2] if dead else msgs[-1]
        router.undo_scenes_from(c, user["id"])
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
        dead = [m["id"] for m in msgs if m["id"] >= last_user["id"]]
        router.undo_scenes_from(c, last_user["id"])
        c.append({"role": "user", "content": body.content.strip(), "supersedes": dead})
        return stream_turn(rt, c)

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
