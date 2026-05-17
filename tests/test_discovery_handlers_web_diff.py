from __future__ import annotations

import httpx
import pytest

from core.discovery_handlers import DiscoverySourceLike
from core.discovery_handlers.web_diff import poll


@pytest.mark.asyncio
async def test_web_diff_emits_change_then_not_modified():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="hello world"))) as client:
        first = await poll(DiscoverySourceLike("page", "web_diff", "https://example.test/page"), client)
        second = await poll(DiscoverySourceLike("page", "web_diff", "https://example.test/page", etag=first.etag), client)

    assert first.finds[0].finding_type == "diff"
    assert second.status == "not_modified"
    assert second.finds == []


@pytest.mark.asyncio
async def test_web_diff_rejects_private_http_targets():
    async with httpx.AsyncClient() as client:
        result = await poll(DiscoverySourceLike("page", "web_diff", "http://192.0.2.4/status"), client)

    assert result.status == "error"
    assert "must use https://" in result.error