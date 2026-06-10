"""Migration coverage for the per-issue newsletter rebuild.

The model shift moves from a global newsletter_pending boolean to a
per-find newsletter_issue_id FK pointing at a specific NewsletterIssue.
Each issue carries a sticky display_label, an optional ship_number
(assigned only at successful send), a target_send_at, an unpublished_at
soft-hide stamp, and an approval_email_sent_at de-dupe stamp for the
24h reminder cron.

Tested behaviors:

1. newsletter_pending=true finds get tagged onto a single catch-up
   draft so the queue survives the model shift. The catch-up issue
   uses the new display_label scheme ("0a" before anything has
   shipped).
2. The catch-up assignment is idempotent: running the migration
   again on an already-migrated DB is a no-op.
3. The catch-up draft's target_send_at anchors on the last sent
   issue's target_send_at + 10 days.
4. The _label_letter helper produces a stable base-26 sequence that
   matches the live label-computer in core.newsletter.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from gestaltworkframe.core.db.migrations import (
    _collapse_artifact_finds_into_categories,
    _migrate_newsletter_issue_table,
    _assign_pending_finds_to_catchup_issue,
    _label_letter,
)
from gestaltworkframe.core.db.models import DiscoveryFind, DiscoverySource, NewsletterIssue


def _make_engine(tmp_path):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'nl_migrate.db'}")


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_label_letter_helper_matches_live_logic():
    """The migration's _label_letter must match the live core.newsletter
    one exactly so backfilled labels and runtime-created labels share
    the same sequence."""
    from gestaltworkframe.core.newsletter import _label_letter as live_label_letter

    for idx in [0, 1, 25, 26, 51, 52, 100, 675, 676]:
        assert _label_letter(idx) == live_label_letter(idx), f"divergence at idx={idx}"


def test_pending_finds_tagged_onto_catchup_draft(tmp_path):
    """Existing newsletter_pending=true finds get assigned to a
    single new draft. Dismissed pending finds and non-pending finds
    are left alone. The catch-up draft gets display_label='0a' when
    nothing has previously shipped."""
    engine = _make_engine(tmp_path)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            source = DiscoverySource(
                name="src", watch_type="rss_watch", target="https://x", active=True,
            )
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p1", title="pending one", url="u1",
                status="auto_indexed", decided_at=now,
                newsletter_pending=True,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p2", title="pending two", url="u2",
                status="auto_indexed", decided_at=now,
                newsletter_pending=True,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p3", title="dismissed pending", url="u3",
                status="auto_indexed", decided_at=now,
                newsletter_pending=True, dismissed=True,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p4", title="not pending", url="u4",
                status="auto_indexed", decided_at=now,
            ))
            await session.commit()

    asyncio.run(setup())

    async def migrate_and_inspect():
        async with engine.begin() as conn:
            await _assign_pending_finds_to_catchup_issue(conn)
        async with _maker(engine)() as session:
            finds = (await session.execute(
                select(DiscoveryFind).order_by(DiscoveryFind.external_id)
            )).scalars().all()
            issues = (await session.execute(select(NewsletterIssue))).scalars().all()
            return finds, issues

    finds, issues = asyncio.run(migrate_and_inspect())
    by_ext = {f.external_id: f for f in finds}

    assert len(issues) == 1
    issue = issues[0]
    assert issue.status == "draft"
    assert issue.target_send_at is not None
    # No prior ship, no existing 0-epoch labels -> '0a'.
    assert issue.display_label == "0a"
    assert issue.ship_number is None
    # target ~ now + 10d when there's no prior anchor.
    delta = issue.target_send_at - datetime.now(timezone.utc)
    if hasattr(delta, "total_seconds"):
        assert 9 * 86400 < delta.total_seconds() < 11 * 86400

    assert by_ext["p1"].newsletter_issue_id == issue.id
    assert by_ext["p2"].newsletter_issue_id == issue.id
    assert by_ext["p3"].newsletter_issue_id is None
    assert by_ext["p4"].newsletter_issue_id is None
    asyncio.run(engine.dispose())


def test_catchup_migration_is_idempotent(tmp_path):
    """Running the migration twice doesn't create a second catch-up."""
    engine = _make_engine(tmp_path)

    async def setup_and_migrate_twice():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            source = DiscoverySource(
                name="src", watch_type="rss_watch", target="https://x", active=True,
            )
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="solo", title="just one", url="u1",
                status="auto_indexed", decided_at=datetime.now(timezone.utc),
                newsletter_pending=True,
            ))
            await session.commit()

        async with engine.begin() as conn:
            await _assign_pending_finds_to_catchup_issue(conn)
        async with engine.begin() as conn:
            await _assign_pending_finds_to_catchup_issue(conn)

        async with _maker(engine)() as session:
            issues = (await session.execute(select(NewsletterIssue))).scalars().all()
            assert len(issues) == 1
            finds = (await session.execute(select(DiscoveryFind))).scalars().all()
            assert finds[0].newsletter_issue_id == issues[0].id

    asyncio.run(setup_and_migrate_twice())
    asyncio.run(engine.dispose())



