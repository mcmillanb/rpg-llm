"""Accounts and sessions.

With no accounts the app is open (a first run, or a single player on a private network). Once
the first account exists (the admin), every page and API call needs a login: a signed session
cookie naming the user. Players see and manage only their own campaigns (the owner recorded in
campaign.yaml); the admin also runs the server: model connections, image generation, tuning,
accounts. Nobody sees anyone else's campaigns, the admin included.

The logged-in user for the current request lives in a context variable, so the one campaign
lookup every endpoint goes through (Runtime.campaign) can refuse other people's campaigns
without each endpoint having to remember to.
"""

import hashlib
import hmac
import re
import time
from contextvars import ContextVar
from urllib.parse import quote, unquote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from rpg_llm.config import AppConfig, User

COOKIE = "rpg_session"
SESSION_DAYS = 30
CURRENT: ContextVar[User | None] = ContextVar("current_user", default=None)
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
MIN_PASSWORD = 8
# Paths that work without a login: the login page and its API, and the static files (styles,
# backdrops), which hold nothing private.
OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/api/session"}

# ---- sessions -------------------------------------------------------------------------------


def _sign(cfg: AppConfig, user: User, issued: int) -> str:
    # the password hash is part of the signature: changing a password ends its old sessions
    msg = f"{user.username}:{issued}:{user.password_hash}".encode()
    return hmac.new(cfg.secret.encode(), msg, hashlib.sha256).hexdigest()


def session_token(cfg: AppConfig, user: User) -> str:
    issued = int(time.time())
    return f"{quote(user.username)}:{issued}:{_sign(cfg, user, issued)}"


def user_from_token(cfg: AppConfig, token: str | None) -> User | None:
    try:
        name, issued, sig = (token or "").rsplit(":", 2)
        issued_at = int(issued)
    except ValueError:
        return None
    user = cfg.user(unquote(name))
    if user is None or time.time() - issued_at > SESSION_DAYS * 86400:
        return None
    return user if hmac.compare_digest(sig, _sign(cfg, user, issued_at)) else None


def set_session(response, request: Request, cfg: AppConfig, user: User) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, session_token(cfg, user), httponly=True, samesite="lax",
                        secure=secure, max_age=SESSION_DAYS * 86400)


def current() -> User | None:
    return CURRENT.get()


def is_admin(cfg: AppConfig) -> bool:
    """The admin, or anyone while no accounts exist yet (first run)."""
    u = CURRENT.get()
    return not cfg.users or (u is not None and u.role == "admin")


def require_admin(cfg: AppConfig) -> None:
    if not is_admin(cfg):
        raise HTTPException(403, "only the admin can do that")


def may_open(meta: dict) -> bool:
    """Whether the current user may see this campaign. Open mode (no accounts): everyone."""
    u = CURRENT.get()
    return u is None or meta.get("owner") == u.username


# ---- login attempts --------------------------------------------------------------------------

class Throttle:
    """Slows down password guessing: after 5 failures within 15 minutes for a username or a
    client address, further attempts are refused until the window passes."""

    LIMIT, WINDOW = 5, 15 * 60

    def __init__(self):
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        self.failures[key] = [t for t in self.failures.get(key, []) if now - t < self.WINDOW]
        return self.failures[key]

    def wait(self, *keys: str) -> int:
        """Seconds until another attempt is allowed (0 = go ahead)."""
        worst = 0
        for k in keys:
            recent = self._recent(k)
            if len(recent) >= self.LIMIT:
                worst = max(worst, int(self.WINDOW - (time.time() - recent[0])) + 1)
        return worst

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._recent(k).append(time.time())

    def clear(self, *keys: str) -> None:
        for k in keys:
            self.failures.pop(k, None)


def client_address(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "?")


# ---- the gate ---------------------------------------------------------------------------------

def install(app, get_config) -> None:
    """Every request: find the logged-in user; without one (once accounts exist), API calls get
    401 and pages redirect to the login page."""

    @app.middleware("http")
    async def gate(request: Request, call_next):
        cfg: AppConfig = get_config()
        path = request.url.path
        user = user_from_token(cfg, request.cookies.get(COOKIE)) if cfg.users else None
        if cfg.users and user is None and path not in OPEN_PATHS and not path.startswith("/static/"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        token = CURRENT.set(user)
        try:
            return await call_next(request)
        finally:
            CURRENT.reset(token)
