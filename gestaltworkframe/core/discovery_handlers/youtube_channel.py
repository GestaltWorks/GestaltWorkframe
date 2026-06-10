"""youtube_channel_watch handler."""

from __future__ import annotations

import urllib.parse

import httpx

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, PollResult, register
from gestaltworkframe.core.discovery_handlers.rss import _detect_and_parse
from gestaltworkframe.kb.target_safety import validate_discovery_target


def _feed_url(target: str) -> str:
    value = target.strip()
    if value.startswith("channel:"):
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + urllib.parse.quote(value.split(":", 1)[1])
    if value.startswith("u/"):
        return "https://www.youtube.com/feeds/videos.xml?user=" + urllib.parse.quote(value[2:])
    if value.startswith("@"):
        return "https://www.youtube.com/feeds/videos.xml?user=" + urllib.parse.quote(value[1:])
    return "https://www.youtube.com/feeds/videos.xml?user=" + urllib.parse.quote(value)


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    try:
        validate_discovery_target(source.watch_type, source.target, source_name=source.name)
    except ValueError as exc:
        return PollResult(finds=[], status="error", error=f"Invalid youtube_channel_watch target: {exc}")
    url = _feed_url(source.target)
    response = await http.get(url, headers={"User-Agent": "gestalt-workframe-discovery/1.0"}, timeout=15)
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"YouTube feed HTTP {response.status_code}")
    finds = _detect_and_parse(response.text)
    normalized = [find._replace(finding_type="video") for find in finds]
    return PollResult(
        finds=normalized,
        etag=response.headers.get("etag", "") or "",
        last_modified=response.headers.get("last-modified", "") or "",
    )


register("youtube_channel_watch", poll)