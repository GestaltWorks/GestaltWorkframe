from __future__ import annotations

import json
import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import api.admin_discovery as api_admin_discovery
import api.main as api_main
from gestaltworkframe.core.db import DiscoveryAudit, DiscoveryFind, DiscoverySource
from kb.library_publisher import LibraryPublisherError, LibraryPublishResult


def _source_body(name: str = "manual_source") -> dict[str, object]:
    return {
        "name": name,
        "watch_type": "github_repo_watch",
        "target": "example/repo",
        "description": "Manual test source",
        "refresh_cadence": "daily",
        "canonical_url": "https://github.com/example/repo",
        "provenance": "Public GitHub repository reviewed for testing.",
        "license_notes": "Link and summarize only.",
        "attribution": "Example owner",
        "trust_tier": "test",
        "display_policy": "public_after_source_review",
        "retrieval_policy": "approved_for_grounded_retrieval_after_review",
        "curriculum_policy": "not_approved_by_default",
        "agent_access_policy": "read_only",
        "secret_handling": "no_secrets",
        "importance_floor": "normal",
        "active": True,
        "notes": "operator note",
    }


async def _override_session(maker) -> AsyncGenerator[AsyncSession, None]:
    async with maker() as session:
        yield session


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import asyncio

    maker = asyncio.run(init())
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    return TestClient(api_main.app), engine, maker


