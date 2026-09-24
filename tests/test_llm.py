import json

import httpx2

from rpg_llm.config import AppConfig, ModelSlot, Role, Server, load_config, save_config
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


def config(**roles):
    return AppConfig(servers=[Server(id="box", base_url="http://box/v1", api_key="k"),
                              Server(id="other", base_url="http://other/v1")], **roles)


def test_router_and_archiver_fall_back_to_dm():
    c = config(dm=Role(server="box", model="big"))
    assert c.configured
    assert c.slot("router") == c.slot("dm") == ModelSlot("http://box/v1", "big", "k")
    assert c.slot("archiver") == c.slot("dm")


def test_each_role_uses_its_own_server_and_key():
    c = config(dm=Role(server="box", model="big"), router=Role(server="other", model="small"))
    assert c.slot("router") == ModelSlot("http://other/v1", "small", "none")


def test_unconfigured_reports_what_is_missing():
    assert AppConfig().missing() == ["add a model server", "choose the DM model"]
    c = config(dm=Role(server="box", model="big"), router=Role(server="gone", model="x"))
    assert not c.configured and "router" in c.missing()[0]


def test_one_model_at_a_time_server_warns_when_roles_would_swap():
    c = AppConfig(servers=[Server(id="lm", base_url="http://lm/v1", one_model_at_a_time=True)],
                  dm=Role(server="lm", model="big"), router=Role(server="lm", model="small"))
    assert c.warnings() and "swapping" in c.warnings()[0]
    c.router = Role()  # same as DM: no swapping
    assert not c.warnings()


def test_password_and_config_round_trip(tmp_path):
    c = config(dm=Role(server="box", model="big"))
    c.set_password("hunter2")
    save_config(tmp_path, c)
    loaded = load_config(tmp_path)
    assert loaded.check_password("hunter2") and not loaded.check_password("nope")
    assert loaded.admin_token() == c.admin_token()
    assert (tmp_path / "config.yaml").stat().st_mode & 0o077 == 0  # API keys: owner-only
