"""Phase 3: newsletter composer + admin approval + distribution.

Covers:
- compose_pending_issue snapshots newsletter_pending finds into an issue,
  filters out dismissed, skips cycle when nothing is pending (unless force)
- update_editorial saves intro + subject; rejects edits to sent/approved
- approve_and_distribute marks issue sent, creates NewsletterDelivery rows,
  flips newsletter_pending=False on every included find
- /admin/api/newsletter/{...} endpoints honor the admin token

External email sending is mocked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import gestaltworkframe.api.admin_discovery as api_admin_discovery
import gestaltworkframe.api.main as api_main
from gestaltworkframe.core import newsletter as newsletter_module
from gestaltworkframe.core.db import (
    DiscoveryFind,
    DiscoverySource,
    NewsletterDelivery,
    NewsletterIssue,
    Subscriber,
)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nl.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(init())

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    # Mock the M365 Graph send so we never hit the network in tests.
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    return TestClient(api_main.app), engine, maker


def _seed_pending_finds(maker, count: int = 3, dismissed_count: int = 1) -> tuple[str, list[str]]:
    """Insert one source + N pending finds. Returns (source_id, pending_find_ids)."""
    async def seed() -> tuple[str, list[str]]:
        async with maker() as session:
            source = DiscoverySource(name="nl_src", watch_type="github_repo_watch", target="o/r", active=True)
            session.add(source)
            await session.flush()
            ids: list[str] = []
            for i in range(count):
                find = DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="release",
                    external_id=f"r{i}",
                    title=f"Pending release {i}",
                    url=f"https://example.com/r/{i}",
                    summary_text="Body",
                    status="auto_indexed",
                    decided_at=datetime.now(timezone.utc),
                    newsletter_pending=True,
                )
                session.add(find)
                await session.flush()
                ids.append(find.id)
            for i in range(dismissed_count):
                session.add(DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="release",
                    external_id=f"d{i}",
                    title=f"Dismissed pending {i}",
                    url=f"https://example.com/d/{i}",
                    status="auto_indexed",
                    decided_at=datetime.now(timezone.utc),
                    newsletter_pending=True,
                    dismissed=True,
                ))
            await session.commit()
            return source.id, ids
    return asyncio.run(seed())


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compose_snapshots_pending_finds_and_excludes_dismissed(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'composer.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        source = DiscoverySource(name="src", watch_type="rss_watch", target="https://x", active=True)
        session.add(source)
        await session.flush()
        for i in range(3):
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post", external_id=f"e{i}",
                title=f"Post {i}", url=f"https://x/{i}", status="auto_indexed",
                decided_at=datetime.now(timezone.utc), newsletter_pending=True,
            ))
        session.add(DiscoveryFind(
            discovery_source_id=source.id, finding_type="post", external_id="dismissed",
            title="Dismissed", url="https://x/dismissed", status="auto_indexed",
            decided_at=datetime.now(timezone.utc), newsletter_pending=True, dismissed=True,
        ))
        await session.commit()

    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session)
    assert result.created is True
    assert result.issue.status == "awaiting_approval"
    snapshot = json.loads(result.issue.finds_json)
    assert len(snapshot) == 3
    titles = {f["title"] for f in snapshot}
    assert "Dismissed" not in titles


@pytest.mark.asyncio
async def test_compose_skips_cycle_when_no_pending_finds(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session)
    assert result.created is False
    assert result.issue.status == "skipped"


@pytest.mark.asyncio
async def test_compose_force_creates_editorial_only_issue(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'force.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session, force=True)
    assert result.created is True
    assert result.issue.status == "awaiting_approval"
    assert json.loads(result.issue.finds_json) == []


# ---------------------------------------------------------------------------
# Editorial update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_editorial_saves_intro_and_subject(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session, force=True)
        issue_id = result.issue.id
    async with maker() as session:
        updated = await newsletter_module.update_editorial(
            session,
            issue_id,
            editorial_markdown="Hello **world**",
            subject="Custom subject",
        )
    assert "Hello **world**" in updated.editorial_markdown
    assert updated.subject == "Custom subject"


@pytest.mark.asyncio
async def test_update_editorial_rejected_after_send(tmp_path, monkeypatch):
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sent.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session, force=True)
        issue_id = result.issue.id
    async with maker() as session:
        await newsletter_module.approve_and_distribute(session, issue_id, approved_by="test")
    async with maker() as session:
        with pytest.raises(ValueError):
            await newsletter_module.update_editorial(session, issue_id, editorial_markdown="too late")


# ---------------------------------------------------------------------------
# Approve + distribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_distributes_and_clears_newsletter_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dist.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    # Seed a source, a pending find, and an active subscriber.
    async with maker() as session:
        source = DiscoverySource(name="dist_src", watch_type="rss_watch", target="https://x", active=True)
        session.add(source)
        await session.flush()
        find = DiscoveryFind(
            discovery_source_id=source.id, finding_type="post", external_id="d1",
            title="Distributable", url="https://x/1", status="auto_indexed",
            decided_at=datetime.now(timezone.utc), newsletter_pending=True,
        )
        session.add(find)
        sub = Subscriber(email="sub@example.com", name="Sub", source_role="student", topics="general")
        session.add(sub)
        await session.commit()
        await session.refresh(find)
        find_id = find.id

    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session)
        issue_id = result.issue.id
    async with maker() as session:
        sent_issue = await newsletter_module.approve_and_distribute(session, issue_id, approved_by="tester")

    assert sent_issue.status == "sent"
    assert sent_issue.sent_at is not None

    async with maker() as session:
        find_after = (await session.execute(select(DiscoveryFind).where(DiscoveryFind.id == find_id))).scalar_one()
        assert find_after.newsletter_pending is False
        deliveries = (await session.execute(select(NewsletterDelivery))).scalars().all()
        # One email delivery + one web delivery row.
        email_deliveries = [d for d in deliveries if d.channel == "email"]
        web_deliveries = [d for d in deliveries if d.channel == "web"]
        assert len(email_deliveries) == 1
        assert len(web_deliveries) == 1
        assert email_deliveries[0].status == "sent"


@pytest.mark.asyncio
async def test_approve_skips_double_send(tmp_path, monkeypatch):
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dbl.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session, force=True)
        issue_id = result.issue.id
    async with maker() as session:
        await newsletter_module.approve_and_distribute(session, issue_id, approved_by="t")
    async with maker() as session:
        with pytest.raises(ValueError):
            await newsletter_module.approve_and_distribute(session, issue_id, approved_by="t")


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def test_render_html_contains_subject_editorial_and_unsubscribe_url(tmp_path):
    issue = NewsletterIssue(
        slug="x",
        subject="Test subject",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown="**Bold intro** to the cycle.",
        finds_json=json.dumps([{"title": "Find A", "url": "https://example.com/a", "summary_text": "S", "source_name": "src", "display_source_name": "Src"}]),
        status="approved",
    )
    html = newsletter_module.render_issue_html(issue, unsubscribe_url="https://example.com/newsletter/unsubscribe?token=xyz")
    assert "Test subject" in html
    assert "Bold intro" in html
    assert "https://example.com/a" in html
    assert "https://example.com/newsletter/unsubscribe?token=xyz" in html
    # Brand voice rule: no em dashes in the template.
    assert "—" not in html
    assert "—" not in html


def test_render_plain_is_email_safe():
    issue = NewsletterIssue(
        slug="x", subject="Subj",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown="Intro",
        finds_json=json.dumps([{"title": "Find A", "url": "https://example.com/a", "summary_text": "S"}]),
        status="approved",
    )
    plain = newsletter_module.render_issue_plain(issue, unsubscribe_url="https://example.com/unsub")
    assert "Subj" in plain
    assert "Intro" in plain
    assert "- Find A" in plain
    assert "https://example.com/a" in plain
    assert "https://example.com/unsub" in plain


def test_render_linkedin_omits_html_and_includes_archive_link():
    issue = NewsletterIssue(
        slug="x", subject="Subj",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown="Intro paragraph.",
        finds_json=json.dumps([{"title": "Find A", "url": "https://example.com/a", "display_source_name": "Source X"}]),
        status="approved",
    )
    post = newsletter_module.render_issue_linkedin(issue)
    assert "<" not in post  # no html
    assert "Source X" in post
    assert "https://example.com/a" in post
    assert "/library/latest" in post


# ---------------------------------------------------------------------------
# Admin API endpoints
# ---------------------------------------------------------------------------


def test_admin_newsletter_endpoints_require_token(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        assert client.get("/admin/api/newsletter/issues").status_code == 401
        assert client.post("/admin/api/newsletter/draft", json={}).status_code == 401
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_newsletter_draft_compose_returns_issue(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        # Seed a pending find so compose has material.
        async def seed():
            async with maker() as session:
                source = DiscoverySource(name="endpoint_src", watch_type="rss_watch", target="https://x", active=True)
                session.add(source)
                await session.flush()
                session.add(DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post", external_id="e1",
                    title="Endpoint test", url="https://x/1", status="auto_indexed",
                    decided_at=datetime.now(timezone.utc), newsletter_pending=True,
                ))
                await session.commit()
        asyncio.run(seed())

        response = client.post(
            "/admin/api/newsletter/draft",
            json={"force": False},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["created"] is True
        assert body["issue"]["status"] == "awaiting_approval"
        assert body["issue"]["find_count"] == 1
        assert "html_preview" in body["issue"]
        assert "linkedin_post" in body["issue"]
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_admin_newsletter_editorial_and_approve_flow(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        # Seed pending find + subscriber so approve has both an input and an output.
        async def seed():
            async with maker() as session:
                source = DiscoverySource(name="flow_src", watch_type="rss_watch", target="https://x", active=True)
                session.add(source)
                await session.flush()
                session.add(DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post", external_id="f1",
                    title="Flow test", url="https://x/1", status="auto_indexed",
                    decided_at=datetime.now(timezone.utc), newsletter_pending=True,
                ))
                session.add(Subscriber(email="flow@example.com", name="F", source_role="student", topics="general"))
                await session.commit()
        asyncio.run(seed())

        draft = client.post("/admin/api/newsletter/draft", json={"force": False}, headers={"X-Admin-Token": "test-admin"})
        issue_id = draft.json()["issue"]["id"]

        # Save editorial
        ed_resp = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/editorial",
            json={"editorial_markdown": "Hi friends", "subject": "Renamed"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert ed_resp.status_code == 200
        assert ed_resp.json()["issue"]["subject"] == "Renamed"

        # Approve (Phase C: schedules send instead of distributing
        # immediately; status flips to "approved" with scheduled_send_at
        # set). The empty body opts into the default 30-minute delay.
        approve = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert approve.status_code == 200
        assert approve.json()["issue"]["status"] == "approved"
        assert approve.json()["issue"]["scheduled_send_at"] is not None

        # Second approve must fail with 409 — the issue is no longer in
        # the draft/awaiting_approval set the atomic UPDATE matches on.
        again = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert again.status_code == 409
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Phase 6 scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_tick_skips_new_draft_when_recent_cycle_exists(tmp_path, monkeypatch):
    """Daily tick: when the last sent cycle is younger than CYCLE_DAYS - 1,
    skip the auto-pacing pass. The reminder pass is unaffected."""
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched1.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        now = datetime.now(timezone.utc)
        recent = NewsletterIssue(
            slug="recent",
            period_start=now - timedelta(days=11),
            period_end=now - timedelta(days=1),
            status="sent",
            target_send_at=now - timedelta(days=1),
            sent_at=now - timedelta(days=1),
            created_at=now - timedelta(days=1),
        )
        session.add(recent)
        await session.commit()
    async with maker() as session:
        summary = await newsletter_module.run_scheduled_cycle(session)
    assert summary["action"] == "tick"
    assert summary["drafts_created"] == 0
    assert summary["draft_skipped_reason"] == "cycle_window_not_elapsed"


@pytest.mark.asyncio
async def test_scheduler_tick_skips_new_draft_when_upcoming_issue_exists(tmp_path, monkeypatch):
    """Daily tick: if an open draft / awaiting_approval issue already
    exists, don't pile another one on top."""
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched2.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        pending = NewsletterIssue(
            slug="pending",
            period_start=datetime.now(timezone.utc) - timedelta(days=10),
            period_end=datetime.now(timezone.utc),
            status="awaiting_approval",
        )
        session.add(pending)
        await session.commit()
    async with maker() as session:
        summary = await newsletter_module.run_scheduled_cycle(session)
    assert summary["action"] == "tick"
    assert summary["drafts_created"] == 0
    assert summary["draft_skipped_reason"] == "upcoming_issue_exists"
    assert "upcoming_issue_id" in summary


