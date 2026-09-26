"""Admin API: first-run setup, model servers and roles, tuning, campaign management, imports.

Everything here is behind the optional admin password (a signed cookie). With no password set,
admin is open, which is what a first run needs.
"""

import re
import secrets
import time

import httpx2
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from rpg_llm import arc, compactor, context, images, router, suggest, themes
from rpg_llm.config import ROLES, AppConfig, ImageGen, NotConfigured, Role, Server, Tuning
from rpg_llm.importers import openwebui
from rpg_llm.llm import LLMClient

COOKIE = "rpg_admin"
MASK = "••••••"


def _mask(key: str) -> str:
    return f"{MASK}{key[-4:]}" if key else ""


class ConfigIn(BaseModel):
    servers: list[Server]
    dm: Role
    router: Role
    archiver: Role
    tuning: Tuning
    images: ImageGen = ImageGen()
    new_password: str | None = None
    clear_password: bool = False


class Login(BaseModel):
    password: str


class Probe(BaseModel):
    base_url: str
    api_key: str = ""
    server_id: str | None = None


class CampaignEdit(BaseModel):
    name: str
    system: str = ""
    premise: str = ""
    dm_instructions: str = ""
    tone: str = ""
    allow_rewind: bool = False
    consequences: str = "normal"
    dice: str = "none"
    style: str = "plain"
    length: str = "medium"
    theme: str = "plain"


class ImportIn(BaseModel):
    token: str
    chat: int = 0
    name: str = ""
    system: str = ""
    premise: str = ""
    file_scenes: bool = True


def _merge(body: ConfigIn, old: AppConfig) -> AppConfig:
    """New config from the form, keeping stored API keys the form only saw masked."""
    ids = [s.id for s in body.servers]
    if len(set(ids)) != len(ids) or not all(ids):
        raise HTTPException(400, "each server needs a unique id")
    servers = []
    for s in body.servers:
        if s.api_key.startswith(MASK):
            prev = old.server(s.id)
            s = s.model_copy(update={"api_key": prev.api_key if prev else ""})
        servers.append(s.model_copy(update={"base_url": s.base_url.strip().rstrip("/")}))
    img = body.images
    if img.api_key.startswith(MASK):
        img = img.model_copy(update={"api_key": old.images.api_key})
    new = old.model_copy(update={"servers": servers, "dm": body.dm, "router": body.router,
                                 "archiver": body.archiver, "tuning": body.tuning, "images": img})
    if body.clear_password:
        new.set_password(None)
    elif body.new_password:
        new.set_password(body.new_password)
    return new


def _public(cfg: AppConfig) -> dict:
    d = cfg.model_dump(exclude={"admin_password_hash", "secret"})
    for s in d["servers"]:
        s["api_key"] = _mask(s["api_key"])
    d["images"]["api_key"] = _mask(d["images"]["api_key"])
    d["has_password"] = bool(cfg.admin_password_hash)
    d["missing"] = cfg.missing()
    d["warnings"] = cfg.warnings()
    return d


