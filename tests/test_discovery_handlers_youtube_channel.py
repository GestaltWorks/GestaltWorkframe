from __future__ import annotations

import httpx
import pytest

from core.discovery_handlers import DiscoverySourceLike
from core.discovery_handlers.youtube_channel import poll


ATOM = """<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry><id>yt:1</id><title>MSP automation video</title><link href='https://youtube.test/watch?v=1'/><published>2026-05-13T00:00:00Z</published><summary>Video summary</summary></entry></feed>"""


@pytest.mark.asyncio
async def test_youtube_channel_poll_parses_feed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "user=msp4msps" in str(request.url)
        return httpx.Response(200, text=ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await poll(DiscoverySourceLike("yt", "youtube_channel_watch", "u/msp4msps"), client)

    assert result.status == "ok"
    assert result.finds[0].finding_type == "video"
    assert result.finds[0].title == "MSP automation video"


@pytest.mark.asyncio
async def test_youtube_channel_rejects_url_targets():
    async with httpx.AsyncClient() as client:
        result = await poll(DiscoverySourceLike("yt", "youtube_channel_watch", "https://www.youtube.com/@msp4msps"), client)

    assert result.status == "error"
    assert "not a URL" in result.error