@pytest.mark.asyncio
async def test_scheduler_tick_auto_creates_draft_when_overdue(tmp_path, monkeypatch):
    """Daily tick: when no upcoming draft exists and the last sent
    cycle was more than CYCLE_DAYS - 1 ago, create a new empty issue
    anchored at last.target_send_at + 10 days and auto-tag any
    eligible auto_indexed finds onto it."""
    mock_send = AsyncMock(return_value="sent")
    monkeypatch.setattr(newsletter_module, "send_internal_email", mock_send)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched3.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        now = datetime.now(timezone.utc)
        source = DiscoverySource(name="sched_src", watch_type="rss_watch", target="https://x", active=True)
        session.add(source)
        await session.flush()
        # An eligible auto_indexed find that should be auto-tagged onto
        # the new draft.
        session.add(DiscoveryFind(
            discovery_source_id=source.id, finding_type="post", external_id="s1",
            title="Eligible item", url="https://x/1", status="auto_indexed",
            decided_at=now, first_seen_at=now,
        ))
        old = NewsletterIssue(
            slug="old", ship_number=1, display_label="1",
            period_start=now - timedelta(days=25),
            period_end=now - timedelta(days=15),
            status="sent",
            target_send_at=now - timedelta(days=15),
            sent_at=now - timedelta(days=15),
            created_at=now - timedelta(days=15),
        )
        session.add(old)
        await session.commit()
    async with maker() as session:
        summary = await newsletter_module.run_scheduled_cycle(session)
    assert summary["action"] == "tick"
    assert summary["drafts_created"] == 1
    assert summary["auto_tagged_finds"] == 1
    # ship=1 exists, no unsent labels in epoch 1, so new draft -> "1a"
    assert summary["new_issue_display_label"] == "1a"


@pytest.mark.asyncio
async def test_scheduler_tick_with_no_history_does_nothing(tmp_path, monkeypatch):
    """Daily tick on a fresh DB: no issues exist, no auto-pacing fires.
    Operator has to click + New issue to bootstrap the first cycle."""
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sched4.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        summary = await newsletter_module.run_scheduled_cycle(session)
    assert summary["action"] == "tick"
    # No prior sent issue and no overdue trigger -> auto-pacing
    # creates a first draft (so the operator gets the cycle bootstrapped
    # even on a fresh install). Confirms the new-install path.
    assert summary["drafts_created"] == 1
    # First draft on a fresh DB -> "0a"
    assert summary["new_issue_display_label"] == "0a"
