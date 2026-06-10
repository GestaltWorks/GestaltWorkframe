"""Newsletter per-issue model helpers tests (ship-gated numbering).

Covers:

- next_display_label returns "0a" on an empty DB and increments to "0b"
- next_display_label resets to "{ship}a" after a shipped issue
- _label_letter base-26 sequence (a..z, aa, ab, ...)
- next_default_target_send_at anchors on last_sent.target_send_at + 10d
- create_empty_issue assigns a sticky display_label, leaves ship_number null
- assign_find_to_issue tags / untags, rejects sent-issue targets
- list_assignable_issues returns drafts + awaiting + approved-not-yet-sent
- auto_populate_draft tags eligible finds onto a new draft
- POST /admin/api/newsletter/issues/new returns the new display_label
- GET /admin/api/newsletter/assignable-issues returns the open set + suggested default
- delete_issue helper reverts finds + purges deliveries + deletes row
- unpublish_issue helper sets unpublished_at + handles each status
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import api.admin_discovery as api_admin_discovery
import api.main as api_main
from gestaltworkframe.core import newsletter as newsletter_module
from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource, NewsletterIssue
from gestaltworkframe.core.db.models import NewsletterDelivery


def _make_engine(tmp_path):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Pure helpers (no HTTP)
# ---------------------------------------------------------------------------


def test_label_letter_base26_sequence():
    assert newsletter_module._label_letter(0) == "a"
    assert newsletter_module._label_letter(1) == "b"
    assert newsletter_module._label_letter(25) == "z"
    assert newsletter_module._label_letter(26) == "aa"
    assert newsletter_module._label_letter(27) == "ab"
    assert newsletter_module._label_letter(51) == "az"
    assert newsletter_module._label_letter(52) == "ba"
    with pytest.raises(ValueError):
        newsletter_module._label_letter(-1)


def test_next_display_label_starts_at_0a(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            assert await newsletter_module.next_display_label(session) == "0a"
            session.add(NewsletterIssue(
                ship_number=None, display_label="0a", slug="i0a", subject="0a",
                period_start=datetime.now(timezone.utc) - timedelta(days=10),
                period_end=datetime.now(timezone.utc),
                status="draft",
            ))
            await session.commit()
            assert await newsletter_module.next_display_label(session) == "0b"

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_next_display_label_resets_after_ship(tmp_path):
    """After a shipped issue (ship_number=1), drafts get '1a', '1b'..."""
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-1", subject="Issue 1",
                period_start=now - timedelta(days=10), period_end=now,
                status="sent", sent_at=now,
            ))
            await session.commit()
            assert await newsletter_module.next_display_label(session) == "1a"
            # Stale 0a draft co-exists; ship_number=2 hasn't happened so
            # the new label anchors on max(ship_number)=1.
            session.add(NewsletterIssue(
                ship_number=None, display_label="0a", slug="stale-0a", subject="0a",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="skipped",
            ))
            await session.commit()
            # next_display_label still '1a' because there are no other
            # unsent labels in the {ship=1} epoch.
            assert await newsletter_module.next_display_label(session) == "1a"

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_next_display_label_does_not_cross_numeric_epoch_prefixes(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-1", subject="Issue 1",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="sent", sent_at=now - timedelta(days=10),
            ))
            session.add(NewsletterIssue(
                ship_number=None, display_label="12a", slug="future-epoch-draft", subject="12a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            session.add(NewsletterIssue(
                ship_number=None, display_label="100a", slug="hundred-epoch-draft", subject="100a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            await session.commit()

            assert await newsletter_module.next_display_label(session) == "1a"

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_create_empty_issue_retries_display_label_collision(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=None, display_label="0a", slug="existing", subject="existing",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            await session.commit()
            values = iter(["0a", "0b"])

            async def fake_next_label(_session):
                return next(values)

            monkeypatch.setattr(newsletter_module, "next_display_label", fake_next_label)

            issue = await newsletter_module.create_empty_issue(session, target_send_at=now)

            assert issue.display_label == "0b"
            assert issue.slug.startswith("issue-0b-")

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_create_empty_issue_assigns_label_no_ship_number(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            issue = await newsletter_module.create_empty_issue(session)
            assert issue.display_label == "0a"
            assert issue.ship_number is None
            assert issue.status == "draft"
            assert issue.target_send_at is not None
            # Default ~ today + 10 days when no prior sent issue exists.
            target = issue.target_send_at
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delta = target - datetime.now(timezone.utc)
            assert 9 * 86400 < delta.total_seconds() < 11 * 86400
            assert issue.subject == "Issue 0a"

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_assign_find_to_issue_tags_and_untags(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            find = DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="x", title="t", url="u", status="auto_indexed",
            )
            session.add(find)
            await session.commit()
            await session.refresh(find)
            issue = await newsletter_module.create_empty_issue(session)

            tagged = await newsletter_module.assign_find_to_issue(session, find.id, issue.id)
            assert tagged.newsletter_issue_id == issue.id
            assert tagged.newsletter_pending is True

            cleared = await newsletter_module.assign_find_to_issue(session, find.id, None)
            assert cleared.newsletter_issue_id is None
            assert cleared.newsletter_pending is False

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_assign_find_to_issue_rejects_sent_issue(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            find = DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="x", title="t", url="u", status="auto_indexed",
            )
            session.add(find)
            sent_issue = NewsletterIssue(
                ship_number=1, display_label="1", slug="sent", subject="Issue 1",
                period_start=datetime.now(timezone.utc) - timedelta(days=10),
                period_end=datetime.now(timezone.utc),
                status="sent",
            )
            session.add(sent_issue)
            await session.commit()
            await session.refresh(find)
            await session.refresh(sent_issue)

            with pytest.raises(ValueError):
                await newsletter_module.assign_find_to_issue(session, find.id, sent_issue.id)

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_auto_populate_draft_tags_eligible_finds(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            for i in range(3):
                session.add(DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id=f"ok{i}", title=f"ok {i}", url=f"u{i}",
                    status="auto_indexed", first_seen_at=now, decided_at=now,
                ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="dim", title="dim", url="u", status="auto_indexed",
                first_seen_at=now, decided_at=now, dismissed=True,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="pub", title="pub", url="u", status="auto_indexed",
                first_seen_at=now, decided_at=now,
                published_in_newsletter_at=now - timedelta(days=1),
            ))
            await session.commit()
            issue = await newsletter_module.create_empty_issue(session)
            tagged = await newsletter_module.auto_populate_draft(session, issue.id)
            assert tagged == 3
            live = await newsletter_module.live_finds_for_issue(session, issue.id)
            assert {f["title"] for f in live} == {"ok 0", "ok 1", "ok 2"}

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_delete_issue_reverts_finds_and_purges_deliveries(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            issue = NewsletterIssue(
                ship_number=1, display_label="1", slug="sent", subject="Issue 1",
                period_start=now - timedelta(days=10), period_end=now,
                status="sent", sent_at=now,
            )
            session.add(issue)
            await session.flush()
            for i in range(2):
                session.add(DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id=f"f{i}", title=f"f{i}", url=f"u{i}",
                    status="auto_indexed", first_seen_at=now,
                    newsletter_issue_id=issue.id, newsletter_pending=False,
                    published_in_newsletter_at=now,
                ))
            session.add(NewsletterDelivery(
                issue_id=issue.id, subscriber_id="s1", channel="email", status="sent",
            ))
            session.add(NewsletterDelivery(
                issue_id=issue.id, channel="web", status="sent",
            ))
            await session.commit()

            summary = await newsletter_module.delete_issue(
                session, issue.id, deleted_by="test",
            )
            assert summary["finds_reverted"] == 2
            assert summary["deliveries_purged"] == 2

            remaining = (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.id == issue.id)
            )).scalar_one_or_none()
            assert remaining is None

            reverted = (await session.execute(select(DiscoveryFind))).scalars().all()
            for f in reverted:
                assert f.newsletter_issue_id is None
                assert f.newsletter_pending is False
                assert f.published_in_newsletter_at is not None

            leftover_deliveries = (await session.execute(
                select(NewsletterDelivery).where(NewsletterDelivery.issue_id == issue.id)
            )).scalars().all()
            assert leftover_deliveries == []

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_delete_open_issue_clears_unsent_publication_state(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            issue = NewsletterIssue(
                ship_number=None, display_label="0a", slug="draft", subject="Issue 0a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            )
            session.add(issue)
            await session.flush()
            find = DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="f", title="f", url="u", status="auto_indexed",
                newsletter_issue_id=issue.id, newsletter_pending=True,
                published_in_newsletter_at=now,
            )
            session.add(find)
            await session.commit()

            await newsletter_module.delete_issue(session, issue.id, deleted_by="test")

            reverted = (await session.execute(select(DiscoveryFind))).scalar_one()
            assert reverted.newsletter_issue_id is None
            assert reverted.newsletter_pending is False
            assert reverted.published_in_newsletter_at is None

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_unpublish_issue_sets_unpublished_at_for_sent(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            issue = NewsletterIssue(
                ship_number=1, display_label="1", slug="sent", subject="Issue 1",
                period_start=now - timedelta(days=10), period_end=now,
                status="sent", sent_at=now,
            )
            session.add(issue)
            await session.commit()
            result = await newsletter_module.unpublish_issue(
                session, issue.id, unpublished_by="test",
            )
            assert result.unpublished_at is not None
            assert result.status == "sent"  # not transitioned

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_unpublish_issue_cancels_scheduled_send_for_approved(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            issue = NewsletterIssue(
                ship_number=None, display_label="0a", slug="approved",
                subject="0a",
                period_start=now - timedelta(days=10), period_end=now,
                status="approved",
                scheduled_send_at=now + timedelta(hours=1),
            )
            session.add(issue)
            await session.commit()
            result = await newsletter_module.unpublish_issue(
                session, issue.id, unpublished_by="test",
            )
            assert result.unpublished_at is not None
            assert result.status == "awaiting_approval"
            assert result.scheduled_send_at is None

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_unpublish_issue_is_idempotent(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            issue = NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-unpublished", subject="Issue 1",
                period_start=now - timedelta(days=10), period_end=now,
                status="sent", sent_at=now, unpublished_at=now - timedelta(hours=1),
            )
            session.add(issue)
            await session.commit()
            original = issue.unpublished_at

            result = await newsletter_module.unpublish_issue(session, issue.id, unpublished_by="test")

            assert result.unpublished_at == original

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_unpublish_issue_rejects_draft(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            issue = NewsletterIssue(
                ship_number=None, display_label="0a", slug="draft",
                subject="0a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            )
            session.add(issue)
            await session.commit()
            with pytest.raises(ValueError):
                await newsletter_module.unpublish_issue(
                    session, issue.id, unpublished_by="test",
                )

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_list_issues_public_filter_excludes_unpublished_and_drafts(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-visible",
                subject="Issue 1",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="sent", sent_at=now - timedelta(days=10),
            ))
            session.add(NewsletterIssue(
                ship_number=2, display_label="2", slug="sent-hidden",
                subject="Issue 2",
                period_start=now - timedelta(days=10), period_end=now,
                status="sent", sent_at=now - timedelta(days=1),
                unpublished_at=now,
            ))
            session.add(NewsletterIssue(
                ship_number=None, display_label="2a", slug="draft",
                subject="2a",
                period_start=now, period_end=now + timedelta(days=10),
                status="draft",
            ))
            await session.commit()

            admin = await newsletter_module.list_issues(session)
            assert len(admin) == 3  # admin sees everything

            public = await newsletter_module.list_issues(
                session, include_unpublished=False, public_only=True,
            )
            slugs = {row["slug"] for row in public}
            assert slugs == {"sent-visible"}

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_assignable_issues_excludes_approved_without_schedule(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            draft = NewsletterIssue(
                ship_number=None, display_label="0a", slug="draft", subject="draft",
                period_start=now - timedelta(days=10), period_end=now, status="draft",
            )
            approved_unscheduled = NewsletterIssue(
                ship_number=None, display_label="0b", slug="approved-open", subject="approved",
                period_start=now - timedelta(days=10), period_end=now, status="approved",
                scheduled_send_at=None,
            )
            approved_future = NewsletterIssue(
                ship_number=None, display_label="0c", slug="approved-future", subject="approved future",
                period_start=now - timedelta(days=10), period_end=now, status="approved",
                scheduled_send_at=now + timedelta(hours=1),
            )
            session.add_all([draft, approved_unscheduled, approved_future])
            await session.commit()

            issues = await newsletter_module.list_assignable_issues(session)

            assert {issue.slug for issue in issues} == {"draft", "approved-future"}

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_get_issue_detail_preview_does_not_dirty_session(tmp_path):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            issue = NewsletterIssue(
                ship_number=None, display_label="0a", slug="draft-preview", subject="Preview",
                period_start=now - timedelta(days=10), period_end=now, status="draft",
                finds_json="[]",
            )
            session.add(issue)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="f", title="Live find", url="https://example.com",
                status="auto_indexed", newsletter_issue_id=issue.id,
                newsletter_pending=True, first_seen_at=now,
            ))
            await session.commit()

            detail = await newsletter_module.get_issue_detail(session, issue.id)

            assert detail is not None
            assert detail["find_count"] == 1
            assert "Live find" in detail["html_preview"]
            assert len(session.dirty) == 0
            persisted = (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.id == issue.id)
            )).scalar_one()
            assert persisted.finds_json == "[]"

    asyncio.run(go())
    asyncio.run(engine.dispose())


def test_assign_ship_number_with_retry_recovers_from_unique_collision(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-1", subject="Issue 1",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="sent", sent_at=now - timedelta(days=10),
            ))
            issue = NewsletterIssue(
                ship_number=None, display_label="1a", slug="sending", subject="Sending",
                period_start=now - timedelta(days=10), period_end=now,
                status="sending",
            )
            session.add(issue)
            await session.commit()
            await session.refresh(issue)
            values = iter([1, 2])

            async def fake_next_ship_number(_session):
                return next(values)

            monkeypatch.setattr(newsletter_module, "_next_ship_number", fake_next_ship_number)

            numbered = await newsletter_module._assign_ship_number_with_retry(session, issue.id)

            assert numbered.ship_number == 2
            assert numbered.display_label == "2"
            assert numbered.status == "sending"

    asyncio.run(go())
    asyncio.run(engine.dispose())



def test_assign_ship_number_retry_exhaustion_returns_to_operator_review(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="sent-1", subject="Issue 1",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="sent", sent_at=now - timedelta(days=10),
            ))
            issue = NewsletterIssue(
                ship_number=None, display_label="1a", slug="sending-fail", subject="Sending",
                period_start=now - timedelta(days=10), period_end=now,
                status="sending", scheduled_send_at=now,
            )
            session.add(issue)
            await session.commit()
            await session.refresh(issue)

            async def always_collides(_session):
                return 1

            monkeypatch.setattr(newsletter_module, "_next_ship_number", always_collides)

            with pytest.raises(RuntimeError):
                await newsletter_module._assign_ship_number_with_retry(session, issue.id, attempts=2)

            reviewed = (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.id == issue.id)
            )).scalar_one()
            assert reviewed.status == "awaiting_approval"
            assert reviewed.scheduled_send_at is None
            assert reviewed.ship_number is None

    asyncio.run(go())
    asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Endpoint coverage
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pi.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(init())

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    return TestClient(api_main.app), engine, maker


def test_create_new_issue_endpoint_returns_next_label(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 201, response.text
        issue = response.json()["issue"]
        assert issue["display_label"] == "0a"
        assert issue["ship_number"] is None
        assert issue["status"] == "draft"
        assert issue["target_send_at"] is not None

        response2 = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response2.json()["issue"]["display_label"] == "0b"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_delete_issue_endpoint_removes_row(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        issue_id = create_resp.json()["issue"]["id"]

        delete_resp = client.delete(
            f"/admin/api/newsletter/issues/{issue_id}",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert delete_resp.status_code == 200, delete_resp.text
        payload = delete_resp.json()["deleted"]
        assert payload["display_label"] == "0a"

        # Refetch returns 404.
        detail_resp = client.get(
            f"/admin/api/newsletter/issues/{issue_id}",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert detail_resp.status_code == 404

        # Next create reuses "0a" because nothing has ever shipped and
        # the previous draft was hard-deleted (no lingering label).
        recreate = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert recreate.json()["issue"]["display_label"] == "0a"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_unpublish_endpoint_rejects_draft(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        create_resp = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        issue_id = create_resp.json()["issue"]["id"]
        resp = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/unpublish",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert resp.status_code == 409, resp.text
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_assignable_issues_endpoint_returns_open_issues_and_suggested_default(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                now = datetime.now(timezone.utc)
                session.add(NewsletterIssue(
                    ship_number=1, display_label="1", slug="sent1",
                    subject="Issue 1",
                    period_start=now - timedelta(days=10), period_end=now,
                    status="sent", target_send_at=now - timedelta(days=2),
                    sent_at=now - timedelta(days=2),
                ))
                session.add(NewsletterIssue(
                    ship_number=None, display_label="1a", slug="draft1a",
                    subject="1a",
                    period_start=now, period_end=now + timedelta(days=10),
                    status="draft", target_send_at=now + timedelta(days=8),
                ))
                await session.commit()
        asyncio.run(seed())

        response = client.get(
            "/admin/api/newsletter/assignable-issues",
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "next_default_target_send_at" in body
        issues = body["issues"]
        labels = {iss["display_label"] for iss in issues}
        assert labels == {"1a"}
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_assign_issue_endpoint_tags_and_clears(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="x", title="t", url="u", status="auto_indexed",
                )
                session.add(find)
                await session.commit()
                await session.refresh(find)
                return find.id
        find_id = asyncio.run(seed())

        create_resp = client.post(
            "/admin/api/newsletter/issues/new",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        issue_id = create_resp.json()["issue"]["id"]

        tag_resp = client.post(
            f"/admin/api/discovery/finds/{find_id}/assign-issue",
            json={"issue_id": issue_id},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert tag_resp.status_code == 200, tag_resp.text
        assert tag_resp.json()["find"]["newsletter_issue_id"] == issue_id
        assert tag_resp.json()["find"]["newsletter_pending"] is True

        untag_resp = client.post(
            f"/admin/api/discovery/finds/{find_id}/assign-issue",
            json={"issue_id": None},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert untag_resp.status_code == 200
        assert untag_resp.json()["find"]["newsletter_issue_id"] is None
        assert untag_resp.json()["find"]["newsletter_pending"] is False
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_assign_issue_endpoint_rejects_closed_issue(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                source = DiscoverySource(name="s", watch_type="rss_watch", target="https://x", active=True)
                session.add(source)
                await session.flush()
                find = DiscoveryFind(
                    discovery_source_id=source.id, finding_type="post",
                    external_id="x", title="t", url="u", status="auto_indexed",
                )
                session.add(find)
                now = datetime.now(timezone.utc)
                closed = NewsletterIssue(
                    ship_number=1, display_label="1", slug="closed",
                    subject="Issue 1",
                    period_start=now - timedelta(days=10), period_end=now,
                    status="sent",
                )
                session.add(closed)
                await session.commit()
                await session.refresh(find)
                await session.refresh(closed)
                return find.id, closed.id
        find_id, issue_id = asyncio.run(seed())

        response = client.post(
            f"/admin/api/discovery/finds/{find_id}/assign-issue",
            json={"issue_id": issue_id},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 409
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
