from __future__ import annotations

import httpx
import pytest

from core.discovery_handlers import DiscoverySourceLike
from core.discovery_handlers.rss import poll


_RSS_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Blog</title>
    <link>https://example.test/blog/</link>
    <description>Test feed</description>
    <item>
      <title>First post</title>
      <link>https://example.test/blog/first-post</link>
      <guid>https://example.test/blog/first-post</guid>
      <pubDate>Mon, 12 May 2026 12:00:00 GMT</pubDate>
      <description>A <strong>summary</strong> with markup.</description>
    </item>
    <item>
      <title>Second post</title>
      <link>https://example.test/blog/second-post</link>
      <guid>guid-2</guid>
      <pubDate>Sun, 11 May 2026 09:00:00 GMT</pubDate>
      <description>Plain summary.</description>
    </item>
  </channel>
</rss>
"""

_ATOM_BODY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Blog</title>
  <id>urn:uuid:test-feed</id>
  <updated>2026-05-12T12:00:00Z</updated>
  <entry>
    <id>urn:uuid:entry-1</id>
    <title>Atom entry one</title>
    <link href="https://example.test/atom/one" />
    <updated>2026-05-12T12:00:00Z</updated>
    <published>2026-05-12T12:00:00Z</published>
    <summary>An atom summary.</summary>
  </entry>
  <entry>
    <id>urn:uuid:entry-2</id>
    <title>Atom entry two</title>
    <link href="https://example.test/atom/two" />
    <updated>2026-05-11T09:00:00Z</updated>
    <published>2026-05-11T09:00:00Z</published>
    <content>Atom content body.</content>
  </entry>
</feed>
"""


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _source(target: str = "https://example.test/feed.xml", etag: str = "", last_modified: str = "") -> DiscoverySourceLike:
    return DiscoverySourceLike(
        name="example_feed",
        watch_type="rss_feed",
        target=target,
        etag=etag,
        last_modified=last_modified,
    )


@pytest.mark.asyncio
async def test_poll_parses_rss_items_and_strips_html():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_RSS_BODY,
            headers={"Content-Type": "application/rss+xml", "ETag": '"r1"', "Last-Modified": "Mon, 12 May 2026 12:00:00 GMT"},
        )

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.status == "ok"
    assert result.etag == '"r1"'
    assert result.last_modified == "Mon, 12 May 2026 12:00:00 GMT"
    assert len(result.finds) == 2
    first = result.finds[0]
    assert first.finding_type == "post"
    assert first.external_id == "rss:https://example.test/blog/first-post"
    assert first.title == "First post"
    assert first.url == "https://example.test/blog/first-post"
    assert "<strong>" not in first.summary_text
    assert "summary" in first.summary_text


@pytest.mark.asyncio
async def test_poll_parses_atom_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ATOM_BODY, headers={"Content-Type": "application/atom+xml"})

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.status == "ok"
    assert len(result.finds) == 2
    titles = {find.title for find in result.finds}
    assert titles == {"Atom entry one", "Atom entry two"}
    first = next(find for find in result.finds if find.title == "Atom entry one")
    assert first.external_id.startswith("atom:")
    assert first.url == "https://example.test/atom/one"
    assert first.summary_text == "An atom summary."


@pytest.mark.asyncio
async def test_poll_handles_304_not_modified():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("If-None-Match") == '"prev"'
        return httpx.Response(304)

    async with _client(handler) as client:
        result = await poll(_source(etag='"prev"'), client)

    assert result.finds == []
    assert result.status == "not_modified"
    assert result.etag == '"prev"'


@pytest.mark.asyncio
async def test_poll_returns_error_on_http_4xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.finds == []
    assert result.status == "error"
    assert "404" in result.error


@pytest.mark.asyncio
async def test_poll_rejects_unparseable_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<<not valid xml>>")

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.status == "ok"
    assert result.finds == []


@pytest.mark.asyncio
async def test_poll_rejects_non_http_target():
    async with httpx.AsyncClient() as client:
        result = await poll(_source(target="not-a-url"), client)

    assert result.finds == []
    assert result.status == "error"
    assert "Invalid rss_feed target" in result.error


@pytest.mark.asyncio
async def test_poll_rejects_private_http_targets():
    async with httpx.AsyncClient() as client:
        result = await poll(_source(target="http://127.0.0.1:8080/feed.xml"), client)

    assert result.finds == []
    assert result.status == "error"
    assert "must use https://" in result.error
