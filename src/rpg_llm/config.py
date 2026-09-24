"""Settings loaded from the environment (and `.env`).

Three model slots share one shape. ROUTER and ARCHIVER fall back to the DM slot
for any field left unset, so a single-model setup only needs the DM_* values.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ModelSlot:
    base_url: str
    model: str
    api_key: str = "none"  # the openai SDK requires a value even when the server ignores it
    context_window: int | None = None  # manual override; otherwise detected from the server


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    dm_base_url: str = "http://localhost:8080/v1"
    dm_model: str = "default"
    dm_api_key: str = "none"
    dm_context_window: int | None = None

    router_base_url: str | None = None
    router_model: str | None = None
    router_api_key: str | None = None
    router_context_window: int | None = None

    archiver_base_url: str | None = None
    archiver_model: str | None = None
    archiver_api_key: str | None = None
    archiver_context_window: int | None = None

    vault_path: Path = Path("vault")
    live_tail_pct: float = 50.0
    router_threshold: float = 0.7
    gatekeeper_enabled: bool = True
    gatekeeper_timeout: float = 30.0
    idle_compact_hours: float = 6.0
    host: str = "0.0.0.0"
    port: int = 8700

    @property
    def dm(self) -> ModelSlot:
        return ModelSlot(self.dm_base_url, self.dm_model, self.dm_api_key, self.dm_context_window)

    @property
    def router(self) -> ModelSlot:
        return self._slot("router")

    @property
    def archiver(self) -> ModelSlot:
        return self._slot("archiver")

    def _slot(self, name: str) -> ModelSlot:
        base_url = getattr(self, f"{name}_base_url")
        model = getattr(self, f"{name}_model")
        api_key = getattr(self, f"{name}_api_key")
        ctx = getattr(self, f"{name}_context_window")
        # A different server without its own key must not inherit the DM's key.
        if api_key is None:
            api_key = self.dm_api_key if base_url in (None, self.dm_base_url) else "none"
        return ModelSlot(
            base_url or self.dm_base_url,
            model or self.dm_model,
            api_key,
            ctx if ctx is not None else (self.dm_context_window if model is None else None),
        )
