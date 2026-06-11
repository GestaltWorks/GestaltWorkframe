"""Unit tests for the provider classes in core/providers.py (no network).

HTTP providers are driven with httpx.MockTransport; the Claude provider uses a
lightweight fake in place of the Anthropic SDK client.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from gestaltworkframe.core.providers import (
    ClaudeProvider,
    LocalProvider,
    OpenAICompatibleProvider,
    _model_ids,
    _normalize_model_id,
    _openai_tools,
)


async def _collect(agen):
    return [chunk async for chunk in agen]


def _mock_client(base_url: str, handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def test_openai_tools_none_and_mapping():
    assert _openai_tools(None) is None
    assert _openai_tools([]) is None
    out = _openai_tools([{"name": "lookup", "description": "d", "input_schema": {"type": "object"}}])
    assert out == [
        {"type": "function", "function": {"name": "lookup", "description": "d", "parameters": {"type": "object"}}}
    ]


def test_model_ids_handles_data_models_and_keys():
    assert _model_ids({"data": [{"id": "a"}, {"model": "b"}, {"name": "c"}]}) == ["a", "b", "c"]
    assert _model_ids({"models": [{"id": "x"}]}) == ["x"]
    # Non-dicts and empty entries are skipped.
    assert _model_ids({"data": ["nope", {}, {"id": "y"}]}) == ["y"]
    assert _model_ids({}) == []


def test_normalize_model_id():
    assert _normalize_model_id("models/Gemini-Pro ") == "gemini-pro"


# ---------------------------------------------------------------------------
# LocalProvider
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_health_status_healthy():
    def handler(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "local-model"}]})

    p = LocalProvider(model="local-model")
    p.client = _mock_client(p.base_url, handler)
    status = await p.health_status()
    assert status["endpoint_healthy"] is True
    assert status["model_available"] is True
    assert status["available_models"] == ["local-model"]
    await p.close()


@pytest.mark.asyncio
async def test_local_health_status_unhealthy_on_error():
    def handler(request):
        raise httpx.ConnectError("down")

    p = LocalProvider()
    p.client = _mock_client(p.base_url, handler)
    status = await p.health_status()
    assert status == {"endpoint_healthy": False, "model_available": False, "available_models": []}
    await p.close()


@pytest.mark.asyncio
async def test_local_chat_success_returns_message():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

    p = LocalProvider()
    p.client = _mock_client(p.base_url, handler)
    msg = await p.chat([{"role": "user", "content": "hello"}])
    assert msg == {"role": "assistant", "content": "hi"}
    await p.close()


@pytest.mark.asyncio
async def test_local_chat_wraps_http_error():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    p = LocalProvider()
    p.client = _mock_client(p.base_url, handler)
    with pytest.raises(Exception, match="LocalProvider chat failed"):
        await p.chat([{"role": "user", "content": "x"}])
    await p.close()


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider (the OpenRouter path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_chat_success_and_tools_payload():
    seen = {}

    def handler(request):
        import json as _json
        seen["payload"] = _json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    p = OpenAICompatibleProvider(base_url="https://api.example.com/", api_key="k1", model="gpt-x")
    p.client = _mock_client(p.base_url, handler)
    msg = await p.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "t", "input_schema": {}}],
        max_tokens=128,
    )
    assert msg == {"content": "ok"}
    assert seen["payload"]["model"] == "gpt-x"
    assert seen["payload"]["max_tokens"] == 128
    assert seen["payload"]["tools"][0]["function"]["name"] == "t"
    await p.close()


@pytest.mark.asyncio
async def test_openai_health_status_normalizes_model_match():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "models/GPT-X"}]})

    p = OpenAICompatibleProvider(base_url="https://api.example.com", api_key="k1", model="gpt-x")
    p.client = _mock_client(p.base_url, handler)
    status = await p.health_status()
    assert status["endpoint_healthy"] is True
    assert status["model_available"] is True  # normalized "models/GPT-X" == "gpt-x"
    await p.close()


@pytest.mark.asyncio
async def test_openai_update_api_key_rotates_auth_header():
    p = OpenAICompatibleProvider(base_url="https://api.example.com", api_key="old", model="m")
    assert p.client.headers["Authorization"] == "Bearer old"
    await p.update_api_key("new")
    assert p.client.headers["Authorization"] == "Bearer new"
    await p.close()


# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------

def test_claude_message_params_splits_system_from_chat():
    p = ClaudeProvider(api_key="k")
    params = p._message_params(
        [
            {"role": "system", "content": "be terse"},
            {"role": "system", "content": "  "},  # blank ignored
            {"role": "user", "content": "hi"},
        ]
    )
    assert params["system"] == "be terse"
    assert params["messages"] == [{"role": "user", "content": "hi"}]


def test_claude_message_params_omits_system_when_none():
    p = ClaudeProvider(api_key="k")
    params = p._message_params([{"role": "user", "content": "hi"}])
    assert "system" not in params


@pytest.mark.asyncio
async def test_claude_update_api_key_replaces_client():
    p = ClaudeProvider(api_key="old")
    before = p.client
    await p.update_api_key("new")
    assert p.client is not before


@pytest.mark.asyncio
async def test_claude_health_status_lists_models():
    p = ClaudeProvider(api_key="k", model="claude-x")

    class _Models:
        async def list(self):
            return SimpleNamespace(data=[SimpleNamespace(id="claude-x"), SimpleNamespace(id="other")])

    p.client = SimpleNamespace(models=_Models())
    status = await p.health_status()
    assert status["endpoint_healthy"] is True
    assert status["model_available"] is True
    assert "claude-x" in status["available_models"]


@pytest.mark.asyncio
async def test_claude_chat_success_passes_system_and_returns_response():
    p = ClaudeProvider(api_key="k", model="claude-x")
    captured = {}

    class _Messages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[{"type": "text", "text": "ok"}])

    p.client = SimpleNamespace(messages=_Messages())
    resp = await p.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], max_tokens=99)
    assert resp.content[0]["text"] == "ok"
    assert captured["system"] == "sys"
    assert captured["max_tokens"] == 99
    assert captured["model"] == "claude-x"


@pytest.mark.asyncio
async def test_claude_chat_wraps_errors():
    p = ClaudeProvider(api_key="k")

    class _Messages:
        async def create(self, **kwargs):
            raise RuntimeError("api down")

    p.client = SimpleNamespace(messages=_Messages())
    with pytest.raises(Exception, match="ClaudeProvider chat failed"):
        await p.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Streaming (OpenAI-compatible SSE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_stream_chat_yields_deltas():
    body = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
        b'data: {"choices":[{"delta":{"content":" there"}}]}\n'
        b'data: [DONE]\n'
    )

    def handler(request):
        return httpx.Response(200, content=body)

    p = OpenAICompatibleProvider(base_url="https://api.example.com", api_key="k", model="m")
    p.client = _mock_client(p.base_url, handler)
    chunks = await _collect(p.stream_chat([{"role": "user", "content": "hi"}]))
    assert chunks == ["hi", " there"]
    await p.close()
