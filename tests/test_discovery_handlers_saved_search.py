from __future__ import annotations

import httpx
import pytest

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike
from gestaltworkframe.core.discovery_handlers.saved_search import poll


@pytest.mark.asyncio
async def test_saved_search_uses_brave_when_configured(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "test-key"
        return httpx.Response(200, json={"web": {"results": [{"title": "Result", "url": "https://example.test", "description": "Found"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await poll(DiscoverySourceLike("search", "saved_search", "automation workflows"), client)

    assert result.status == "ok"
    assert result.finds[0].finding_type == "mention"


@pytest.mark.asyncio
async def test_saved_search_errors_without_key(monkeypatch):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    async with httpx.AsyncClient() as client:
        result = await poll(DiscoverySourceLike("search", "saved_search", "automation workflows"), client)

    assert result.status == "error"
    assert "not configured" in result.error