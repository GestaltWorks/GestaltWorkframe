import pytest

from core.model_profile import GenerationParams
from core.router import LLMRouter


class _StubLocalProvider:
    # Minimal local provider stub that records whether generation was attempted.

    def __init__(self, *, model_available: bool):
        self.model = "local-model"
        self.params = GenerationParams()
        self._model_available = model_available
        self.chat_calls = 0
        self.stream_calls = 0

    async def health_status(self):
        return {
            "endpoint_healthy": self._model_available,
            "model_available": self._model_available,
            "available_models": ["local-model"] if self._model_available else [],
        }

    async def is_healthy(self):
        return self._model_available

    async def chat(self, messages, tools=None, max_tokens=None):
        self.chat_calls += 1
        return {"role": "assistant", "content": "local answer"}

    async def stream_chat(self, messages, tools=None, max_tokens=None):
        self.stream_calls += 1
        yield {"choices": [{"delta": {"content": "local answer"}}]}


MESSAGES = [{"role": "user", "content": "give me a technical answer"}]


@pytest.mark.asyncio
async def test_down_local_route_is_skipped_without_chat_attempt():
    # Regression: route selection used to ignore provider health, so a down
    # local endpoint was still selected and the router issued a real chat()
    # that blocked the provider full timeout before failing. The health gate
    # must skip it without ever calling chat().
    local = _StubLocalProvider(model_available=False)
    router = LLMRouter(primary=local, health_cache_ttl_seconds=0.0)

    result = await router.chat(MESSAGES)

    assert local.chat_calls == 0
    assert result is LLMRouter._LOCAL_UNAVAILABLE


@pytest.mark.asyncio
async def test_healthy_local_route_still_used():
    local = _StubLocalProvider(model_available=True)
    router = LLMRouter(primary=local, health_cache_ttl_seconds=0.0)

    result = await router.chat(MESSAGES)

    assert local.chat_calls == 1
    assert result["content"] == "local answer"


@pytest.mark.asyncio
async def test_down_local_route_is_skipped_in_stream():
    local = _StubLocalProvider(model_available=False)
    router = LLMRouter(primary=local, health_cache_ttl_seconds=0.0)

    chunks = [chunk async for chunk in router.stream_chat(MESSAGES)]

    assert local.stream_calls == 0
    assert chunks == [LLMRouter._LOCAL_UNAVAILABLE["content"]]
