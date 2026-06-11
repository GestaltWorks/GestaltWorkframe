"""Direct unit tests for core/discovery_queue.py curation/CRUD helpers.

discovery_queue was previously exercised only indirectly through the admin API
tests, leaving most of its read/curation functions uncovered. These tests drive
the helpers directly against an in-memory SQLite session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gestaltworkframe.core.db.models  # noqa: F401 - register tables
from gestaltworkframe.core.db.models import DiscoveryFind, DiscoverySource
from gestaltworkframe.core.discovery_queue import (
    count_uncurated_finds_per_source,
    decide_find,
    list_finds_for_source,
    list_pending_finds,
    list_recent_finds,
    list_source_health,
    set_find_dismissed,
    set_find_newsletter_pending,
    update_watched_source,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


def _source(**kw) -> DiscoverySource:
    return DiscoverySource(
        id=kw.get("id", "src-1"),
        name=kw.get("name", "example/repo"),
        watch_type=kw.get("watch_type", "github_repo_watch"),
        target=kw.get("target", "example/repo"),
        refresh_interval_seconds=kw.get("refresh_interval_seconds", 3600),
        importance_floor=kw.get("importance_floor", "normal"),
        active=kw.get("active", True),
        featured=kw.get("featured", False),
    )


def _find(**kw) -> DiscoveryFind:
    now = datetime.now(timezone.utc)
    return DiscoveryFind(
        id=kw["id"],
        discovery_source_id=kw.get("discovery_source_id", "src-1"),
        finding_type=kw.get("finding_type", "release"),
        external_id=kw.get("external_id", kw["id"]),
        title=kw.get("title", "Example finding"),
        url=kw.get("url", "https://example.test/x"),
        summary_text=kw.get("summary_text", "summary"),
        status=kw.get("status", "auto_indexed"),
        importance_signal=kw.get("importance_signal", "normal"),
        first_seen_at=kw.get("first_seen_at", now),
        last_seen_at=kw.get("last_seen_at", now),
        ticker_featured=kw.get("ticker_featured", False),
        newsletter_pending=kw.get("newsletter_pending", False),
        dismissed=kw.get("dismissed", False),
        featured=kw.get("featured", False),
    )


async def _seed(session_maker):
    async with session_maker() as session:
        session.add(_source(id="src-1", name="repo-one", target="example/repo-one"))
        session.add(_source(id="src-2", name="repo-two", target="example/repo-two", featured=True))
        session.add_all(
            [
                _find(id="pending-1", status="pending", finding_type="new_source_candidate", title="New repo candidate"),
                _find(id="auto-1", status="auto_indexed", title="Auto release one"),
                _find(id="auto-2", discovery_source_id="src-2", status="auto_indexed", title="Auto release two"),
                _find(id="approved-1", status="approved", reviewer="someone", title="Already approved"),
            ]
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Read / list helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_pending_finds_returns_only_pending(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        rows = await list_pending_finds(session)

    ids = {row["id"] for row in rows}
    assert ids == {"pending-1"}
    assert rows[0]["source_name"] == "repo-one"


@pytest.mark.asyncio
async def test_list_recent_finds_status_filter_and_reviewed_alias(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        auto = await list_recent_finds(session, status="auto_indexed")
        reviewed = await list_recent_finds(session, status="reviewed")
        everything = await list_recent_finds(session, include_activity=True)

    assert {r["id"] for r in auto} == {"auto-1", "auto-2"}
    # "reviewed" is an alias for approved/published/withdrawn.
    assert {r["id"] for r in reviewed} == {"approved-1"}
    assert {"pending-1", "auto-1", "auto-2", "approved-1"} <= {r["id"] for r in everything}


@pytest.mark.asyncio
async def test_list_source_health_lists_all_sources_sorted(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        health = await list_source_health(session)

    names = [s["name"] for s in health]
    assert names == sorted(names)
    assert {"repo-one", "repo-two"} <= set(names)


@pytest.mark.asyncio
async def test_count_uncurated_finds_per_source_excludes_curated(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        # Curate auto-2 (dismiss it) -> it drops out of the uncurated count.
        await set_find_dismissed(session, "auto-2", dismissed=True, reviewer="t")
        counts = await count_uncurated_finds_per_source(session)

    # auto-1 remains uncurated under src-1; auto-2 was dismissed.
    assert counts.get("src-1") == 1
    assert "src-2" not in counts


@pytest.mark.asyncio
async def test_list_finds_for_source_supports_topic_and_pagination(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        result = await list_finds_for_source(session, "src-1")
        filtered = await list_finds_for_source(session, "src-1", topic="candidate")
        paged = await list_finds_for_source(session, "src-1", page=1, page_size=1)

    assert result["total"] >= 2
    assert any(f["id"] == "auto-1" for f in result["finds"])
    # Topic substring matches the new_source_candidate title only.
    assert [f["id"] for f in filtered["finds"]] == ["pending-1"]
    # Pagination caps the page and reports total_pages over the full set.
    assert len(paged["finds"]) == 1
    assert paged["page_size"] == 1
    assert paged["total_pages"] == result["total"]


# ---------------------------------------------------------------------------
# decide_find
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decide_find_reject_sets_status(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        result = await decide_find(session, "pending-1", "reject", reviewer="curator", notes="not relevant")

    assert result["status"] == "rejected"
    assert result["reviewer"] == "curator"
    assert result["decision_notes"] == "not relevant"


@pytest.mark.asyncio
async def test_decide_find_approve_without_library_publish(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        result = await decide_find(
            session, "pending-1", "approve", reviewer="curator",
            publish_to_library=False, ingest_into_chroma=False,
        )

    assert result["status"] == "approved"
    assert result["published_to_library_repo"] is False


@pytest.mark.asyncio
async def test_decide_find_rejects_unknown_decision(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(ValueError, match="Unsupported decision"):
            await decide_find(session, "pending-1", "maybe", reviewer="curator")


@pytest.mark.asyncio
async def test_decide_find_missing_find_raises(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(LookupError):
            await decide_find(session, "does-not-exist", "approve", reviewer="curator")


# ---------------------------------------------------------------------------
# Curation toggles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_find_newsletter_pending_toggle_clears_dismissed(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        await set_find_dismissed(session, "auto-1", dismissed=True, reviewer="t")
        result = await set_find_newsletter_pending(session, "auto-1", pending=True, reviewer="t")

    assert result["newsletter_pending"] is True
    # Queueing for newsletter un-dismisses the find.
    assert result["dismissed"] is False


@pytest.mark.asyncio
async def test_set_find_dismissed_clears_public_flags(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        await set_find_newsletter_pending(session, "auto-1", pending=True, reviewer="t")
        result = await set_find_dismissed(session, "auto-1", dismissed=True, reviewer="t")

    assert result["dismissed"] is True
    assert result["newsletter_pending"] is False
    assert result["ticker_featured"] is False
    assert result["featured"] is False


@pytest.mark.asyncio
async def test_curation_toggles_missing_find_raises(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(LookupError):
            await set_find_dismissed(session, "nope", dismissed=True, reviewer="t")
        with pytest.raises(LookupError):
            await set_find_newsletter_pending(session, "nope", pending=True, reviewer="t")


# ---------------------------------------------------------------------------
# update_watched_source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_watched_source_updates_mutable_fields(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        result = await update_watched_source(
            session, "src-1", refresh_interval_seconds=7200, active=False,
            notes="paused for now", importance_floor="high",
        )

    assert result["refresh_interval_seconds"] == 7200
    assert result["active"] is False
    assert result["importance_floor"] == "high"
    assert result["notes"] == "paused for now"


@pytest.mark.asyncio
async def test_update_watched_source_missing_raises(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(LookupError):
            await update_watched_source(session, "ghost", active=False)


@pytest.mark.asyncio
async def test_update_watched_source_validates_interval_and_floor(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(ValueError, match="at least 300"):
            await update_watched_source(session, "src-1", refresh_interval_seconds=60)
        with pytest.raises(ValueError, match="importance_floor"):
            await update_watched_source(session, "src-1", importance_floor="urgent")
