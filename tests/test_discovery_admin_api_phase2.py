"""Phase 2 curation split: ticker-feature, newsletter-queue, dismiss,
source drilldown with pagination + filters, and uncurated-counts.

Mirrors the test infrastructure in tests/test_discovery_admin_api.py
(shared TestClient + isolated SQLite engine per test). Lives in its own
file so the Phase 2 invariants are easy to find when something regresses.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import gestaltworkframe.api.admin_discovery as api_admin_discovery
import gestaltworkframe.api.main as api_main
from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource


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


def _seed_one_find(maker, status: str = "auto_indexed") -> str:
    async def seed() -> str:
        async with maker() as session:
            source = DiscoverySource(name="phase2_src", watch_type="github_repo_watch", target="x/y", active=True)
            session.add(source)
            await session.flush()
            find = DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="release",
                external_id="rel-1",
                title="Phase 2 test release",
                url="https://github.com/x/y/releases/tag/v1",
                summary_text="Body",
                status=status,
                decided_at=datetime.now(timezone.utc),
            )
            session.add(find)
            await session.commit()
            await session.refresh(find)
            return find.id
    return asyncio.run(seed())


def test_admin_discovery_ticker_feature_sets_flag_and_timestamp(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        find_id = _seed_one_find(maker)
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/ticker-feature",
            json={"featured": True, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        find = response.json()["find"]
        assert find["ticker_featured"] is True
        assert find["ticker_featured_at"] is not None
        # Legacy mirror kept in sync for existing consumers.
        assert find["featured"] is True

        # Unfeaturing clears both flag and timestamp.
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/ticker-feature",
            json={"featured": False, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        find = response.json()["find"]
        assert find["ticker_featured"] is False
        assert find["ticker_featured_at"] is None
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_newsletter_queue_sets_pending(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        find_id = _seed_one_find(maker)
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/newsletter-queue",
            json={"pending": True, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        assert response.json()["find"]["newsletter_pending"] is True
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_dismiss_clears_other_curation_flags(tmp_path, monkeypatch):
    """Dismiss is the I-reviewed-and-skipped action. It must also clear
    ticker_featured / newsletter_pending so the row exits public surfaces
    in one operation."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        find_id = _seed_one_find(maker)
        client.post(
            f"/admin/api/discovery/finds/{find_id}/ticker-feature",
            json={"featured": True, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        client.post(
            f"/admin/api/discovery/finds/{find_id}/newsletter-queue",
            json={"pending": True, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/dismiss",
            json={"dismissed": True, "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        find = response.json()["find"]
        assert find["dismissed"] is True
        assert find["ticker_featured"] is False
        assert find["newsletter_pending"] is False
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_source_drilldown_paginates_and_filters(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed_many() -> str:
            async with maker() as session:
                source = DiscoverySource(
                    name="bulk_src",
                    watch_type="rss_watch",
                    target="https://example.com/feed",
                    active=True,
                )
                session.add(source)
                await session.flush()
                for i in range(25):
                    title = f"Item about jinja {i}" if i % 3 == 0 else f"Item about webhook {i}"
                    session.add(
                        DiscoveryFind(
                            discovery_source_id=source.id,
                            finding_type="post",
                            external_id=f"e-{i}",
                            title=title,
                            url=f"https://example.com/{i}",
                            summary_text="",
                            status="auto_indexed",
                            decided_at=datetime.now(timezone.utc) - timedelta(days=i),
                        )
                    )
                await session.commit()
                return source.id
        source_id = asyncio.run(seed_many())

        response = client.get(
            f"/admin/api/discovery/sources/{source_id}/finds?page=1&page_size=10",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 25
        assert data["total_pages"] == 3
        assert len(data["finds"]) == 10
        assert data["page"] == 1

        response = client.get(
            f"/admin/api/discovery/sources/{source_id}/finds?topic=jinja&page_size=50",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        jinja = response.json()
        assert all("jinja" in f["title"].lower() for f in jinja["finds"])
        assert jinja["total"] < 25
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_uncurated_counts_excludes_curated_finds(tmp_path, monkeypatch):
    """The New content badge driver: only auto_indexed finds that are not
    yet ticker_featured / newsletter_pending / dismissed should count."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(
                    name="counts_src",
                    watch_type="github_repo_watch",
                    target="z/q",
                    active=True,
                )
                session.add(source)
                await session.flush()
                raw = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="release",
                    external_id="r1", title="raw", url="u1", status="auto_indexed",
                )
                feat = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="release",
                    external_id="r2", title="feat", url="u2", status="auto_indexed",
                    ticker_featured=True, ticker_featured_at=datetime.now(timezone.utc),
                )
                dism = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="release",
                    external_id="r3", title="dism", url="u3", status="auto_indexed",
                    dismissed=True,
                )
                session.add_all([raw, feat, dism])
                await session.commit()
                return source.id
        source_id = asyncio.run(seed())

        response = client.get(
            "/admin/api/discovery/uncurated-counts",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        counts = response.json()["counts"]
        assert counts.get(source_id) == 1
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_ticker_is_empty_when_nothing_is_ticker_featured(tmp_path, monkeypatch):
    """Three feature flags are independent: source.featured (Strong
    Signals), find.ticker_featured (this ticker), and
    find.published_in_newsletter_at (the Newsletter-Archive archive). The ticker
    reads ticker_featured only. Pre-curation it returns an empty list;
    the frontend renders nothing rather than dumping every auto_indexed
    row into the rail."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(name="fb_src", watch_type="rss_watch",
                                          target="https://example.com", active=True)
                session.add(source)
                await session.flush()
                now = datetime.now(timezone.utc)
                # Auto_indexed material exists, but none is ticker_featured.
                for i in range(3):
                    session.add(DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id=f"f{i}", title=f"Recent {i}", url=f"u{i}",
                        status="auto_indexed", decided_at=now - timedelta(days=i),
                    ))
                await session.commit()
        asyncio.run(seed())

        response = client.get("/library/ticker.json")
        assert response.status_code == 200
        assert response.json()["finds"] == []
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_ticker_rejected_status_drops_out_but_withdrawn_stays(tmp_path, monkeypatch):
    """Status governs the /library/latest discovery feed; ticker_featured
    governs the ticker. The two surfaces are orthogonal: a withdrawn
    item the operator pins to the ticker is exactly the evergreen-
    spotlight workflow the surface exists for. Only `rejected` is hard-
    excluded as trash."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(name="status_src", watch_type="rss_watch",
                                          target="https://example.com", active=True)
                session.add(source)
                await session.flush()
                now = datetime.now(timezone.utc)
                # Operator pinned all four to the ticker. The status field
                # is irrelevant except for 'rejected'.
                session.add_all([
                    DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id="s1", title="auto_indexed pinned", url="u1",
                        status="auto_indexed", decided_at=now,
                        ticker_featured=True, ticker_featured_at=now - timedelta(hours=1),
                    ),
                    DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id="s2", title="withdrawn pinned", url="u2",
                        status="withdrawn", decided_at=now,
                        ticker_featured=True, ticker_featured_at=now - timedelta(hours=2),
                    ),
                    DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id="s3", title="pending pinned", url="u3",
                        status="pending", decided_at=now,
                        ticker_featured=True, ticker_featured_at=now - timedelta(hours=3),
                    ),
                    DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id="s4", title="rejected pinned", url="u4",
                        status="rejected", decided_at=now,
                        ticker_featured=True, ticker_featured_at=now - timedelta(hours=4),
                    ),
                ])
                await session.commit()
        asyncio.run(seed())

        response = client.get("/library/ticker.json")
        titles = [f["title"] for f in response.json()["finds"]]
        assert titles == ["auto_indexed pinned", "withdrawn pinned", "pending pinned"]
        assert "rejected pinned" not in titles
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_ticker_filters_on_ticker_featured_within_window(tmp_path, monkeypatch):
    """The ticker reads from ticker_featured + ticker_featured_at and
    filters on the 30-day rolling window. ticker_featured=False rows,
    ticker_featured rows older than 30 days, and dismissed rows all
    drop out. The flag is independent of newsletter publication and
    source-level featured."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(
                    name="ticker_src",
                    watch_type="rss_watch",
                    target="https://example.com",
                    active=True,
                )
                session.add(source)
                await session.flush()
                now = datetime.now(timezone.utc)
                fresh = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="t1", title="fresh", url="u1", status="auto_indexed",
                    decided_at=now,
                    ticker_featured=True,
                    ticker_featured_at=now - timedelta(days=1),
                )
                stale = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="t2", title="stale", url="u2", status="auto_indexed",
                    decided_at=now,
                    ticker_featured=True,
                    ticker_featured_at=now - timedelta(days=45),
                )
                not_featured = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="t3", title="not_featured", url="u3", status="auto_indexed",
                    decided_at=now,
                )
                dismissed = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="t4", title="dismissed", url="u4", status="auto_indexed",
                    decided_at=now,
                    ticker_featured=True,
                    ticker_featured_at=now - timedelta(days=1),
                    dismissed=True,
                )
                session.add_all([fresh, stale, not_featured, dismissed])
                await session.commit()
        asyncio.run(seed())

        response = client.get("/library/ticker.json")
        assert response.status_code == 200
        titles = [f["title"] for f in response.json()["finds"]]
        assert titles == ["fresh"]
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_ticker_caps_at_ten_items_with_newest_first(tmp_path, monkeypatch):
    """Operator spec: 'should only live on the ticker for 30 days unless
    bumped off (max 10) by newer content.' Verify the cap with twelve
    ticker_featured rows."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(
                    name="bulk_ticker",
                    watch_type="rss_watch",
                    target="https://example.com",
                    active=True,
                )
                session.add(source)
                await session.flush()
                now = datetime.now(timezone.utc)
                for i in range(12):
                    session.add(DiscoveryFind(
                        discovery_source_id=source.id, finding_type="post",
                        external_id=f"p{i}", title=f"item-{i:02d}", url=f"u{i}",
                        status="auto_indexed", decided_at=now,
                        ticker_featured=True,
                        ticker_featured_at=now - timedelta(hours=i),
                    ))
                await session.commit()
        asyncio.run(seed())

        response = client.get("/library/ticker.json")
        titles = [f["title"] for f in response.json()["finds"]]
        # Newest first, capped at 10.
        assert len(titles) == 10
        assert titles[0] == "item-00"  # most recent
        assert titles[-1] == "item-09"  # 10th most recent
        assert "item-10" not in titles
        assert "item-11" not in titles
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