def test_admin_discovery_source_create_and_patch(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/discovery/sources",
            json=_source_body(),
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        source = response.json()["source"]
        assert source["name"] == "manual_source"
        assert source["notes"] == "operator note"

        patch = client.patch(
            f"/admin/api/discovery/sources/{source['id']}",
            json={"refresh_cadence": "weekly", "active": False, "notes": "paused"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert patch.status_code == 200
        updated = patch.json()["source"]
        assert updated["refresh_interval_seconds"] == 604800
        assert updated["active"] is False
        assert updated["notes"] == "paused"
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_finds_returns_grouped_summary(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(name="example/repo", watch_type="github_repo_watch", target="example/repo")
                session.add(source)
                await session.flush()
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="release",
                        external_id="release:1",
                        title="Automation workflow bundle release v2",
                        url="https://github.com/example/repo/releases/2",
                        summary_text="Major changelog for importable automation templates",
                        importance_signal="high",
                    )
                )
                await session.commit()

        asyncio.run(seed())
        response = client.get(
            "/admin/api/discovery/finds?status=pending&limit=25",
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["finds"][0]["review_topic"] == "Releases and major updates"
        assert body["finds"][0]["newsletter_candidate"] is True
        assert body["summary"]["suggested_posts"][0]["title"] == "Automation workflow bundle release v2"
        assert body["summary"]["topic_groups"][0]["newsletter_candidates"] == 1
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_finds_hide_routine_source_activity_by_default(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(name="example/repo", watch_type="github_repo_artifact_scan", target="example/repo")
                session.add(source)
                await session.flush()
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="artifact",
                        external_id="readme:update",
                        title="Example commit abc123: Update README.md",
                        url="https://github.com/example/repo/commit/abc123",
                        summary_text="Update README.md",
                        importance_signal="low",
                    )
                )
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="release",
                        external_id="release:2",
                        title="Example release v2 with new workflow bundles",
                        url="https://github.com/example/repo/releases/2",
                        summary_text="Major release with importable workflow bundle updates",
                        importance_signal="high",
                    )
                )
                await session.commit()

        asyncio.run(seed())
        response = client.get(
            "/admin/api/discovery/finds?status=pending&limit=25",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        assert [find["title"] for find in response.json()["finds"]] == ["Example release v2 with new workflow bundles"]

        activity = client.get(
            "/admin/api/discovery/finds?status=pending&limit=25&include_activity=true",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert activity.status_code == 200
        assert {find["title"] for find in activity.json()["finds"]} == {"Example commit abc123: Update README.md", "Example release v2 with new workflow bundles"}
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_finds_caps_limit(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.get(
            "/admin/api/discovery/finds?limit=999",
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 422
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_run_once_rate_limits_multi_trigger(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)

    class FakeReport:
        def to_dict(self) -> dict[str, object]:
            return {"sources_due": 0, "sources_polled": 0, "new_finds": 0}

    async def fake_run_one_pass(_session: AsyncSession) -> FakeReport:
        return FakeReport()

    monkeypatch.setattr(api_admin_discovery, "DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(api_admin_discovery, "run_one_pass", fake_run_one_pass)
    try:
        first = client.post(
            "/admin/api/discovery/run-once",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert first.status_code == 200

        second = client.post(
            "/admin/api/discovery/run-once",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert second.status_code == 429
        retry_after = int(second.headers["Retry-After"])
        assert 1 <= retry_after <= 60
    finally:
        api_main.app.dependency_overrides.clear()
        api_admin_discovery._discovery_run_once_last_started_at = 0.0
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_approve_and_reject_endpoints(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    ingested = []
    published = []
    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.ingest_approved_find_into_chroma", lambda find, source: ingested.append((find.id, source.name)))

    async def fake_publish(find, source, *, notes: str = "", target_path: str = ""):
        published.append((find.id, source.name, notes))
        return LibraryPublishResult(
            public_url="https://github.com/example-org/library-repo/blob/main/discovery/first.md",
            commit_url="https://github.com/example-org/library-repo/commit/abc",
            path="discovery/first.md",
        )

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.publish_find_to_library", fake_publish)
    try:
        import asyncio

        async def seed() -> tuple[str, str]:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                first = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:1",
                    title="First",
                    url="https://example.test/1",
                    raw_payload=json.dumps({"kind": "post"}),
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                second = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:2",
                    title="Second",
                    url="https://example.test/2",
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(first)
                session.add(second)
                await session.commit()
                return first.id, second.id

        first_id, second_id = asyncio.run(seed())
        approve = client.post(
            f"/admin/api/discovery/finds/{first_id}/approve",
            json={"notes": "useful", "reviewer": "tester", "ingest_into_chroma": True, "publish_to_library": True},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert approve.status_code == 200
        assert approve.json()["find"]["status"] == "approved"
        assert approve.json()["find"]["ingested_into_chroma"] is True
        assert approve.json()["find"]["published_to_library_repo"] is True
        assert ingested == [(first_id, "seed")]
        assert published == [(first_id, "seed", "useful")]

        reject = client.post(
            f"/admin/api/discovery/finds/{second_id}/reject",
            json={"notes": "noise", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert reject.status_code == 200
        assert reject.json()["find"]["status"] == "rejected"
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_approve_defaults_to_public_update_without_ingest_or_library_publish(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    published = []

    async def fake_publish(*args, **kwargs):
        published.append((args, kwargs))
        return LibraryPublishResult(public_url="https://example.test/file.md", commit_url="https://example.test/commit", path="file.md")

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.publish_find_to_library", fake_publish)
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(discovery_source_id=source.id, finding_type="post", external_id="post:default", title="Default", url="https://example.test/default")
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/approve",
            json={"notes": "publish update", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 200
        body = response.json()["find"]
        assert body["status"] == "approved"
        assert body["ingested_into_chroma"] is False
        assert body["published_to_library_repo"] is False
        assert published == []
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_latest_json_filters_to_recent_updates_and_hides_internal_scores(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> None:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                now = datetime.now(timezone.utc)
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="post",
                        external_id="post:recent",
                        title="Recent update",
                        url="https://example.test/recent",
                        status="approved",
                        decided_at=now,
                        last_seen_at=None,
                    )
                )
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="post",
                        external_id="post:old",
                        title="Old update",
                        url="https://example.test/old",
                        status="approved",
                        decided_at=now - timedelta(days=10),
                    )
                )
                session.add(
                    DiscoveryFind(
                        discovery_source_id=source.id,
                        finding_type="post",
                        external_id="post:null-decided",
                        title="Null decided update",
                        url="https://example.test/null-decided",
                        status="approved",
                        decided_at=None,
                    )
                )
                await session.commit()

        asyncio.run(seed())
        response = client.get("/library/latest.json?days=2&limit=20")

        assert response.status_code == 200
        body = response.json()
        assert body["days"] == 2
        assert [find["title"] for find in body["finds"]] == ["Recent update"]
        assert "last_seen_at" in body["finds"][0]
        assert "publish_score" not in body["finds"][0]
        assert "ingest_score" not in body["finds"][0]
        assert "event_kind" not in body["finds"][0]

        default_window = client.get("/library/latest.json?limit=20")
        assert default_window.status_code == 200
        assert default_window.json()["days"] == 15
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_promotes_source_candidate_to_watched_source(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="new_source_candidate",
                    external_id="scout:github_repo_watch:example/repo",
                    title="Scout proposal: Example Repo",
                    url="example/repo",
                    summary_text="Relevant public repo.",
                    raw_payload=json.dumps(
                        {
                            "kind": "new_source_candidate",
                            "proposal": {
                                "name": "Example Repo",
                                "watch_type": "github_repo_watch",
                                "target": "example/repo",
                                "reason": "Relevant public repo.",
                            },
                        }
                    ),
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/promote-source",
            json={"notes": "track it", "reviewer": "tester", "refresh_cadence": "daily", "add_artifact_scan": True},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["find"]["status"] == "approved"
        assert [source["watch_type"] for source in body["sources"]] == ["github_repo_watch", "github_repo_artifact_scan"]
        assert body["sources"][0]["target"] == "example/repo"
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_source_candidate_promotion_requires_pending(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="new_source_candidate",
                    external_id="scout:github_repo_watch:example/repo",
                    title="Scout proposal: Example Repo",
                    url="example/repo",
                    status="approved",
                    raw_payload=json.dumps({"proposal": {"name": "Example Repo", "watch_type": "github_repo_watch", "target": "example/repo"}}),
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/promote-source",
            json={"notes": "again", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 400
        assert "Only pending source candidates" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_approve_publish_failure_keeps_find_pending(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)

    async def broken_publish(*_args, **_kwargs):
        raise LibraryPublisherError("simulated publish failure with\nsecond line")

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.publish_find_to_library", broken_publish)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:publish-failure",
                    title="Publish failure",
                    url="https://example.test/failure",
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/approve",
            json={"notes": "publish", "reviewer": "tester", "publish_to_library": True},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 502

        async def load_status() -> tuple[str, str]:
            async with maker() as session:
                find = await session.get(DiscoveryFind, find_id)
                assert find is not None
                return find.status, find.library_promotion_error

        status, error = asyncio.run(load_status())
        assert status == "pending"
        assert error == "LibraryPublisherError: simulated publish failure with"
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_promotes_approved_find_to_library(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    promoted = []

    async def fake_publish(find, source, *, notes: str = "", target_path: str = ""):
        promoted.append((find.id, source.name, notes, target_path))
        return LibraryPublishResult(
            public_url="https://github.com/example-org/library-repo/blob/main/discovery/approved/library-candidate.md",
            commit_url="https://github.com/example-org/library-repo/commit/123",
            path=target_path or "discovery/approved/test.md",
        )

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.publish_find_to_library", fake_publish)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:library",
                    title="Library candidate",
                    url="https://example.test/library",
                    status="approved",
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/promote-library",
            json={"notes": "ship it", "reviewer": "tester", "target_path": "discovery/approved/library-candidate.md"},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 200
        body = response.json()["find"]
        assert body["published_to_library_repo"] is True
        assert body["library_file_url"].endswith("/discovery/approved/library-candidate.md")
        assert body["library_target_path"] == "discovery/approved/library-candidate.md"
        assert promoted == [(find_id, "seed", "ship it", "discovery/approved/library-candidate.md")]
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_unpublishes_latest_library_and_kb(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    deleted = []
    purged = []

    class DeleteResult:
        commit_url = "https://github.com/example-org/library-repo/commit/delete"
        path = "discovery/approved/bad.md"

    async def fake_delete_result(path: str, *, title: str = "discovery reference"):
        deleted.append((path, title))
        return DeleteResult()

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.delete_library_file", fake_delete_result)
    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.purge_discovery_find_from_chroma", lambda find_id: purged.append(find_id))
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:bad",
                    title="Bad approved item",
                    url="https://example.test/bad",
                    status="approved",
                    decided_at=datetime.now(timezone.utc),
                    ingested_into_chroma=True,
                    published_to_library_repo=True,
                    library_target_path="discovery/approved/bad.md",
                    library_file_url="https://github.com/example-org/library-repo/blob/main/discovery/approved/bad.md",
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        latest = client.post(
            f"/admin/api/discovery/finds/{find_id}/unpublish-latest",
            json={"notes": "bad signal", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert latest.status_code == 200
        assert latest.json()["find"]["status"] == "withdrawn"
        assert client.get("/library/latest.json?limit=20").json()["finds"] == []
        reviewed = client.get("/admin/api/discovery/finds?status=reviewed", headers={"X-Admin-Token": "test-admin"})
        assert [find["id"] for find in reviewed.json()["finds"]] == [find_id]

        library = client.post(
            f"/admin/api/discovery/finds/{find_id}/unpublish-library",
            json={"notes": "remove file", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert library.status_code == 200
        assert library.json()["find"]["published_to_library_repo"] is False
        assert library.json()["find"]["library_target_path"] == ""
        assert deleted == [("discovery/approved/bad.md", "Bad approved item")]

        kb = client.post(
            f"/admin/api/discovery/finds/{find_id}/purge-kb",
            json={"notes": "purge vector", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert kb.status_code == 200
        assert kb.json()["find"]["ingested_into_chroma"] is False
        assert purged == [find_id]
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_unpublish_latest_requires_public_find(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(discovery_source_id=source.id, finding_type="post", external_id="post:pending-unpublish", title="Pending", url="https://example.test/pending")
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/unpublish-latest",
            json={"notes": "not public", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 400
        assert "Only public" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_unpublish_latest_handles_published_and_withdrawn(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed(status: str) -> str:
            async with maker() as session:
                source = DiscoverySource(name=f"seed-{status}", watch_type="rss_feed", target=f"https://example.test/{status}.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(discovery_source_id=source.id, finding_type="post", external_id=f"post:{status}", title=f"{status} item", url=f"https://example.test/{status}", status=status, decided_at=datetime.now(timezone.utc))
                session.add(find)
                await session.commit()
                return find.id

        published_id = asyncio.run(seed("published"))
        published = client.post(
            f"/admin/api/discovery/finds/{published_id}/unpublish-latest",
            json={"notes": "published unpublish", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert published.status_code == 200
        assert published.json()["find"]["status"] == "withdrawn"

        withdrawn_id = asyncio.run(seed("withdrawn"))
        withdrawn = client.post(
            f"/admin/api/discovery/finds/{withdrawn_id}/unpublish-latest",
            json={"notes": "already withdrawn", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["find"]["status"] == "withdrawn"

        async def audit_count() -> int:
            async with maker() as session:
                return len((await session.execute(select(DiscoveryAudit).where(DiscoveryAudit.find_id == withdrawn_id))).scalars().all())

        assert asyncio.run(audit_count()) == 0
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_unpublish_library_failure_is_audited(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)

    async def broken_delete(*_args, **_kwargs):
        raise LibraryPublisherError("delete failed")

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.delete_library_file", broken_delete)
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:delete-fail",
                    title="Delete fail",
                    url="https://example.test/delete-fail",
                    status="approved",
                    published_to_library_repo=True,
                    library_target_path="discovery/approved/delete-fail.md",
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/unpublish-library",
            json={"notes": "remove file", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 502

        async def load_audit() -> tuple[bool, str, str]:
            async with maker() as session:
                find = await session.get(DiscoveryFind, find_id)
                audit = (await session.execute(select(DiscoveryAudit).where(DiscoveryAudit.find_id == find_id))).scalars().all()[-1]
                assert find is not None
                return find.published_to_library_repo, find.library_promotion_error, audit.after_state

        published, error, after_state = asyncio.run(load_audit())
        assert published is True
        assert error == "LibraryPublisherError: delete failed"
        assert after_state == "delete_failed:LibraryPublisherError: delete failed"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_purge_kb_failure_keeps_ingested_state(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)

    def broken_purge(_find_id: str) -> None:
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr("gestaltworkframe.core.discovery_queue.purge_discovery_find_from_chroma", broken_purge)
    try:
        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(discovery_source_id=source.id, finding_type="post", external_id="post:purge-fail", title="Purge fail", url="https://example.test/purge-fail", ingested_into_chroma=True)
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/purge-kb",
            json={"notes": "purge vector", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 502

        async def load_state() -> tuple[bool, str]:
            async with maker() as session:
                find = await session.get(DiscoveryFind, find_id)
                audit = (await session.execute(select(DiscoveryAudit).where(DiscoveryAudit.find_id == find_id))).scalars().all()[-1]
                assert find is not None
                return find.ingested_into_chroma, audit.after_state

        ingested, after_state = asyncio.run(load_state())
        assert ingested is True
        assert after_state == "purge_failed:RuntimeError: chroma unavailable"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_discovery_promote_library_requires_approved_find(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:pending",
                    title="Pending candidate",
                    url="https://example.test/pending",
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/promote-library",
            json={"notes": "too soon", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 400
        assert "Only approved" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_promote_library_reports_missing_publisher_token(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    monkeypatch.delenv("LIBRARY_PUBLISHER_GITHUB_TOKEN", raising=False)
    try:
        import asyncio

        async def seed() -> str:
            async with maker() as session:
                source = DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml")
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="post",
                    external_id="post:no-token",
                    title="Needs publisher",
                    url="https://example.test/no-token",
                    status="approved",
                    first_seen_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                )
                session.add(find)
                await session.commit()
                return find.id

        find_id = asyncio.run(seed())
        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/promote-library",
            json={"notes": "publish", "reviewer": "tester"},
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 503
        assert "publisher GitHub App is not configured" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_source_create_uses_watchlist_validation(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        body = _source_body("bad_source")
        body["watch_type"] = "unsupported_source_type"

        response = client.post(
            "/admin/api/discovery/sources",
            json=body,
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 422
        assert "unsupported watch_type" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())


def test_admin_discovery_source_create_rejects_ssrf_target(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        body = _source_body("ssrf_source")
        body["watch_type"] = "rss_feed"
        body["target"] = "http://localhost:8080/v1/models"

        response = client.post(
            "/admin/api/discovery/sources",
            json=body,
            headers={"X-Admin-Token": "test-admin"},
        )

        assert response.status_code == 422
        assert "must use https://" in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        import asyncio
        asyncio.run(engine.dispose())