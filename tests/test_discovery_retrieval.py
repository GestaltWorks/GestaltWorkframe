from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from core.db import DiscoveryFind, DiscoverySource
from core.discovery_document import discovery_find_to_document
from core.discovery_retrieval import approved_discovery_context


@pytest.mark.asyncio
async def test_approved_discovery_context_returns_latest_matching_finds(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'discovery.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("core.discovery_retrieval.async_session_maker", maker)
    now = datetime.now(timezone.utc)
    async with maker() as session:
        source = DiscoverySource(id="source-1", name="platform_blog", watch_type="rss_feed", target="https://example.com/feed.xml")
        session.add(source)
        session.add(
            DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="post",
                external_id="post-1",
                title="Latest Automation onboarding workflow",
                url="https://example.com/onboarding",
                summary_text="Fresh onboarding workflow signal.",
                status="approved",
                decided_at=now,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        await session.commit()

    context = await approved_discovery_context("what is latest in Automation onboarding")

    assert "Approved latest discovery context" in context
    assert "Latest Automation onboarding workflow" in context
    async with maker() as session:
        stored = next(iter((await session.execute(select(DiscoveryFind))).scalars()))
        assert stored is not None
        assert stored.canonical_document_json == ""
    await engine.dispose()


@pytest.mark.asyncio
async def test_approved_discovery_context_ignores_non_latest_queries():
    assert await approved_discovery_context("how do I build a workflow") == ""


def test_discovery_find_to_document_preserves_source_fields():
    now = datetime.now(timezone.utc)
    source = DiscoverySource(id="source-1", name="platform_blog", watch_type="rss_feed", target="https://example.com/feed.xml")
    find = DiscoveryFind(
        id="find-1",
        discovery_source_id=source.id,
        finding_type="post",
        external_id="post-1",
        title="Workflow bundle",
        url="https://example.com/bundle",
        summary_text="Bundle summary",
        status="approved",
        first_seen_at=now,
        last_seen_at=now,
    )
    document = discovery_find_to_document(find, source)
    assert document.doc_id == "discovery:find-1"
    assert document.source.source_url == "https://example.com/bundle"
    assert "Bundle summary" in document.body_text