async def _list_models(base_url: str, api_key: str) -> dict:
    """GET /models on an OpenAI-compatible server. Also tries base_url + /v1 (people often
    paste the bare host:port), and LM Studio's /api/v0/models for which model is loaded."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    base = base_url.strip().rstrip("/")
    candidates = [base] if base.endswith("/v1") else [base + "/v1", base]
    last_error = "no response"
    async with httpx2.AsyncClient(timeout=10) as http:
        for url in candidates:
            try:
                r = await http.get(f"{url}/models", headers=headers)
                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} from {url}/models"
                    continue
                data = r.json().get("data") or []
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
            models = [{"id": m["id"], "context": (m.get("meta") or {}).get("n_ctx"),
                       "owned_by": m.get("owned_by")} for m in data if m.get("id")]
            kind = "llama.cpp" if any(m["owned_by"] == "llamacpp" for m in models) else None
            try:  # LM Studio extras: loaded state and context length
                r2 = await http.get(f"{url.removesuffix('/v1')}/api/v0/models", headers=headers)
                if r2.status_code == 200:
                    kind = "LM Studio"
                    extra = {m["id"]: m for m in r2.json().get("data", [])}
                    for m in models:
                        e = extra.get(m["id"]) or {}
                        m["state"] = e.get("state")
                        m["context"] = e.get("loaded_context_length") or e.get("max_context_length")
            except Exception:
                pass
            return {"ok": True, "base_url": url, "kind": kind,
                    "models": [{k: v for k, v in m.items() if k != "owned_by"} for m in models]}
    return {"ok": False, "error": last_error}


MIN_CONTEXT = {"dm": 32768, "router": 16384, "archiver": 32768}

LOOKUP_TOOL = {"type": "function", "function": {
    "name": "lookup", "description": "Look up a person or place in the campaign wiki",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                   "required": ["name"]}}}
JSON_SCHEMA = {"type": "object", "properties": {"transition": {"type": "boolean"},
                                                 "reason": {"type": "string"}},
               "required": ["transition", "reason"], "additionalProperties": False}


async def _test_role(cfg: AppConfig, role: str) -> dict:
    """The smoke test, per role: context window, a reply, and the capability the role needs
    (tool calling for the DM, JSON-schema output for router and archiver)."""
    try:
        llm = LLMClient(cfg.slot(role))
    except NotConfigured as e:
        return {"ok": False, "error": str(e)}
    out: dict = {"model": llm.slot.model, "base_url": llm.slot.base_url}
    try:
        out["context_window"] = await llm.context_window()
        t = time.time()
        msg = await llm.chat([{"role": "user", "content": "Reply with exactly: ready"}],
                             max_tokens=600)
        out["reply"] = (msg.get("content") or "").strip()[:80]
        out["reply_seconds"] = round(time.time() - t, 1)
        out["thinks"] = bool(msg.get("reasoning_content"))
        if role == "dm":
            msg = await llm.chat(
                [{"role": "system", "content": "You are a game master. Use tools to look things up."},
                 {"role": "user", "content": "What do we know about the smuggler Tavi Orsk?"}],
                tools=[LOOKUP_TOOL], max_tokens=1500)
            calls = msg.get("tool_calls") or []
            out["tool_calling"] = bool(calls)
            if not calls:
                out["note"] = ("No tool call: the GM can't look things up in the wiki. For llama.cpp, "
                               "start the server with --jinja.")
        else:
            t = time.time()
            v = await llm.json([{"role": "user", "content":
                                 "The characters leave the bar and fly to another planet. "
                                 "Is that a scene transition?"}], JSON_SCHEMA, max_tokens=200)
            out["json_output"] = isinstance(v.get("transition"), bool)
            out["json_seconds"] = round(time.time() - t, 1)
        out["ok"] = bool(out["reply"]) and out.get("tool_calling", out.get("json_output", True))
        minimum = MIN_CONTEXT.get(role)
        ctx = out.get("context_window")
        out["warnings"] = [f"context window {ctx:,} tokens is below the {minimum:,} minimum for "
                           f"this job (see Recommended server settings)"] if ctx and ctx < minimum else []
    except Exception as e:
        out.update(ok=False, error=f"{type(e).__name__}: {e}")
    return out


def register(app: FastAPI, R) -> None:
    """Add the admin routes. `R()` returns the live Runtime."""

    def guard(request: Request) -> None:
        cfg = R().settings.app
        if cfg.admin_password_hash and request.cookies.get(COOKIE) != cfg.admin_token():
            raise HTTPException(401, "admin login required")

    auth = [Depends(guard)]

    def set_cookie(response: Response, cfg: AppConfig) -> None:
        if cfg.admin_password_hash:
            response.set_cookie(COOKIE, cfg.admin_token(), httponly=True, samesite="strict",
                                max_age=30 * 24 * 3600)

    @app.post("/api/admin/login")
    async def login(body: Login, response: Response):
        cfg = R().settings.app
        if not cfg.check_password(body.password):
            raise HTTPException(401, "wrong password")
        set_cookie(response, cfg)
        return {"ok": True}

    @app.post("/api/admin/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ---- config ---------------------------------------------------------------

    @app.get("/api/admin/config", dependencies=auth)
    async def get_config():
        return _public(R().settings.app)

    @app.put("/api/admin/config", dependencies=auth)
    async def put_config(body: ConfigIn, response: Response):
        rt = R()
        new = _merge(body, rt.settings.app)
        rt.settings.app = new
        rt.settings.save()
        rt.reload()
        set_cookie(response, new)  # changing the password keeps this browser logged in
        return _public(new)

    @app.post("/api/admin/probe", dependencies=auth)
    async def probe(body: Probe):
        key = body.api_key
        if key.startswith(MASK):
            prev = R().settings.app.server(body.server_id)
            key = prev.api_key if prev else ""
        return await _list_models(body.base_url, key)

    @app.post("/api/admin/test/{role}", dependencies=auth)
    async def test_role(role: str, body: ConfigIn):
        """Tests the role as configured in the (possibly unsaved) form."""
        if role not in ROLES:
            raise HTTPException(404, "unknown role")
        return await _test_role(_merge(body, R().settings.app), role)

    @app.post("/api/admin/test-images", dependencies=auth)
    async def test_images(body: ConfigIn):
        """Paint one small test portrait with the (possibly unsaved) image settings."""
        cfg = _merge(body, R().settings.app).images
        t = time.time()
        try:
            webp = await images.generate(cfg, images.portrait_prompt(
                "plain", "a friendly innkeeper with a grey beard and a leather apron"), 512, 512)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        import base64
        return {"ok": True, "seconds": round(time.time() - t, 1),
                "image": "data:image/webp;base64," + base64.b64encode(webp).decode()}

    # ---- campaigns ------------------------------------------------------------

    @app.get("/api/admin/themes", dependencies=auth)
    async def list_themes():
        return {"plain": "Plain", **{k: v["label"] for k, v in themes.public().items()}}

    @app.get("/api/admin/campaigns", dependencies=auth)
    async def campaigns():
        rt = R()
        out = []
        for c in rt.vault.campaigns():
            state = c.load_state()
            out.append({
                "slug": c.slug, **{k: c.meta.get(k, "") for k in
                                   ("name", "system", "premise", "dm_instructions")},
                "allow_rewind": bool(c.meta.get("allow_rewind")), **context.table(c.meta),
                "tone": c.meta.get("tone") or suggest.tone_for(rt.vault.root, c.meta.get("system") or ""),
                "theme": c.meta.get("theme") or themes.default_for(c.meta.get("system") or ""),
                "messages": len(c.messages()), "scenes": len(state.scenes),
                "filed": sum(s.status == "compacted" for s in state.scenes),
                "wiki_entries": len(c.gazetteer()), "last_activity": c.last_activity(),
                "busy": rt.st(c.slug)["compacting"],
            })
        return out

    @app.patch("/api/admin/campaigns/{slug}", dependencies=auth)
    async def edit_campaign(slug: str, body: CampaignEdit):
        c = R().campaign(slug)
        meta = {**c.meta, **body.model_dump()}
        meta.update(context.table(meta))  # normalise unknown values
        if meta.get("theme") not in (*themes.THEMES, themes.PLAIN):
            meta["theme"] = themes.PLAIN
        c.save_meta(meta)
        if not any(s.status == "compacted" for s in c.load_state().scenes):
            # nothing filed yet, so the brief is still just the premise: keep it in step
            c.write("brief.md", f"# {body.name}\n\n## Premise\n\n{body.premise.strip() or '(not set)'}\n")
        return {"ok": True}

    @app.delete("/api/admin/campaigns/{slug}", dependencies=auth)
    async def delete_campaign(slug: str):
        """Moves the campaign folder to <vault>/trash/ rather than deleting it."""
        rt = R()
        c = rt.campaign(slug)
        if rt.st(slug)["compacting"]:
            raise HTTPException(409, "the campaign is being filed; try again shortly")
        return {"ok": True, "moved_to": str(rt.vault.trash(c))}

    @app.post("/api/admin/campaigns/{slug}/rebuild", dependencies=auth)
    async def rebuild(slug: str):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)
        if rt.st(slug)["compacting"]:
            raise HTTPException(409, "already filing this campaign")
        return start_job(rt, "rebuild", c, f"Rebuild wiki: {c.meta.get('name')}",
                         lambda job: _rebuild(rt, c, job))

    @app.get("/api/admin/campaigns/{slug}/arc", dependencies=auth)
    async def get_arc(slug: str):
        c = R().campaign(slug)
        hist = sorted(p.name for p in c.path(arc.HISTORY_DIR).glob("*.md")) \
            if c.path(arc.HISTORY_DIR).exists() else []
        return {"text": c.read(arc.FILE), "versions": len(hist)}

    @app.post("/api/admin/campaigns/{slug}/arc", dependencies=auth)
    async def new_arc(slug: str):
        rt = R()
        rt.require_configured()
        c = rt.campaign(slug)

        async def work(job):
            job["progress"] = "writing the story arc (about a minute)…"
            await arc.generate(c, rt.archiver)
            return {"arc": "written"}
        return start_job(rt, "arc", c, f"Story arc: {c.meta.get('name')}", work)

    # ---- import ---------------------------------------------------------------

    @app.post("/api/admin/import/inspect", dependencies=auth)
    async def inspect(file: UploadFile = File(...)):
        rt = R()
        uploads = rt.vault.root / ".uploads"
        uploads.mkdir(exist_ok=True)
        token = secrets.token_hex(8)
        path = uploads / f"{token}.json"
        path.write_bytes(await file.read())
        try:
            chats = openwebui.load_chats(path)
        except Exception as e:
            path.unlink(missing_ok=True)
            raise HTTPException(400, f"not an Open WebUI export: {e}")
        out = []
        for i, chat in enumerate(chats):
            msgs = [m for m in openwebui.branch(chat) if m.get("role") in ("user", "assistant")]
            ts = [m["timestamp"] for m in msgs if m.get("timestamp")]
            out.append({"index": i, "title": chat.get("_title") or f"Chat {i + 1}",
                        "messages": len(msgs), "first": min(ts) if ts else None,
                        "last": max(ts) if ts else None})
        return {"token": token, "chats": out}

    @app.post("/api/admin/import", dependencies=auth)
    async def do_import(body: ImportIn):
        rt = R()
        rt.require_configured()
        if not re.fullmatch(r"[0-9a-f]{16}", body.token):
            raise HTTPException(400, "bad upload token")
        path = rt.vault.root / ".uploads" / f"{body.token}.json"
        if not path.exists():
            raise HTTPException(404, "upload not found; choose the file again")
        chats = openwebui.load_chats(path)
        if not 0 <= body.chat < len(chats):
            raise HTTPException(400, "no such chat in the export")
        c = openwebui.import_chat(rt.vault, chats[body.chat], body.name or None, body.system,
                                  body.premise)
        path.unlink(missing_ok=True)
        return start_job(rt, "import", c, f"Import: {c.meta.get('name')}",
                         lambda job: _process_import(rt, c, job, body.file_scenes))

    @app.get("/api/admin/jobs", dependencies=auth)
    async def jobs():
        return sorted(R().jobs.values(), key=lambda j: -j["started"])


# ---- jobs -------------------------------------------------------------------

def start_job(rt, kind: str, c, title: str, work) -> dict:
    job = {"id": secrets.token_hex(6), "kind": kind, "slug": c.slug, "title": title,
           "state": "running", "progress": "starting…", "started": time.time(),
           "finished": None, "error": None, "result": None}
    rt.jobs[job["id"]] = job

    async def run():
        st = rt.st(c.slug)
        st["compacting"] = True  # keeps idle filing and deletes away meanwhile
        try:
            job["result"] = await work(job)
            job["state"] = "done"
        except Exception as e:
            job.update(state="error", error=f"{type(e).__name__}: {e}")
        finally:
            st["compacting"] = False
            job["finished"] = time.time()

    rt.spawn(run())
    return job


def _summary(report: dict) -> dict:
    return {"filed": len(report.get("filed", [])), "seconds": report.get("seconds")}


async def _process_import(rt, c, job: dict, file_scenes: bool) -> dict:
    threshold = rt.settings.tuning.router_threshold

    def progress(n, total, verdict):
        job["progress"] = f"finding scenes: {n}/{total} exchanges"

    opened = await router.backfill(c, rt.router, threshold, progress)
    result = {"scene_boundaries": opened}
    if file_scenes:
        job["progress"] = "filing closed scenes into the wiki (about 40 s per scene)…"
        result.update(_summary(await compactor.compact(c, rt.router, rt.archiver, rt.lock(c.slug))))
    job["progress"] = "done"
    return result


async def _rebuild(rt, c, job: dict) -> dict:
    job["progress"] = "clearing the old wiki…"
    compactor.reset_wiki(c)
    job["progress"] = "filing scenes into the wiki (about 40 s per scene)…"
    report = await compactor.compact(c, rt.router, rt.archiver, rt.lock(c.slug))
    job["progress"] = "done"
    return _summary(report)
