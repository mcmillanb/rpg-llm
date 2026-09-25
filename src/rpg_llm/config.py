"""Configuration.

Two layers:
- `Env`: where the app keeps its data and which port it serves on. Environment variables with
  sensible defaults; nothing else is read from the environment.
- `AppConfig`: everything else (model servers, which model does which job, tuning, admin
  password), edited in the admin page and stored in `<vault>/config.yaml` so it lives with the
  campaigns (and survives container rebuilds).

Model servers and roles are separate: add any number of OpenAI-compatible servers (e.g. one
llama.cpp instance per GPU, each on its own port), then point the DM, router and archiver roles
at a server + model. Router and archiver fall back to the DM when left unset.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

CONFIG_FILE = "config.yaml"
ROLES = ("dm", "router", "archiver")


@dataclass(frozen=True)
class ModelSlot:
    base_url: str
    model: str
    api_key: str = "none"  # the openai SDK requires a value even when the server ignores it
    context_window: int | None = None  # manual override; otherwise detected from the server


class Env(BaseSettings):
    """VAULT_PATH, HOST and PORT environment variables (all optional)."""

    vault_path: Path = Path("vault")
    host: str = "0.0.0.0"
    port: int = 8700


class Server(BaseModel):
    id: str
    name: str = ""
    base_url: str
    api_key: str = ""
    # LM Studio and similar list several models but only run one at a time: pointing two roles
    # at different models here makes it swap on nearly every call.
    one_model_at_a_time: bool = False


class Role(BaseModel):
    server: str | None = None  # Server.id; None = same as the DM (router/archiver only)
    model: str | None = None
    context_window: int | None = None
    thinking: bool = True  # DM only: False asks Qwen-style models to skip their reasoning


class ImageGen(BaseModel):
    """Optional image generator for portraits and scene art."""
    kind: str = "none"  # none | comfyui | openai
    base_url: str = ""
    api_key: str = ""
    model: str = ""  # openai-style APIs: the image model name
    # comfyui: model files for the built-in Qwen-Image 2.1 workflow
    unet: str = "qwen_image_2.1_bf16.safetensors"
    clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    vae: str = "qwen_image_2.1_vae_bf16.safetensors"
    steps: int = Field(20, ge=1, le=100)

    @property
    def enabled(self) -> bool:
        return self.kind in ("comfyui", "openai") and bool(self.base_url.strip())


class Tuning(BaseModel):
    live_tail_pct: float = Field(50.0, ge=5, le=95)
    router_threshold: float = Field(0.7, ge=0, le=1)
    gatekeeper_enabled: bool = True
    gatekeeper_timeout: float = Field(30.0, ge=1)
    idle_compact_hours: float = Field(6.0, ge=0)


class AppConfig(BaseModel):
    servers: list[Server] = []
    dm: Role = Role()
    router: Role = Role()
    archiver: Role = Role()
    tuning: Tuning = Tuning()
    images: ImageGen = ImageGen()
    admin_password_hash: str | None = None
    secret: str = Field(default_factory=lambda: secrets.token_hex(16))  # signs admin cookies

    def server(self, server_id: str | None) -> Server | None:
        return next((s for s in self.servers if s.id == server_id), None)

    def missing(self) -> list[str]:
        """What still needs setting before the game can run."""
        out = []
        if not self.servers:
            out.append("add a model server")
        if not (self.server(self.dm.server) and self.dm.model):
            out.append("choose the DM model")
        for name in ("router", "archiver"):
            r = getattr(self, name)
            if r.server is not None and not (self.server(r.server) and r.model):
                out.append(f"finish the {name} model (or set it to 'same as DM')")
        return out

    @property
    def configured(self) -> bool:
        return not self.missing()

    def warnings(self) -> list[str]:
        out = []
        for srv in self.servers:
            if not srv.one_model_at_a_time:
                continue
            used = {}
            for role in ROLES:
                try:
                    slot = self.slot(role)
                except NotConfigured:
                    continue
                if slot.base_url == srv.base_url:
                    used.setdefault(slot.model, []).append(role)
            if len(used) > 1:
                out.append(f"{srv.name or srv.id} runs one model at a time, but "
                           + " and ".join(f"{'/'.join(r)} use {m}" for m, r in used.items())
                           + ": it will keep swapping models.")
        return out

    def slot(self, role: str) -> ModelSlot:
        r: Role = getattr(self, role)
        if role != "dm" and r.server is None:
            return self.slot("dm")
        srv = self.server(r.server)
        if srv is None or not r.model:
            raise NotConfigured(f"the {role} model is not set up")
        return ModelSlot(srv.base_url, r.model, srv.api_key or "none", r.context_window)

    # ---- admin password ----------------------------------------------------

    def set_password(self, password: str | None) -> None:
        if not password:
            self.admin_password_hash = None
            return
        salt = secrets.token_hex(8)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
        self.admin_password_hash = f"{salt}${digest}"

    def check_password(self, password: str) -> bool:
        if not self.admin_password_hash:
            return True
        salt, digest = self.admin_password_hash.split("$", 1)
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
        return hmac.compare_digest(test, digest)

    def admin_token(self) -> str:
        """Cookie value for a logged-in admin; changes whenever the password does."""
        return hmac.new(self.secret.encode(), (self.admin_password_hash or "").encode(),
                        "sha256").hexdigest()


class NotConfigured(RuntimeError):
    pass


def load_config(vault_path: Path) -> AppConfig:
    p = vault_path / CONFIG_FILE
    if p.exists():
        return AppConfig.model_validate(yaml.safe_load(p.read_text()) or {})
    return AppConfig()


def save_config(vault_path: Path, cfg: AppConfig) -> None:
    vault_path.mkdir(parents=True, exist_ok=True)
    p = vault_path / CONFIG_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False, allow_unicode=True))
    tmp.chmod(0o600)  # holds API keys
    tmp.replace(p)


@dataclass
class Settings:
    """Env + AppConfig together; what the app, scripts and evals pass around."""

    env: Env
    app: AppConfig

    @classmethod
    def load(cls) -> "Settings":
        env = Env()
        return cls(env, load_config(env.vault_path))

    def save(self) -> None:
        save_config(self.env.vault_path, self.app)

    @property
    def vault_path(self) -> Path:
        return self.env.vault_path

    @property
    def tuning(self) -> Tuning:
        return self.app.tuning

    @property
    def dm(self) -> ModelSlot:
        return self.app.slot("dm")

    @property
    def router(self) -> ModelSlot:
        return self.app.slot("router")

    @property
    def archiver(self) -> ModelSlot:
        return self.app.slot("archiver")
