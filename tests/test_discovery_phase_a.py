"""Phase A discovery redesign tests.

Verifies the new routing contract:
- Approved sources auto-index first-class events on poll (no operator gate).
- New-source-candidate finds remain in `pending` status for the approval gate.
- Per-file artifact noise lands in `source_activity` status, never goes to KB.
- Feature toggles flip the `featured` flag on sources and finds.
- The sources-with-activity rollup groups recent activity by source.
- Phase A.1: auto-ingest writes to BOTH the LIBRARY repo AND the Chroma
  index in one step. The two writes are independent and fail-soft.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import gestaltworkframe.core.db.models  # noqa: F401 - register tables
from gestaltworkframe.core.db.models import DiscoveryFind, DiscoverySource
from gestaltworkframe.core.discovery_handlers import FindCandidate
from gestaltworkframe.core.discovery_queue import (
    list_sources_with_activity,
    set_find_featured,
    set_source_featured,
)
from gestaltworkframe.core.discovery_scheduler import _initial_status_for, _is_routine_artifact_noise


def _candidate(**overrides):
    return FindCandidate(
        finding_type=overrides.get("finding_type", "release"),
        external_id=overrides.get("external_id", "ext-1"),
        title=overrides.get("title", "Example release v1.0"),
        url=overrides.get("url", "https://example.test/release/1"),
        summary_text=overrides.get("summary_text", "initial release"),
        raw_payload=overrides.get("raw_payload", {}),
        importance_signal=overrides.get("importance_signal", "normal"),
    )


def _source(**overrides) -> DiscoverySource:
    return DiscoverySource(
        id=overrides.get("id", "src-1"),
        name=overrides.get("name", "example_source"),
        watch_type=overrides.get("watch_type", "github_repo_watch"),
        target=overrides.get("target", "example/repo"),
        active=True,
    )


def test_release_from_tracked_repo_is_auto_indexed():
    """First-class events (releases) auto-ingest on poll."""
    source = _source(watch_type="github_repo_watch")
    candidate = _candidate(finding_type="release", title="example/repo — v2.0")
    assert _initial_status_for(candidate, source) == "auto_indexed"


def test_new_source_candidate_stays_pending():
    """new_source_candidate is the one remaining approval gate."""
    source = _source(watch_type="discovery_scout")
    candidate = _candidate(finding_type="new_source_candidate", title="MyCompany/new-repo")
    assert _initial_status_for(candidate, source) == "pending"


def test_artifact_file_diff_is_routine_source_activity():
    """Per-file diffs inside tracked repos roll up as source_activity, not approval items."""
    source = _source(watch_type="github_repo_artifact_scan", name="docs.example/repo_artifacts")
    candidate = _candidate(
        finding_type="artifact",
        title="Repository file artifact: docs/some-page.md",
        summary_text="Markdown file changed",
        importance_signal="normal",
    )
    assert _is_routine_artifact_noise(candidate, source) is True
    assert _initial_status_for(candidate, source) == "source_activity"


def test_artifact_bundle_json_is_first_class_event():
    """Bundle/schema/release-like artifact additions escape the noise filter."""
    source = _source(watch_type="github_repo_artifact_scan", name="docs.example/repo_artifacts")
    candidate = _candidate(
        finding_type="artifact",
        title="Repository artifact: workflows/new-onboarding.bundle.json",
        summary_text="New importable bundle",
        importance_signal="normal",
    )
    assert _is_routine_artifact_noise(candidate, source) is False
    assert _initial_status_for(candidate, source) == "auto_indexed"


def test_high_importance_artifact_escapes_noise_filter():
    """An artifact tagged high-importance never lands in source_activity."""
    source = _source(watch_type="github_repo_artifact_scan", name="docs.example/repo_artifacts")
    candidate = _candidate(
        finding_type="artifact",
        title="some readme tweak",
        summary_text="trivial doc churn",
        importance_signal="high",
    )
    assert _is_routine_artifact_noise(candidate, source) is False


def test_rss_post_from_blog_watcher_is_auto_indexed():
    """RSS posts from approved sources auto-index. They are not artifact noise."""
    source = _source(watch_type="rss_watch", name="automation_blog")
    candidate = _candidate(finding_type="rss_item", title="A new automation pattern")
    assert _is_routine_artifact_noise(candidate, source) is False
    assert _initial_status_for(candidate, source) == "auto_indexed"


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


async def _seed(session_maker):
    async with session_maker() as session:
        now = datetime.now(timezone.utc)
        src_a = DiscoverySource(id="src-a", name="example/repo-a", watch_type="github_repo_watch", target="example/repo-a", active=True, featured=False)
        src_b = DiscoverySource(id="src-b", name="example/repo-b", watch_type="github_repo_watch", target="example/repo-b", active=True, featured=True)
        session.add_all([src_a, src_b])

        session.add_all([
            DiscoveryFind(
                id="find-a-release",
                discovery_source_id="src-a",
                finding_type="release",
                external_id="rel-1",
                title="repo-a v1.0",
                url="https://example.test/a/1",
                status="auto_indexed",
                importance_signal="high",
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(days=1),
            ),
            DiscoveryFind(
                id="find-a-noise",
                discovery_source_id="src-a",
                finding_type="artifact",
                external_id="art-1",
                title="repo-a docs/page.md",
                url="https://example.test/a/page.md",
                status="source_activity",
                importance_signal="low",
                first_seen_at=now - timedelta(hours=12),
                last_seen_at=now - timedelta(hours=12),
            ),
            DiscoveryFind(
                id="find-b-release",
                discovery_source_id="src-b",
                finding_type="release",
                external_id="rel-2",
                title="repo-b v2.0",
                url="https://example.test/b/2",
                status="auto_indexed",
                featured=True,
                featured_at=now - timedelta(hours=2),
                importance_signal="high",
                first_seen_at=now - timedelta(hours=6),
                last_seen_at=now - timedelta(hours=6),
            ),
        ])
        await session.commit()


@pytest.mark.asyncio
async def test_sources_with_activity_groups_and_orders_correctly(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        rollup = await list_sources_with_activity(session, window_days=7)

    assert len(rollup) == 2
    # Featured source sorts first.
    assert rollup[0]["id"] == "src-b"
    assert rollup[0]["featured"] is True
    assert rollup[0]["featured_finds"] == 1
    assert rollup[0]["notable_finds"] == 1

    second = rollup[1]
    assert second["id"] == "src-a"
    assert second["total_finds"] == 2  # release + noise
    assert second["notable_finds"] == 1  # noise excluded from notable count
    # Recent finds list does not include source_activity entries.
    recent_ids = [item["id"] for item in second["recent_finds"]]
    assert "find-a-release" in recent_ids
    assert "find-a-noise" not in recent_ids


@pytest.mark.asyncio
async def test_set_find_featured_round_trip(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        # Feature the release on src-a (was not featured in seed).
        result = await set_find_featured(session, "find-a-release", featured=True, reviewer="tester")
        assert result["featured"] is True
        # Unfeature it.
        result = await set_find_featured(session, "find-a-release", featured=False, reviewer="tester")
        assert result["featured"] is False


@pytest.mark.asyncio
async def test_set_source_featured_round_trip(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        result = await set_source_featured(session, "src-a", featured=True, reviewer="tester")
        assert result["featured"] is True
        result = await set_source_featured(session, "src-a", featured=False, reviewer="tester")
        assert result["featured"] is False


@pytest.mark.asyncio
async def test_feature_toggle_404s_on_unknown_id(session_maker):
    await _seed(session_maker)
    async with session_maker() as session:
        with pytest.raises(LookupError):
            await set_find_featured(session, "nonexistent", featured=True, reviewer="tester")
        with pytest.raises(LookupError):
            await set_source_featured(session, "nonexistent", featured=True, reviewer="tester")


# ---------------------------------------------------------------------------
# Phase A.1: auto-ingest writes to both LIBRARY repo and Chroma.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_ingest_publishes_to_library_and_chroma_for_first_class_event(monkeypatch):
    """A release from an approved source should hit BOTH writes in one step."""
    from gestaltworkframe.core.discovery_scheduler import _auto_ingest_if_eligible
    from kb import library_publisher as cp_module
    from kb import discovery_ingest as di_module
    from kb.library_publisher import LibraryPublishResult

    publish_calls = []
    chroma_calls = []

    async def fake_publish(find, source, **kwargs):
        publish_calls.append((find.id, source.name))
        return LibraryPublishResult(
            public_url="https://github.com/test/library/blob/main/updates/example.md",
            commit_url="https://github.com/test/library/commit/abc",
            path="updates/example.md",
        )

    def fake_ingest(find, source):
        chroma_calls.append((find.id, source.name))

    monkeypatch.setattr(cp_module, "publish_find_to_library", fake_publish)
    monkeypatch.setattr(di_module, "ingest_approved_find_into_chroma", fake_ingest)

    source = DiscoverySource(id="src-z", name="example/release-repo", watch_type="github_repo_watch", target="example/release-repo", active=True)
    record = DiscoveryFind(
        id="find-z",
        discovery_source_id="src-z",
        finding_type="release",
        external_id="rel-z-1",
        title="example/release-repo v1.0",
        url="https://example.test/release/1",
        status="auto_indexed",
    )

    await _auto_ingest_if_eligible(record, source)

    assert publish_calls == [("find-z", "example/release-repo")]
    assert chroma_calls == [("find-z", "example/release-repo")]
    assert record.published_to_library_repo is True
    assert record.ingested_into_chroma is True
    assert record.library_target_path == "updates/example.md"
    assert record.library_promotion_error == ""


@pytest.mark.asyncio
async def test_auto_ingest_chroma_still_runs_when_library_unconfigured(monkeypatch):
    """If the LIBRARY publisher app creds are absent, Chroma still indexes the find."""
    from gestaltworkframe.core.discovery_scheduler import _auto_ingest_if_eligible
    from kb import library_publisher as cp_module
    from kb import discovery_ingest as di_module
    from kb.library_publisher import LibraryPublisherConfigError

    async def unconfigured(find, source, **kwargs):
        raise LibraryPublisherConfigError("LIBRARY publisher GitHub App not configured")

    chroma_calls = []
    def fake_ingest(find, source):
        chroma_calls.append(find.id)

    monkeypatch.setattr(cp_module, "publish_find_to_library", unconfigured)
    monkeypatch.setattr(di_module, "ingest_approved_find_into_chroma", fake_ingest)

    source = DiscoverySource(id="src-y", name="example/no-creds", watch_type="github_repo_watch", target="example/no-creds", active=True)
    record = DiscoveryFind(
        id="find-y",
        discovery_source_id="src-y",
        finding_type="release",
        external_id="rel-y-1",
        title="no-creds v1.0",
        url="https://example.test/no-creds/1",
        status="auto_indexed",
    )

    await _auto_ingest_if_eligible(record, source)

    assert chroma_calls == ["find-y"]
    assert record.ingested_into_chroma is True
    assert record.published_to_library_repo is False
    # Error type recorded so the operator can see why publish was skipped.
    assert record.library_promotion_error == "LibraryPublisherConfigError"


@pytest.mark.asyncio
async def test_auto_ingest_does_not_run_for_source_activity_status(monkeypatch):
    """source_activity (artifact noise) bypasses both writes."""
    from gestaltworkframe.core.discovery_scheduler import _auto_ingest_if_eligible
    from kb import library_publisher as cp_module
    from kb import discovery_ingest as di_module

    called = {"publish": False, "ingest": False}

    async def fake_publish(*args, **kwargs):
        called["publish"] = True

    def fake_ingest(*args, **kwargs):
        called["ingest"] = True

    monkeypatch.setattr(cp_module, "publish_find_to_library", fake_publish)
    monkeypatch.setattr(di_module, "ingest_approved_find_into_chroma", fake_ingest)

    source = DiscoverySource(id="src-x", name="example/noisy-repo", watch_type="github_repo_artifact_scan", target="example/noisy-repo", active=True)
    record = DiscoveryFind(
        id="find-x",
        discovery_source_id="src-x",
        finding_type="artifact",
        external_id="art-x-1",
        title="docs/page.md",
        url="https://example.test/page.md",
        status="source_activity",
    )

    await _auto_ingest_if_eligible(record, source)

    assert called["publish"] is False
    assert called["ingest"] is False
    assert record.published_to_library_repo is False
    assert record.ingested_into_chroma is False
