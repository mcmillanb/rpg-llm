"""Thin wrapper over any OpenAI-compatible chat completions endpoint."""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from openai import AsyncOpenAI

from rpg_llm.config import ModelSlot

# Qwen thinking off for structured calls; servers that don't know the kwarg ignore it.
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


class LLMClient:
    def __init__(self, slot: ModelSlot, http_client: httpx2.AsyncClient | None = None):
        self.slot = slot
        self._client = AsyncOpenAI(
            base_url=slot.base_url, api_key=slot.api_key, http_client=http_client, timeout=900
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
        """Streams deltas as dicts: {"reasoning": str}, {"content": str}, and finally
        {"tool_calls": [...]} if the model asked for tools (arguments fully assembled)."""
        stream = await self._client.chat.completions.create(
            model=self.slot.model, messages=messages, stream=True, **kwargs
        )
        calls: dict[int, dict] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"reasoning": reasoning}
            if delta.content:
                yield {"content": delta.content}
            for tc in delta.tool_calls or []:
                call = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                call["id"] = tc.id or call["id"]
                if tc.function:
                    call["name"] += tc.function.name or ""
                    call["arguments"] += tc.function.arguments or ""
        if calls:
            yield {"tool_calls": [calls[i] for i in sorted(calls)]}

    async def json(self, messages: list[dict], schema: dict, max_tokens: int = 1500) -> dict:
        """Structured output against a JSON schema. Falls back to pulling the first JSON
        object out of the text for servers that ignore response_format."""
        msg = await self.chat(
            messages,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "result", "strict": True, "schema": schema}},
            max_tokens=max_tokens,
            temperature=0.2,
            extra_body=NO_THINKING,
        )
        return parse_json(msg.get("content") or "")

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


def parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))
