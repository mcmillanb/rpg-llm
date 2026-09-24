"""Thin wrapper over any OpenAI-compatible chat completions endpoint."""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
from openai import AsyncOpenAI

from rpg_llm.config import ModelSlot


class LLMClient:
    def __init__(self, slot: ModelSlot, http_client: httpx2.AsyncClient | None = None):
        self.slot = slot
        self._client = AsyncOpenAI(
            base_url=slot.base_url, api_key=slot.api_key, http_client=http_client, timeout=600
        )
        self._context_window = slot.context_window

    async def chat(self, messages: list[dict], **kwargs: Any) -> dict:
        """One non-streaming completion. Returns the first choice's message as a dict,
        with `reasoning_content` (Qwen thinking) kept separate from `content`."""
        resp = await self._client.chat.completions.create(
            model=self.slot.model, messages=messages, **kwargs
        )
        return resp.choices[0].message.model_dump(exclude_none=True)

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict]:
        """Streams deltas as dicts: {"reasoning": str} or {"content": str}."""
        stream = await self._client.chat.completions.create(
            model=self.slot.model, messages=messages, stream=True, **kwargs
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"reasoning": reasoning}
            if delta.content:
                yield {"content": delta.content}

    async def context_window(self) -> int | None:
        """Manual override if set, else what the server reports (llama.cpp puts it in
        `meta.n_ctx` on /v1/models). None if the server doesn't say."""
        if self._context_window is None:
            models = await self._client.models.list()
            for m in models.data:
                if m.id == self.slot.model:
                    meta = (m.model_extra or {}).get("meta") or {}
                    self._context_window = meta.get("n_ctx")
                    break
        return self._context_window
