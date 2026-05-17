from __future__ import annotations

import httpx
import pytest

from core.discovery_handlers import DiscoverySourceLike
from core.discovery_handlers.github_user_org import poll


@pytest.mark.asyncio
async def test_github_user_org_poll_returns_repo_candidates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/orgs/platform-app/repos"
        return httpx.Response(200, json=[{"id": 2, "name": "docs", "full_name": "platform-app/docs", "html_url": "https://github.com/platform-app/docs", "description": "Docs"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await poll(DiscoverySourceLike("platform-app", "github_user_org_watch", "platform-app"), client)

    assert result.status == "ok"
    assert result.finds[0].finding_type == "new_repo"
    assert result.finds[0].external_id == "github-user-org:platform-app:2"