def test_newsletter_issue_migration_backfills_blank_current_schema_rows(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup_and_migrate():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=None, display_label="", slug="blank-current-schema", subject="Blank",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            await session.commit()
        async with engine.begin() as conn:
            await _migrate_newsletter_issue_table(conn)
        async with _maker(engine)() as session:
            return (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.slug == "blank-current-schema")
            )).scalar_one()

    issue = asyncio.run(setup_and_migrate())
    assert issue.display_label == "0a"
    assert issue.ship_number is None
    asyncio.run(engine.dispose())


def test_catchup_migration_anchors_on_last_sent_issue(tmp_path):
    """When a prior sent issue exists, the catch-up draft's
    target_send_at is anchored to that issue's send date + 10 days,
    and the display_label uses '1a' to reflect the post-ship epoch."""
    engine = _make_engine(tmp_path)

    async def setup_and_migrate():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            source = DiscoverySource(
                name="src", watch_type="rss_watch", target="https://x", active=True,
            )
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            last_sent_target = now - timedelta(days=2)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1",
                slug="prior", subject="Issue 1",
                period_start=now - timedelta(days=12),
                period_end=now - timedelta(days=2),
                status="sent",
                target_send_at=last_sent_target,
                sent_at=last_sent_target,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p1", title="p", url="u",
                status="auto_indexed", decided_at=now,
                newsletter_pending=True,
            ))
            await session.commit()
            anchor = last_sent_target

        async with engine.begin() as conn:
            await _assign_pending_finds_to_catchup_issue(conn)

        async with _maker(engine)() as session:
            issues = (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.status == "draft")
            )).scalars().all()
            return issues[0], anchor

    catchup, anchor = asyncio.run(setup_and_migrate())
    target = catchup.target_send_at
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    expected = anchor + timedelta(days=10)
    assert abs((target - expected).total_seconds()) < 5
    # ship_number=1 is the largest at catch-up time, no existing
    # 1-epoch unsent rows, so the catch-up gets '1a'.
    assert catchup.display_label == "1a"
    assert catchup.ship_number is None
    asyncio.run(engine.dispose())




def test_catchup_migration_label_epoch_uses_letter_suffix_only(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup_and_migrate():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            source = DiscoverySource(name="src", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            session.add(NewsletterIssue(
                ship_number=1, display_label="1", slug="prior", subject="Issue 1",
                period_start=now - timedelta(days=20), period_end=now - timedelta(days=10),
                status="sent", sent_at=now - timedelta(days=10),
            ))
            session.add(NewsletterIssue(
                ship_number=None, display_label="12a", slug="other-epoch", subject="12a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            session.add(NewsletterIssue(
                ship_number=None, display_label="100a", slug="hundred-epoch", subject="100a",
                period_start=now - timedelta(days=10), period_end=now,
                status="draft",
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post",
                external_id="p1", title="p", url="u",
                status="auto_indexed", decided_at=now,
                newsletter_pending=True,
            ))
            await session.commit()

        async with engine.begin() as conn:
            await _assign_pending_finds_to_catchup_issue(conn)

        async with _maker(engine)() as session:
            return (await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.slug.like("catchup-%"))
            )).scalar_one()

    catchup = asyncio.run(setup_and_migrate())
    assert catchup.display_label == "1a"
    asyncio.run(engine.dispose())


def test_artifact_collapse_reentrant_keeps_existing_category_rep(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup_and_collapse():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        async with _maker(engine)() as session:
            source = DiscoverySource(
                name="repo", watch_type="github_repo_artifact_scan",
                target="example-org/library-repo", active=True,
            )
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            rep = DiscoveryFind(
                discovery_source_id=source.id, finding_type="repo_artifact_category",
                external_id="category:Library", title="repo/Library", url="https://example.com/tree/Library",
                status="auto_indexed", first_seen_at=now, category="Library",
                raw_payload=json.dumps({"children": []}), child_count=0,
            )
            child = DiscoveryFind(
                discovery_source_id=source.id, finding_type="repo_artifact",
                external_id="artifact:Library/thing.bundle.json:abc", title="repo/Library/thing.bundle.json",
                url="https://example.com/blob/thing", status="auto_indexed",
                first_seen_at=now + timedelta(seconds=1), category="",
                raw_payload="{}", newsletter_pending=True,
            )
            session.add_all([rep, child])
            await session.commit()
            rep_id = rep.id
            child_id = child.id

        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM schema_migrations WHERE name = :name"), {
                "name": "collapse_artifact_finds_into_categories_v1",
            })
            await _collapse_artifact_finds_into_categories(conn)

        async with _maker(engine)() as session:
            rows = (await session.execute(select(DiscoveryFind))).scalars().all()
            return rep_id, child_id, rows

    rep_id, child_id, rows = asyncio.run(setup_and_collapse())
    ids = {row.id for row in rows}
    assert rep_id in ids
    assert child_id not in ids
    rep = next(row for row in rows if row.id == rep_id)
    assert rep.category == "Library"
    assert rep.newsletter_pending is True
    assert rep.child_count == 1
    asyncio.run(engine.dispose())
