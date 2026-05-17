from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from core.db import (
    DISCOVERY_AUDIT_EVENTS,
    DISCOVERY_AUDIT_SOURCE_ADDED,
    DISCOVERY_AUDIT_SOURCE_UPDATED,
    DiscoveryAudit,
    DiscoveryFind,
    DiscoverySource,
)
from core import discovery_handlers
from core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult
from core.discovery_scheduler import (
    reconcile_watchlist_seed,
    run_one_pass,
    select_due_sources,
)
from kb.watchlist import WatchedSource


def _seed_entry(
    name: str = "test_repo",
    watch_type: str = "github_repo_watch",
    target: str = "example/repo",
    refresh_cadence: str = "daily",
    active: bool = True,
) -> WatchedSource:
    return WatchedSource(
        name=name,
        watch_type=watch_type,
        target=target,
        description="test description",
        refresh_cadence=refresh_cadence,
        canonical_url="https://example.test",
        provenance="test provenance",
        license_notes="test license",
        attribution="test attribution",
        trust_tier="test_tier",
        display_policy="test_display",
        retrieval_policy="test_retrieval",
        curriculum_policy="test_curriculum",
        agent_access_policy="read_only",
        secret_handling="no_secrets",
        importance_floor="normal",
        active=active,
    )


async def _new_session(tmp_path) -> tuple[AsyncSession, sessionmaker]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'discovery.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, maker


@pytest.mark.asyncio
async def test_reconcile_creates_rows_for_new_seed_entries(tmp_path):
    engine, maker = await _new_session(tmp_path)
    async with maker() as session:
        created, updated = await reconcile_watchlist_seed(session, seed=[_seed_entry()])
        assert created == 1
        assert updated == 0

        result = await session.execute(select(DiscoverySource))
        sources = result.scalars().all()
        assert len(sources) == 1
        assert sources[0].name == "test_repo"
        assert sources[0].watch_type == "github_repo_watch"
        assert sources[0].refresh_interval_seconds == 86400
        assert sources[0].active is True

        audits = (await session.execute(select(DiscoveryAudit))).scalars().all()
        assert any(audit.event_type == DISCOVERY_AUDIT_SOURCE_ADDED for audit in audits)
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_updates_drift_and_records_audit(tmp_path):
    engine, maker = await _new_session(tmp_path)
    async with maker() as session:
        await reconcile_watchlist_seed(session, seed=[_seed_entry()])
        created, updated = await reconcile_watchlist_seed(
            session,
            seed=[_seed_entry(target="example/new-repo", refresh_cadence="hourly", active=False)],
        )
        assert created == 0
        assert updated == 1

        sources = (await session.execute(select(DiscoverySource))).scalars().all()
        assert sources[0].target == "example/new-repo"
        assert sources[0].refresh_interval_seconds == 3600
        assert sources[0].active is False

        events = [
            audit.event_type
            for audit in (await session.execute(select(DiscoveryAudit))).scalars().all()
        ]
        assert DISCOVERY_AUDIT_SOURCE_UPDATED in events
    await engine.dispose()


def test_discovery_audit_event_set_documents_supported_events():
    assert DISCOVERY_AUDIT_EVENTS == {
        "source_added",
        "source_updated",
        "poll_started",
        "poll_succeeded",
        "poll_failed",
        "find_seen",
        "find_decision",
        "library_promoted",
        "find_unpublished",
        "library_unpublished",
        "kb_purged",
    }


