"""Tests for the public library feed endpoints not covered elsewhere:
/library/latest.json, /library/ticker.json, /library/issues.json, and
/library/issues/{slug}.json.

The /library/sources.* routes are covered in test_library_sources_feed.py.
These cover the latest/ticker find feeds and the public newsletter archive,
including the 404 paths that keep drafts and unpublished issues private.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gestaltworkframe.core.db.models  # noqa: F401  - register tables
from gestaltworkframe.api.library_feed import router as library_feed_router
from gestaltworkframe.core.db import get_session
from gestaltworkframe.core.db.models import DiscoveryFind, DiscoverySource, NewsletterIssue

_CACHE_HEADER = "public, max-age=300, stale-while-revalidate=1800"


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    app = FastAPI()
    app.include_router(library_feed_router)
    app.dependency_overrides[get_session] = override_session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, maker
    await engine.dispose()


async def _seed_finds(maker) -> None:
    async with maker() as session:
        now = datetime.now(timezone.utc)
        session.add(
            DiscoverySource(
                id="src-1", name="example/repo", watch_type="github_repo_watch",
                target="example/repo", active=True, featured=False,
            )
        )
        session.add_all([
            # In the public latest feed: approved status + recent decided_at.
            DiscoveryFind(
                id="find-latest", discovery_source_id="src-1", finding_type="release",
                external_id="rel-1", title="repo v1.0", url="https://example.test/1",
                status="auto_indexed", decided_at=now - timedelta(hours=1),
                first_seen_at=now - timedelta(hours=2), last_seen_at=now - timedelta(hours=1),
            ),
            # On the ticker: ticker_featured within the 30-day window.
            DiscoveryFind(
                id="find-ticker", discovery_source_id="src-1", finding_type="release",
                external_id="rel-2", title="repo v2.0", url="https://example.test/2",
                status="approved", ticker_featured=True,
                ticker_featured_at=now - timedelta(days=1),
                decided_at=now - timedelta(hours=1),
                first_seen_at=now - timedelta(hours=2), last_seen_at=now - timedelta(hours=1),
            ),
        ])
        await session.commit()


async def _add_issue(
    maker, *, slug: str, status: str, unpublished: bool = False, ship: int | None = None
) -> None:
    async with maker() as session:
        now = datetime.now(timezone.utc)
        session.add(
            NewsletterIssue(
                slug=slug,
                display_label=f"2026-06-01-{slug}",
                ship_number=ship,
                subject=f"Issue {slug}",
                period_start=now - timedelta(days=10),
                period_end=now,
                status=status,
                finds_json="[]",
                sent_at=now if status == "sent" else None,
                unpublished_at=now if unpublished else None,
            )
        )
        await session.commit()


# --- /library/latest.json ---------------------------------------------------

async def test_latest_feed_returns_finds_with_cache_header(client):
    ac, maker = client
    await _seed_finds(maker)
    resp = await ac.get("/library/latest.json")
    assert resp.status_code == 200
    body = resp.json()
    assert resp.headers["Cache-Control"] == _CACHE_HEADER
    assert body["title"] == "Updates and Additions"
    ids = {f["id"] for f in body["finds"]}
    assert "find-latest" in ids


async def test_latest_feed_clamps_query_params(client):
    ac, maker = client
    await _seed_finds(maker)
    resp = await ac.get("/library/latest.json?limit=1000&offset=-5&days=999")
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 100   # clamped to max
    assert body["offset"] == 0    # negative floored
    assert body["days"] == 365    # clamped to max


# --- /library/ticker.json ---------------------------------------------------

async def test_ticker_feed_returns_featured_finds(client):
    ac, maker = client
    await _seed_finds(maker)
    resp = await ac.get("/library/ticker.json?limit=999")
    assert resp.status_code == 200
    body = resp.json()
    assert resp.headers["Cache-Control"] == _CACHE_HEADER
    assert body["window_days"] == 30
    assert body["limit"] == 100  # clamped
    ids = {f["id"] for f in body["finds"]}
    assert "find-ticker" in ids
    assert "find-latest" not in ids  # not ticker_featured


# --- /library/issues.json ---------------------------------------------------

async def test_issues_feed_lists_sent_only(client):
    ac, maker = client
    await _add_issue(maker, slug="sent-one", status="sent")
    await _add_issue(maker, slug="draft-one", status="draft")
    resp = await ac.get("/library/issues.json")
    assert resp.status_code == 200
    body = resp.json()
    assert resp.headers["Cache-Control"] == _CACHE_HEADER
    slugs = {row["slug"] for row in body["issues"]}
    assert slugs == {"sent-one"}
    row = body["issues"][0]
    assert row["subject"] == "Issue sent-one"
    assert "find_count" in row and "sent_at" in row


# --- /library/issues/{slug}.json -------------------------------------------

async def test_issue_detail_public_returns_sent_issue(client):
    ac, maker = client
    await _add_issue(maker, slug="sent-detail", status="sent")
    resp = await ac.get("/library/issues/sent-detail.json")
    assert resp.status_code == 200
    issue = resp.json()["issue"]
    assert issue["slug"] == "sent-detail"
    assert issue["subject"] == "Issue sent-detail"
    assert "html" in issue and "finds" in issue


@pytest.mark.parametrize(
    "slug,status,unpublished",
    [
        ("draft-detail", "draft", False),     # drafts are private
        ("hidden-detail", "sent", True),      # unpublished is 404, not 200
    ],
)
async def test_issue_detail_public_404_paths(client, slug, status, unpublished):
    ac, maker = client
    await _add_issue(maker, slug=slug, status=status, unpublished=unpublished)
    resp = await ac.get(f"/library/issues/{slug}.json")
    assert resp.status_code == 404


async def test_issue_detail_public_404_on_unknown_slug(client):
    ac, _ = client
    resp = await ac.get("/library/issues/nope.json")
    assert resp.status_code == 404
