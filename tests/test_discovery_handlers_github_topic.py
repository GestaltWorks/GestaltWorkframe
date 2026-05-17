from __future__ import annotations

import httpx
import pytest

from core.discovery_handlers import DiscoverySourceLike
from core.discovery_handlers.github_topic import poll


@pytest.mark.asyncio
async def test_github_topic_poll_returns_repo_candidates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/repositories"
        assert "topic%3Aautomation" in str(request.url)
        return httpx.Response(200, json={"items": [{"id": 1, "full_name": "example/automation", "html_url": "https://github.com/example/automation", "description": "Automation tools", "stargazers_count": 5}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await poll(DiscoverySourceLike("topic", "github_topic_watch", "automation"), client)

    assert result.status == "ok"
    assert result.finds[0].finding_type == "new_repo"
    assert result.finds[0].external_id == "github-topic:automation:1"