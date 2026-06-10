import inspect
import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from core.model_profile import GenerationParams

Message = dict[str, Any]
ToolSpec = dict[str, Any]


def _openai_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        }
        for tool in tools
    ]


def _model_ids(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") or payload.get("models") or []
    ids = []
    for item in data:
        if isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name")
            if value:
                ids.append(str(value))
    return ids


def _normalize_model_id(model: str) -> str:
    return model.removeprefix("models/").strip().lower()


class LLMProvider:
    """Base interface for all LLM providers."""

    async def is_healthy(self) -> bool:
        raise NotImplementedError()

    async def health_status(self) -> dict[str, Any]:
        healthy = await self.is_healthy()
        return {
            "endpoint_healthy": healthy,
            "model_available": healthy,
            "available_models": [],
        }

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        raise NotImplementedError()

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[Any, None]:
        raise NotImplementedError()

class LocalProvider(LLMProvider):
    """Provider for a local OpenAI-compatible endpoint (llama.cpp, etc.)."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        model: str = "local-model",
        params: GenerationParams | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.params = params or GenerationParams()
        self.client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        
    async def is_healthy(self) -> bool:
        return (await self.health_status())["endpoint_healthy"]

    async def health_status(self) -> dict[str, Any]:
        try:
            response = await self.client.get("/models", timeout=5.0)
            endpoint_healthy = response.status_code == 200
            models = _model_ids(response.json()) if endpoint_healthy else []
            model_available = endpoint_healthy and (not models or self.model in models)
            return {
                "endpoint_healthy": endpoint_healthy,
                "model_available": model_available,
                "available_models": models,
            }
        except Exception:
            return {"endpoint_healthy": False, "model_available": False, "available_models": []}

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.params.temperature,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                "top_p": self.params.top_p,
            }
            if self.params.stop:
                payload["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools

            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]
        except Exception as e:
            raise Exception(f"LocalProvider chat failed: {e}")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[Any, None]:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.params.temperature,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                "top_p": self.params.top_p,
                "stream": True,
            }
            if self.params.stop:
                payload["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools

            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            yield data
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            raise Exception(f"LocalProvider stream_chat failed: {e}")
        
    async def close(self):
        await self.client.aclose()

class OllamaProvider(LLMProvider):
    """Provider for Ollama's native local API."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:7b",
        params: GenerationParams | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.params = params or GenerationParams(temperature=0.3)
        self.client = httpx.AsyncClient(base_url=base_url, timeout=60.0)

    async def is_healthy(self) -> bool:
        return (await self.health_status())["endpoint_healthy"]

    async def health_status(self) -> dict[str, Any]:
        try:
            response = await self.client.get("/api/tags", timeout=5.0)
            endpoint_healthy = response.status_code == 200
            models = self._model_ids(response.json()) if endpoint_healthy else []
            model_available = endpoint_healthy and (not models or self.model in models)
            return {
                "endpoint_healthy": endpoint_healthy,
                "model_available": model_available,
                "available_models": models,
            }
        except Exception:
            return {"endpoint_healthy": False, "model_available": False, "available_models": []}

    def _model_ids(self, payload: dict[str, Any]) -> list[str]:
        ids = []
        for item in payload.get("models", []):
            if isinstance(item, dict):
                value = item.get("name") or item.get("model")
                if value:
                    ids.append(str(value))
        return ids

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": self.params.temperature,
                    "num_predict": max_tokens if max_tokens is not None else self.params.max_tokens,
                    "top_p": self.params.top_p,
                },
            }
            if self.params.stop:
                payload["options"]["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools
            response = await self.client.post("/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            message = data.get("message", {})
            return {"role": message.get("role", "assistant"), "content": message.get("content", "")}
        except Exception as e:
            raise Exception(f"OllamaProvider chat failed: {e}")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[Any, None]:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": {
                    "temperature": self.params.temperature,
                    "num_predict": max_tokens if max_tokens is not None else self.params.max_tokens,
                    "top_p": self.params.top_p,
                },
            }
            if self.params.stop:
                payload["options"]["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools

            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("done"):
                        break
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
        except Exception as e:
            raise Exception(f"OllamaProvider stream_chat failed: {e}")

    async def close(self):
        await self.client.aclose()


class OpenAICompatibleProvider(LLMProvider):
    """Cloud endpoint that exposes OpenAI-compatible chat completions."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        params: GenerationParams | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.params = params or GenerationParams(max_tokens=4096)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def update_api_key(self, new_key: str) -> None:
        """Replace the in-flight httpx client with one using the new key."""
        old = self.client
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=60.0,
            headers={"Authorization": f"Bearer {new_key}"},
        )
        await old.aclose()

    async def is_healthy(self) -> bool:
        return (await self.health_status())["endpoint_healthy"]

    async def health_status(self) -> dict[str, Any]:
        try:
            response = await self.client.get("/models", timeout=5.0)
            endpoint_healthy = response.status_code == 200
            models = _model_ids(response.json()) if endpoint_healthy else []
            normalized_models = {_normalize_model_id(model) for model in models}
            return {
                "endpoint_healthy": endpoint_healthy,
                "model_available": endpoint_healthy and (not models or _normalize_model_id(self.model) in normalized_models),
                "available_models": models,
            }
        except Exception:
            return {"endpoint_healthy": False, "model_available": False, "available_models": []}

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.params.temperature,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                "top_p": self.params.top_p,
            }
            if self.params.stop:
                payload["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools
            response = await self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]
        except Exception as e:
            raise Exception(f"OpenAICompatibleProvider chat failed: {e}")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.params.temperature,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                "top_p": self.params.top_p,
                "stream": True,
            }
            if self.params.stop:
                payload["stop"] = self.params.stop
            if converted_tools := _openai_tools(tools):
                payload["tools"] = converted_tools
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if content := delta.get("content"):
                        yield content
        except Exception as e:
            raise Exception(f"OpenAICompatibleProvider stream_chat failed: {e}")

    async def close(self):
        await self.client.aclose()


class ClaudeProvider(LLMProvider):
    """Provider for Anthropic Claude."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        params: GenerationParams | None = None,
    ):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.params = params or GenerationParams(max_tokens=4096)

    async def update_api_key(self, new_key: str) -> None:
        """Replace the Anthropic client with one using the new key."""
        self.client = AsyncAnthropic(api_key=new_key)

    def _message_params(self, messages: list[Message]) -> dict[str, Any]:
        system_parts = []
        chat_messages = []
        for message in messages:
            if message.get("role") == "system":
                content = str(message.get("content", "")).strip()
                if content:
                    system_parts.append(content)
                continue
            chat_messages.append(dict(message))
        params: dict[str, Any] = {"messages": chat_messages}
        if system_parts:
            params["system"] = "\n\n".join(system_parts)
        return params

    async def is_healthy(self) -> bool:
        return (await self.health_status())["endpoint_healthy"]

    async def health_status(self) -> dict[str, Any]:
        try:
            result = await self.client.models.list()
            models = [str(getattr(item, "id", "")) for item in getattr(result, "data", []) if getattr(item, "id", "")]
            return {
                "endpoint_healthy": True,
                "model_available": not models or self.model in models,
                "available_models": models,
            }
        except Exception:
            return {"endpoint_healthy": False, "model_available": False, "available_models": []}

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        try:
            api_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                **self._message_params(messages),
            }
            if tools:
                api_params["tools"] = tools

            response = await self.client.messages.create(**api_params)
            return response
        except Exception as e:
            raise Exception(f"ClaudeProvider chat failed: {e}")

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[Any, None]:
        try:
            params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens if max_tokens is not None else self.params.max_tokens,
                **self._message_params(messages),
            }
            if tools:
                params["tools"] = tools

            async with self.client.messages.stream(**params) as stream:
                async for event in stream:
                    yield event
        except Exception as e:
            raise Exception(f"ClaudeProvider stream_chat failed: {e}")

    async def close(self):
        close = getattr(self.client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result
