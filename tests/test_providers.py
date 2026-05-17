from types import SimpleNamespace

import httpx
import pytest

from core.model_profile import GenerationParams
from core.providers import ClaudeProvider, LocalProvider, OllamaProvider, OpenAICompatibleProvider, _openai_tools


def _tool():
    return {
        "name": "lookup",
        "description": "Lookup a thing",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }


def test_openai_tool_conversion_is_shared():
    assert _openai_tools([_tool()]) == [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup a thing",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]


@pytest.mark.asyncio
async def test_local_chat_uses_shared_tool_conversion():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    provider = LocalProvider(base_url="http://test", model="local")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")

    try:
        result = await provider.chat([{"role": "user", "content": "hi"}], tools=[_tool()])
    finally:
        await provider.close()

    assert result["content"] == "ok"
    assert b'"tools"' in seen["payload"]
    assert b'"lookup"' in seen["payload"]


@pytest.mark.asyncio
async def test_local_health_is_model_level():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "loaded-model"}]})

    provider = LocalProvider(base_url="http://test", model="missing-model")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")

    try:
        status = await provider.health_status()
    finally:
        await provider.close()

    assert status["endpoint_healthy"] is True
    assert status["model_available"] is False
    assert status["available_models"] == ["loaded-model"]


@pytest.mark.asyncio
async def test_local_stream_chat_uses_max_tokens_override():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = request.read()
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

    provider = LocalProvider(
        base_url="http://test",
        model="local",
        params=GenerationParams(max_tokens=123),
    )
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")

    try:
        chunks = [chunk async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}], max_tokens=456)]
    finally:
        await provider.close()

    assert chunks
    assert b'"max_tokens":456' in seen["payload"]


@pytest.mark.asyncio
async def test_ollama_stream_chat_yields_message_content_and_options():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = request.read()
        body = b'{"message":{"content":"hel"},"done":false}\n{"message":{"content":"lo"},"done":false}\n{"done":true}\n'
        return httpx.Response(200, content=body)

    provider = OllamaProvider(
        base_url="http://test",
        model="llama3",
        params=GenerationParams(temperature=0.2, max_tokens=123, top_p=0.9, stop=["STOP"]),
    )
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")

    try:
        chunks = [chunk async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}], tools=[_tool()], max_tokens=456)]
    finally:
        await provider.close()

    assert chunks == ["hel", "lo"]
    payload = seen["payload"].decode()
    assert '"stream":true' in payload
    assert '"num_predict":456' in payload
    assert '"stop":["STOP"]' in payload
    assert '"lookup"' in payload


@pytest.mark.asyncio
async def test_openai_compatible_chat_uses_bearer_key_and_chat_completions():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["payload"] = request.read()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    provider = OpenAICompatibleProvider(base_url="https://cloud.example/v1", api_key="sk-test", model="gemma")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example/v1", headers={"Authorization": "Bearer sk-test"})

    try:
        result = await provider.chat([{"role": "user", "content": "hi"}], tools=[_tool()])
    finally:
        await provider.close()

    assert result["content"] == "ok"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["path"] == "/v1/chat/completions"
    assert b'"model":"gemma"' in seen["payload"]
    assert b'"lookup"' in seen["payload"]


@pytest.mark.asyncio
async def test_openai_compatible_health_normalizes_google_model_prefix():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "models/gemini-2.5-flash"}]})

    provider = OpenAICompatibleProvider(base_url="https://cloud.example/v1", api_key="sk-test", model="gemini-2.5-flash")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example/v1")

    try:
        status = await provider.health_status()
    finally:
        await provider.close()

    assert status["endpoint_healthy"] is True
    assert status["model_available"] is True
    assert status["available_models"] == ["models/gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_openai_compatible_health_handles_network_failure():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    provider = OpenAICompatibleProvider(base_url="https://cloud.example/v1", api_key="sk-test", model="gemini-2.5-flash")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example/v1")

    try:
        status = await provider.health_status()
    finally:
        await provider.close()

    assert status == {"endpoint_healthy": False, "model_available": False, "available_models": []}


@pytest.mark.asyncio
async def test_openai_compatible_stream_chat_yields_text_chunks():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = request.read()
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
                b'data: [DONE]\n\n'
            ),
        )

    provider = OpenAICompatibleProvider(base_url="https://cloud.example/v1", api_key="sk-test", model="gemini-2.5-flash")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example/v1")

    try:
        chunks = [chunk async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}], max_tokens=123)]
    finally:
        await provider.close()

    assert chunks == ["hel", "lo"]
    assert b'"stream":true' in seen["payload"]
    assert b'"max_tokens":123' in seen["payload"]