@pytest.mark.asyncio
async def test_select_due_sources_filters_by_cadence(tmp_path):
    engine, maker = await _new_session(tmp_path)
    now = datetime.now(timezone.utc)
    async with maker() as session:
        await reconcile_watchlist_seed(
            session,
            seed=[
                _seed_entry(name="due_never_polled"),
                _seed_entry(name="due_old_poll", target="example/two"),
                _seed_entry(name="not_due_fresh_poll", target="example/three"),
            ],
        )

        rows = (await session.execute(select(DiscoverySource))).scalars().all()
        by_name = {row.name: row for row in rows}
        by_name["due_old_poll"].last_polled_at = now - timedelta(days=2)
        by_name["not_due_fresh_poll"].last_polled_at = now - timedelta(minutes=5)
        await session.commit()

        due = await select_due_sources(session, now=now)
        due_names = {source.name for source in due}
        assert due_names == {"due_never_polled", "due_old_poll"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_pass_persists_finds_and_dedupes(tmp_path, monkeypatch):
    engine, maker = await _new_session(tmp_path)

    candidate = FindCandidate(
        finding_type="release",
        external_id="release:1",
        title="example/repo — v1.0",
        url="https://example.test/release/1",
        summary_text="initial release",
        raw_payload={"kind": "release", "tag": "v1.0"},
        importance_signal="high",
    )

    async def fake_handler(source: DiscoverySourceLike, _client: httpx.AsyncClient) -> PollResult:
        return PollResult(
            finds=[candidate],
            etag='"abc"',
            last_modified="Mon, 12 May 2026 12:00:00 GMT",
            status="ok",
        )

    # Override the registered handler for our test watch type.
    monkeypatch.setitem(discovery_handlers._HANDLERS, "github_repo_watch", fake_handler)

    async with maker() as session:
        report_first = await run_one_pass(session, seed=[_seed_entry()])
        assert report_first.sources_due == 1
        assert report_first.sources_polled == 1
        assert report_first.new_finds == 1
        assert report_first.sources_failed == 0

        finds = (await session.execute(select(DiscoveryFind))).scalars().all()
        assert len(finds) == 1
        # Phase A: first-class events (releases) auto-index on poll. They do
        # not sit in the pending approval queue. The only pending status now
        # is for new_source_candidate finds.
        assert finds[0].status == "auto_indexed"
        assert finds[0].importance_signal == "high"
        payload = json.loads(finds[0].raw_payload)
        assert payload["kind"] == "release"

        sources = (await session.execute(select(DiscoverySource))).scalars().all()
        assert sources[0].last_status == "ok"
        assert sources[0].etag == '"abc"'
        assert sources[0].last_polled_at is not None
        assert sources[0].consecutive_failures == 0

        # Re-run with the source still active; dedup must hold.
        sources[0].last_polled_at = datetime.now(timezone.utc) - timedelta(days=2)
        await session.commit()

        report_second = await run_one_pass(session, seed=[_seed_entry()])
        assert report_second.new_finds == 0
        assert report_second.repeat_finds == 1

        finds = (await session.execute(select(DiscoveryFind))).scalars().all()
        assert len(finds) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_pass_records_handler_errors(tmp_path, monkeypatch):
    engine, maker = await _new_session(tmp_path)

    async def failing_handler(*_args, **_kwargs) -> PollResult:
        raise RuntimeError("simulated outage")

    monkeypatch.setitem(discovery_handlers._HANDLERS, "github_repo_watch", failing_handler)

    async with maker() as session:
        report = await run_one_pass(session, seed=[_seed_entry()])
        assert report.sources_failed == 1
        assert report.sources_polled == 0
        assert report.new_finds == 0
        per_source = report.per_source[0]
        assert per_source.status == "error"
        assert "simulated outage" in per_source.error

        sources = (await session.execute(select(DiscoverySource))).scalars().all()
        assert sources[0].last_status == "error"
        assert "simulated outage" in sources[0].last_error
        assert sources[0].consecutive_failures == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_pass_contains_result_persistence_errors(tmp_path, monkeypatch):
    engine, maker = await _new_session(tmp_path)

    async def handler(_source: DiscoverySourceLike, _client: httpx.AsyncClient) -> PollResult:
        return PollResult(
            finds=[
                FindCandidate(
                    finding_type="release",
                    external_id="release:bad",
                    title="bad result",
                    url="https://example.test/bad",
                    summary_text="bad result",
                    raw_payload={},
                )
            ]
        )

    async def broken_persist(*_args, **_kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setitem(discovery_handlers._HANDLERS, "github_repo_watch", handler)
    monkeypatch.setattr("core.discovery_scheduler._persist_finds", broken_persist)

    async with maker() as session:
        report = await run_one_pass(session, seed=[_seed_entry()])
        assert report.sources_failed == 1
        assert report.sources_polled == 0
        assert "simulated persistence failure" in report.per_source[0].error

        sources = (await session.execute(select(DiscoverySource))).scalars().all()
        assert sources[0].last_status == "error"
        assert sources[0].consecutive_failures == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_select_due_sources_handles_sqlite_naive_datetimes(tmp_path):
    engine, maker = await _new_session(tmp_path)
    async with maker() as session:
        source = DiscoverySource(
            name="naive_datetime_source",
            watch_type="github_repo_watch",
            target="example/repo",
            refresh_interval_seconds=60,
            last_polled_at=datetime(2026, 5, 13, 12, 0, 0),
        )
        session.add(source)
        await session.commit()

        due = await select_due_sources(
            session,
            now=datetime(2026, 5, 13, 12, 2, 0, tzinfo=timezone.utc),
        )
        assert [row.name for row in due] == ["naive_datetime_source"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_pass_polls_due_sources_with_bounded_concurrency(tmp_path, monkeypatch):
    engine, maker = await _new_session(tmp_path)
    active = 0
    max_active = 0

    async def slow_handler(source: DiscoverySourceLike, _client: httpx.AsyncClient) -> PollResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return PollResult(
            finds=[
                FindCandidate(
                    finding_type="test",
                    external_id=f"find:{source.name}",
                    title=source.name,
                    url=f"https://example.test/{source.name}",
                    summary_text="test finding",
                    raw_payload={"source": source.name},
                )
            ]
        )

    monkeypatch.setitem(discovery_handlers._HANDLERS, "github_repo_watch", slow_handler)

    async with maker() as session:
        report = await run_one_pass(
            session,
            seed=[
                _seed_entry(name="one", target="example/one"),
                _seed_entry(name="two", target="example/two"),
                _seed_entry(name="three", target="example/three"),
            ],
            poll_concurrency=2,
        )

        assert report.sources_due == 3
        assert report.sources_polled == 3
        assert report.new_finds == 3
        assert max_active == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_one_pass_times_out_slow_sources(tmp_path, monkeypatch):
    engine, maker = await _new_session(tmp_path)

    async def hanging_handler(*_args, **_kwargs) -> PollResult:
        await asyncio.sleep(1)
        return PollResult(finds=[])

    monkeypatch.setitem(discovery_handlers._HANDLERS, "github_repo_watch", hanging_handler)

    async with maker() as session:
        report = await run_one_pass(
            session,
            seed=[_seed_entry()],
            per_source_timeout_seconds=0.01,
        )
        assert report.sources_failed == 1
        assert report.sources_polled == 0
        assert "timed out" in report.per_source[0].error

        sources = (await session.execute(select(DiscoverySource))).scalars().all()
        assert sources[0].last_status == "error"
        assert "timed out" in sources[0].last_error
    await engine.dispose()
