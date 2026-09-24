import json

import httpx2

from rpg_llm.config import ModelSlot, Settings
from rpg_llm.llm import LLMClient


def fake_server(handler):
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def completion(message: dict) -> dict:
    return {
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }


async def test_chat_keeps_reasoning_separate():
    def handler(request):
        return httpx2.Response(200, json=completion(
            {"role": "assistant", "content": "ready", "reasoning_content": "thinking..."}))

    llm = LLMClient(ModelSlot("http://fake/v1", "m"), fake_server(handler))
    msg = await llm.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ready"
    assert msg["reasoning_content"] == "thinking..."


async def test_stream_splits_reasoning_and_content():
    chunks = [{"reasoning_content": "hmm"}, {"content": "Hel"}, {"content": "lo"}]
    body = "".join(
        "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 0,
                               "model": "m", "choices": [{"index": 0, "delta": d}]}) + "\n\n"
        for d in chunks
    ) + "data: [DONE]\n\n"

    def handler(request):
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    llm = LLMClient(ModelSlot("http://fake/v1", "m"), fake_server(handler))
    out = [d async for d in llm.stream([{"role": "user", "content": "hi"}])]
    assert out == [{"reasoning": "hmm"}, {"content": "Hel"}, {"content": "lo"}]


async def test_context_window_detected_from_llama_cpp_meta():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"object": "list", "data": [
            {"id": "m", "object": "model", "created": 0, "owned_by": "llamacpp",
             "meta": {"n_ctx": 262144}}]})

    llm = LLMClient(ModelSlot("http://fake/v1", "m"), fake_server(handler))
    assert await llm.context_window() == 262144


async def test_context_window_override_skips_server():
    def handler(request):
        raise AssertionError("should not call server")

    llm = LLMClient(ModelSlot("http://fake/v1", "m", context_window=32768), fake_server(handler))
    assert await llm.context_window() == 32768


def test_router_and_archiver_fall_back_to_dm(monkeypatch):
    s = Settings(_env_file=None, dm_base_url="http://box/v1", dm_model="big", dm_api_key="k")
    assert s.router == s.dm
    assert s.archiver == s.dm


def test_router_on_other_server_does_not_inherit_dm_key():
    s = Settings(_env_file=None, dm_base_url="http://box/v1", dm_model="big", dm_api_key="secret",
                 router_base_url="http://other/v1", router_model="small")
    assert s.router.api_key == "none"
    assert s.router.model == "small"
