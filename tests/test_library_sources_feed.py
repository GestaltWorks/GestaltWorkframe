"""Tests for the public Phase C /library/sources endpoints.

Two routes: /library/sources.json (directory) and
/library/sources/{id}.json (detail). Both consume the same
list_sources_with_activity rollup as the admin surface, but the
public payload is filtered to remove admin-only fields.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import core.db.models  # noqa: F401  - register tables
from api.library_feed import router as library_feed_router
from core.db import get_session
from core.db.models import DiscoveryFind, DiscoverySource


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

    import httpx
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, maker
    await engine.dispose()


async def _seed(maker) -> None:
    async with maker() as session:
        now = datetime.now(timezone.utc)
        session.add_all([
            DiscoverySource(id="src-a", name="example/repo-a", watch_type="github_repo_watch", target="example/repo-a", active=True, featured=False),
            DiscoverySource(id="src-b", name="example/repo-b", watch_type="github_repo_watch", target="example/repo-b", active=True, featured=True),
            DiscoverySource(id="src-c", name="blog.example", watch_type="rss_watch", target="https://blog.example/feed", active=True, featured=False),
        ])
        session.add_all([
            DiscoveryFind(
                id="find-a-release",
                discovery_source_id="src-a",
                finding_type="release",
                external_id="rel-a-1",
                title="repo-a v1.0",
                url="https://example.test/a/1",
                status="auto_indexed",
                importance_signal="high",
                first_seen_at=now - timedelta(hours=2),
                last_seen_at=now - timedelta(hours=2),
            ),
            DiscoveryFind(
                id="find-b-release",
                discovery_source_id="src-b",
                finding_type="release",
                external_id="rel-b-1",
                title="repo-b v2.0",
                url="https://example.test/b/2",
                status="auto_indexed",
                featured=True,
                featured_at=now - timedelta(hours=1),
                importance_signal="high",
                first_seen_at=now - timedelta(hours=1),
                last_seen_at=now - timedelta(hours=1),
            ),
            DiscoveryFind(
                id="find-c-post",
                discovery_source_id="src-c",
                finding_type="rss_item",
                external_id="post-1",
                title="A new automation pattern",
                url="https://blog.example/post-1",
                status="auto_indexed",
                importance_signal="normal",
                first_seen_at=now - timedelta(hours=3),
                last_seen_at=now - timedelta(hours=3),
            ),
        ])
        await session.commit()


@pytest.mark.asyncio
async def test_library_sources_json_returns_all_active_sources(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources.json")
    assert response.status_code == 200
    body = response.json()
    assert "sources" in body
    names = [src["name"] for src in body["sources"]]
    assert "example/repo-a" in names
    assert "example/repo-b" in names
    assert "blog.example" in names
    # Featured sorts first.
    assert body["sources"][0]["name"] == "example/repo-b"
    assert body["sources"][0]["featured"] is True


@pytest.mark.asyncio
async def test_library_sources_json_filters_by_watch_type(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources.json?watch_type=rss_watch")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["name"] == "blog.example"


@pytest.mark.asyncio
async def test_library_sources_json_featured_only_returns_subset(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources.json?featured_only=true")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["name"] == "example/repo-b"


@pytest.mark.asyncio
async def test_library_source_detail_by_id(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources/src-b.json")
    assert response.status_code == 200
    body = response.json()
    assert body["source"]["name"] == "example/repo-b"
    assert body["source"]["featured"] is True
    titles = [item["title"] for item in body["source"]["recent_finds"]]
    assert "repo-b v2.0" in titles


@pytest.mark.asyncio
async def test_library_source_detail_by_name_fallback(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources/blog.example.json")
    assert response.status_code == 200
    body = response.json()
    assert body["source"]["watch_type"] == "rss_watch"


@pytest.mark.asyncio
async def test_library_source_detail_404_on_unknown(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources/does-not-exist.json")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_library_sources_public_payload_excludes_admin_fields(client):
    ac, maker = client
    await _seed(maker)

    response = await ac.get("/library/sources.json")
    assert response.status_code == 200
    body = response.json()
    public_keys = set(body["sources"][0].keys())
    # Admin-only fields (reviewer, decided_at, library_promotion_error, raw_payload)
    # must NOT leak through the public payload.
    forbidden = {"reviewer", "decided_at", "library_promotion_error", "raw_payload", "decision_notes"}
    assert public_keys.isdisjoint(forbidden), f"public payload leaked admin fields: {public_keys & forbidden}"
