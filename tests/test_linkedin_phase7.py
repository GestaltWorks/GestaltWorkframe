"""Phase 7: LinkedIn auto-post.

Dark-by-default behavior: no env vars set means status="skipped" with
reason="not_configured" and the approve_and_distribute path records a
single NewsletterDelivery row reflecting that. With env vars set, the
poster refreshes the access token and posts via the LinkedIn /rest/posts
endpoint.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from core import linkedin as linkedin_module
from core import newsletter as newsletter_module
from core.db import (
    DiscoveryFind,
    DiscoverySource,
    NewsletterDelivery,
    Subscriber,
)


@pytest.mark.asyncio
async def test_linkedin_skipped_when_env_not_configured(monkeypatch):
    for key in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_AUTHOR_URN"):
        monkeypatch.delenv(key, raising=False)
    assert linkedin_module.is_configured() is False
    result = await linkedin_module.post_to_linkedin("Hello world")
    assert result.status == "skipped"
    assert result.reason == "not_configured"


@pytest.mark.asyncio
async def test_linkedin_post_with_mocked_http_success(monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "csec")
    monkeypatch.setenv("LINKEDIN_REFRESH_TOKEN", "rtok")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:abc123")
    assert linkedin_module.is_configured() is True

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "atok", "expires_in": 5184000})
        if request.url.path.endswith("/posts"):
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:9999"})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with patch.object(linkedin_module.httpx, "AsyncClient", _Client):
        result = await linkedin_module.post_to_linkedin("Test post content")
    assert result.status == "sent"
    assert result.post_urn == "urn:li:share:9999"


@pytest.mark.asyncio
async def test_linkedin_post_failure_returns_failed(monkeypatch):
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "cid")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "csec")
    monkeypatch.setenv("LINKEDIN_REFRESH_TOKEN", "rtok")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:abc123")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accessToken"):
            return httpx.Response(200, json={"access_token": "atok"})
        return httpx.Response(403, text="forbidden")

    transport = httpx.MockTransport(mock_handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with patch.object(linkedin_module.httpx, "AsyncClient", _Client):
        result = await linkedin_module.post_to_linkedin("Test post")
    assert result.status == "failed"
    assert "http_403" in result.reason


@pytest.mark.asyncio
async def test_approve_records_linkedin_skipped_when_not_configured(tmp_path, monkeypatch):
    """Phase 7 integration: approve_and_distribute always writes a
    NewsletterDelivery row for the linkedin channel. With env not
    configured, status is skipped and the row carries the reason."""
    for key in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_AUTHOR_URN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'li.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        source = DiscoverySource(name="li_src", watch_type="rss_watch", target="https://x", active=True)
        session.add(source)
        await session.flush()
        session.add(DiscoveryFind(
            discovery_source_id=source.id, finding_type="post", external_id="l1",
            title="LI test", url="https://x/1", status="auto_indexed",
            decided_at=datetime.now(timezone.utc), newsletter_pending=True,
        ))
        session.add(Subscriber(email="li@example.com", name="L", source_role="student", topics="general"))
        await session.commit()
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session)
        issue_id = result.issue.id
    async with maker() as session:
        await newsletter_module.approve_and_distribute(session, issue_id, approved_by="t")
    async with maker() as session:
        deliveries = (await session.execute(select(NewsletterDelivery))).scalars().all()
        linkedin = [d for d in deliveries if d.channel == "linkedin"]
        assert len(linkedin) == 1
        assert linkedin[0].status == "skipped"