@pytest.mark.asyncio
async def test_openai_compatible_stream_chat_wraps_http_errors():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "bad"})

    provider = OpenAICompatibleProvider(base_url="https://cloud.example/v1", api_key="sk-test", model="gemini-2.5-flash")
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://cloud.example/v1")

    with pytest.raises(Exception, match="OpenAICompatibleProvider stream_chat failed"):
        async for _ in provider.stream_chat([{"role": "user", "content": "hi"}]):
            pass

    await provider.close()


class _Models:
    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises

    async def list(self):
        if self.raises:
            raise self.raises
        return SimpleNamespace(data=[])


def _claude(client, max_tokens: int = 321) -> ClaudeProvider:
    provider = ClaudeProvider.__new__(ClaudeProvider)
    provider.client = client
    provider.model = "claude-test"
    provider.params = GenerationParams(max_tokens=max_tokens)
    return provider


@pytest.mark.asyncio
async def test_claude_health_uses_api_call():
    assert await _claude(SimpleNamespace(models=_Models())).is_healthy() is True
    assert await _claude(SimpleNamespace(models=_Models(RuntimeError("bad key")))).is_healthy() is False


class _StreamContext:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        self._events = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


class _Messages:
    def __init__(self) -> None:
        self.params = None

    async def create(self, **params):
        self.params = params
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    def stream(self, **params):
        self.params = params
        return _StreamContext(["chunk"])


@pytest.mark.asyncio
async def test_claude_chat_moves_system_messages_to_system_param():
    messages = _Messages()
    provider = _claude(SimpleNamespace(messages=messages), max_tokens=777)

    await provider.chat([
        {"role": "system", "content": "Persona"},
        {"role": "system", "content": "Safety"},
        {"role": "user", "content": "hi"},
    ])

    assert messages.params["system"] == "Persona\n\nSafety"
    assert messages.params["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_claude_stream_uses_profile_max_tokens():
    messages = _Messages()
    provider = _claude(SimpleNamespace(messages=messages), max_tokens=777)

    chunks = [chunk async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}])]

    assert chunks == ["chunk"]
    assert messages.params["max_tokens"] == 777


@pytest.mark.asyncio
async def test_claude_stream_uses_max_tokens_override():
    messages = _Messages()
    provider = _claude(SimpleNamespace(messages=messages), max_tokens=777)

    chunks = [chunk async for chunk in provider.stream_chat([{"role": "user", "content": "hi"}], max_tokens=222)]

    assert chunks == ["chunk"]
    assert messages.params["max_tokens"] == 222


@pytest.mark.asyncio
async def test_claude_stream_moves_system_messages_to_system_param():
    messages = _Messages()
    provider = _claude(SimpleNamespace(messages=messages), max_tokens=777)

    chunks = [
        chunk
        async for chunk in provider.stream_chat([
            {"role": "system", "content": "Persona"},
            {"role": "user", "content": "hi"},
        ])
    ]

    assert chunks == ["chunk"]
    assert messages.params["system"] == "Persona"
    assert messages.params["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_claude_close_awaits_client_close():
    closed = False

    class _Client:
        async def close(self):
            nonlocal closed
            closed = True

    await _claude(_Client()).close()

    assert closed is True