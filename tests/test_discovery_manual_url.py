"""Phase B tests: custom URL paste flow.

Covers:
- _parse_head: pulls og/twitter/title from real-world-shaped HTML.
- extract_url_metadata: rejects private / loopback URLs via the SSRF guard.
- POST /admin/api/discovery/manual-find: creates a DiscoveryFind under the
  synthetic manual_curation source and flips newsletter_pending.
- POST /admin/api/discovery/manual-find: pasting the same URL twice is a
  no-op (returns the existing row), and re-pastes can flip
  newsletter_pending back on.
- POST /admin/api/discovery/extract-metadata: surfaces SSRF rejection as a
  400 with the validator's message.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import gestaltworkframe.api.admin_discovery as api_admin_discovery
import gestaltworkframe.api.main as api_main
from gestaltworkframe.core import url_metadata as url_metadata_module
from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource
from gestaltworkframe.core.discovery_queue import MANUAL_CURATION_SOURCE_NAME
from gestaltworkframe.core.url_metadata import (
    MetadataExtractError,
    _parse_head,
    extract_url_metadata,
)


# ---------------------------------------------------------------------------
# Unit tests for the head parser. No network.
# ---------------------------------------------------------------------------


def test_parse_head_extracts_og_priority():
    html = b"""<!doctype html><html><head>
    <title>Title from title tag</title>
    <meta property="og:title" content="OG Title Wins">
    <meta name="twitter:title" content="Twitter Title (lower priority)">
    <meta property="og:description" content="A short OG description.">
    <meta property="og:image" content="https://example.com/img.png">
    <meta property="og:site_name" content="Example Site">
    </head><body>...</body></html>"""
    result = _parse_head(html, "https://example.com/post")
    assert result.title == "OG Title Wins"
    assert result.description == "A short OG description."
    assert result.image_url == "https://example.com/img.png"
    assert result.source_name == "Example Site"
    assert result.url == "https://example.com/post"


def test_parse_head_falls_back_to_title_tag_and_hostname():
    html = b"""<html><head><title>Just the title</title>
    <meta name="description" content="Plain description.">
    </head><body></body></html>"""
    result = _parse_head(html, "https://www.example.com/post")
    assert result.title == "Just the title"
    assert result.description == "Plain description."
    # Drops the leading "www." for the synthesized source label.
    assert result.source_name == "example.com"
    assert result.image_url == ""


def test_parse_head_drops_non_http_image():
    html = b"""<html><head><title>X</title>
    <meta property="og:image" content="javascript:alert(1)">
    </head><body></body></html>"""
    result = _parse_head(html, "https://example.com/")
    assert result.image_url == ""


def test_parse_head_handles_relative_image_by_dropping():
    html = b"""<html><head><title>X</title>
    <meta property="og:image" content="/local/path.png">
    </head><body></body></html>"""
    result = _parse_head(html, "https://example.com/")
    # Relative URLs are dropped rather than naively joined; the UI is
    # editable so the operator can paste a real image URL if needed.
    assert result.image_url == ""


# ---------------------------------------------------------------------------
# SSRF guard tests. No network — validate_public_https_url rejects on the
# string before any DNS resolution happens.
# ---------------------------------------------------------------------------


def test_extract_url_metadata_rejects_http_scheme():
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("http://example.com/"))


def test_extract_url_metadata_rejects_localhost():
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("https://localhost/"))


def test_extract_url_metadata_rejects_private_ip():
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("https://10.0.0.1/"))


def test_extract_url_metadata_rejects_garbage():
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("not-a-url"))


def _mock_metadata_http(monkeypatch, handler):
    real_async_client = url_metadata_module.httpx.AsyncClient
    monkeypatch.setattr(url_metadata_module, "_resolve_hostname_is_global", lambda _host: True)
    monkeypatch.setattr(
        url_metadata_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_extract_url_metadata_fetches_html_with_mock_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/post"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>Fetched title</title></head><body></body></html>",
        )

    _mock_metadata_http(monkeypatch, handler)
    result = asyncio.run(extract_url_metadata("https://example.com/post"))
    assert result.title == "Fetched title"
    assert result.url == "https://example.com/post"


def test_extract_url_metadata_rejects_redirect_to_private_before_follow(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://10.0.0.1/private"})

    _mock_metadata_http(monkeypatch, handler)
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("https://example.com/start"))
    assert seen == ["https://example.com/start"]


def test_extract_url_metadata_rejects_oversized_response(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * (url_metadata_module.MAX_RESPONSE_BYTES + 1),
        )

    _mock_metadata_http(monkeypatch, handler)
    with pytest.raises(MetadataExtractError):
        asyncio.run(extract_url_metadata("https://example.com/large"))


# ---------------------------------------------------------------------------
# Endpoint tests. SQLite-backed, no network — extract-metadata endpoint is
# exercised only with the SSRF-reject path so we don't depend on outbound
# DNS during the test run.
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gestaltworkframe.api.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(init())

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    return TestClient(api_main.app), engine, maker


def test_extract_metadata_endpoint_rejects_private_url(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/extract-metadata",
            json={"url": "https://10.0.0.1/some/path"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 400
        assert "private" in response.json()["detail"].lower() or "address" in response.json()["detail"].lower()
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_extract_metadata_endpoint_requires_admin_token(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/extract-metadata",
            json={"url": "https://example.com/"},
        )
        assert response.status_code in (401, 403)
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_manual_find_creates_synthetic_source_and_find(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/manual-find",
            json={
                "url": "https://example.com/post",
                "title": "An interesting post",
                "description": "Operator's editable summary.",
                "image_url": "https://example.com/cover.png",
                "source_label": "Example Blog",
                "queue_for_newsletter": True,
            },
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200, response.text
        find = response.json()["find"]
        assert find["title"] == "An interesting post"
        assert find["url"] == "https://example.com/post"
        assert find["status"] == "auto_indexed"
        assert find["newsletter_pending"] is True
        assert find["source_name"] == MANUAL_CURATION_SOURCE_NAME

        async def fetch_state():
            async with maker() as session:
                src_result = await session.execute(
                    select(DiscoverySource).where(
                        DiscoverySource.name == MANUAL_CURATION_SOURCE_NAME
                    )
                )
                src = src_result.scalar_one()
                find_result = await session.execute(
                    select(DiscoveryFind).where(
                        DiscoveryFind.discovery_source_id == src.id
                    )
                )
                finds = list(find_result.scalars().all())
                return src, finds

        src, finds = asyncio.run(fetch_state())
        # Synthetic source is inactive (never polled).
        assert src.active is False
        assert src.watch_type == "manual_curation"
        assert len(finds) == 1
        assert finds[0].newsletter_pending is True
        assert finds[0].finding_type == "manual_url"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_manual_find_is_idempotent_on_duplicate_url(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        first = client.post(
            "/admin/api/discovery/manual-find",
            json={
                "url": "https://example.com/post",
                "title": "First paste",
                "queue_for_newsletter": False,
            },
            headers={"X-Admin-Token": "test-admin"},
        )
        assert first.status_code == 200
        first_id = first.json()["find"]["id"]
        assert first.json()["find"]["newsletter_pending"] is False

        # Same URL, queue this time. Returns same row id, newsletter_pending
        # flipped to True.
        second = client.post(
            "/admin/api/discovery/manual-find",
            json={
                "url": "https://example.com/post",
                "title": "Second paste (title ignored)",
                "queue_for_newsletter": True,
            },
            headers={"X-Admin-Token": "test-admin"},
        )
        assert second.status_code == 200
        assert second.json()["find"]["id"] == first_id
        assert second.json()["find"]["newsletter_pending"] is True

        async def count_finds():
            async with maker() as session:
                result = await session.execute(select(DiscoveryFind))
                return len(list(result.scalars().all()))

        assert asyncio.run(count_finds()) == 1
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_manual_find_endpoint_rejects_javascript_url(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/manual-find",
            json={
                "url": "javascript:alert(1)",
                "title": "evil",
            },
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code in (400, 422)
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_manual_find_endpoint_rejects_private_url(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/manual-find",
            json={
                "url": "https://10.0.0.1/internal",
                "title": "private",
            },
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code in (400, 422)
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
