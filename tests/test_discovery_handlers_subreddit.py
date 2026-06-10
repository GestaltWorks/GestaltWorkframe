from __future__ import annotations

import httpx
import pytest

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike
from gestaltworkframe.core.discovery_handlers.subreddit import poll


@pytest.mark.asyncio
async def test_subreddit_poll_returns_post_candidates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/r/msp/new.json"
        payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc",
                            "title": "Automation thread",
                            "permalink": "/r/msp/comments/abc/thread/",
                            "score": 51,
                            "selftext": "Useful MSP automation discussion",
                        }
                    }
                ]
            }
        }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await poll(DiscoverySourceLike("msp", "subreddit_watch", "r/msp"), client)

    assert result.status == "ok"
    assert result.finds[0].importance_signal == "high"
    assert result.finds[0].external_id == "reddit:msp:abc"