"""Migration coverage for the artifact-scan category rollup.

The migration converts a per-file shape (one DiscoveryFind per leaf file
in a github_repo_artifact_scan source) into a per-category shape (one
DiscoveryFind per top-level directory). Curation flags are unioned across
the collapsed children; leaf metadata is preserved in raw_payload.children
so LIBRARY retrieval can keep citing specific files.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from core.db.migrations import _collapse_artifact_finds_into_categories
from core.db.models import DiscoveryFind, DiscoverySource


def _make_engine(tmp_path):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rollup.db'}")


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def test_migration_collapses_per_file_rows_into_per_category(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(
                name="example_author_automation",
                watch_type="github_repo_artifact_scan",
                target="example-author/automation-bundles",
                active=True,
            )
            session.add(source)
            await session.flush()
            now = datetime.now(timezone.utc)
            # Three files under "TimeZest", two under "Account Management",
            # one under "Apple Shortcuts + Siri".
            files = [
                ("artifact:TimeZest/Option Generators/ListAgentsWorkflow.json:aaa",
                 "example-author/automation-bundles artifact: TimeZest/Option Generators/ListAgentsWorkflow.json"),
                ("artifact:TimeZest/Option Generators/ListAppointmentsWorkflow.json:bbb",
                 "example-author/automation-bundles artifact: TimeZest/Option Generators/ListAppointmentsWorkflow.json"),
                ("artifact:TimeZest/Subworkflows/Send Link/ReadMe.md:ccc",
                 "example-author/automation-bundles artifact: TimeZest/Subworkflows/Send Link/ReadMe.md"),
                ("artifact:Account Management/DeleteOrg.bundle.json:ddd",
                 "example-author/automation-bundles artifact: Account Management/DeleteOrg.bundle.json"),
                ("artifact:Account Management/DeleteUser.bundle.json:eee",
                 "example-author/automation-bundles artifact: Account Management/DeleteUser.bundle.json"),
                ("artifact:Apple Shortcuts + Siri/workflow_template.bundle.json:fff",
                 "example-author/automation-bundles artifact: Apple Shortcuts + Siri/workflow_template.bundle.json"),
            ]
            for i, (ext_id, title) in enumerate(files):
                session.add(DiscoveryFind(
                    discovery_source_id=source.id,
                    finding_type="workflow_bundle",
                    external_id=ext_id,
                    title=title,
                    url=f"https://github.com/example-author/automation-bundles/blob/HEAD/{ext_id.split(':')[1]}",
                    status="auto_indexed",
                    first_seen_at=now - timedelta(days=10 - i),
                    last_seen_at=now - timedelta(hours=i),
                    decided_at=now - timedelta(days=10 - i),
                    # Flag one of the TimeZest files as ticker_featured; it
                    # should propagate to the rolled-up TimeZest row.
                    ticker_featured=(i == 1),
                    ticker_featured_at=(now - timedelta(hours=2)) if i == 1 else None,
                    # Flag one of the Account Management files as queued;
                    # propagate to its category.
                    newsletter_pending=(i == 3),
                ))
            await session.commit()
            return source.id

    source_id = asyncio.run(setup())

    async def migrate_and_inspect():
        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)

        maker = _maker(engine)
        async with maker() as session:
            rows = (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == source_id)
            )).scalars().all()
            return rows

    rows = asyncio.run(migrate_and_inspect())
    # Three categories total. Per-file rows collapsed.
    assert len(rows) == 3, f"Expected 3 category rows, got {len(rows)}: {[r.title for r in rows]}"
    by_category = {row.category: row for row in rows}
    assert set(by_category.keys()) == {"TimeZest", "Account Management", "Apple Shortcuts + Siri"}

    timezest = by_category["TimeZest"]
    assert timezest.child_count == 3
    assert timezest.ticker_featured is True  # unioned from the flagged child
    assert timezest.ticker_featured_at is not None
    assert timezest.external_id == "category:TimeZest"
    assert timezest.title == "example_author_automation/TimeZest"
    assert "/tree/HEAD/TimeZest" in timezest.url
    children = json.loads(timezest.raw_payload)["children"]
    assert len(children) == 3
    assert any(c["path"] == "TimeZest/Option Generators/ListAgentsWorkflow.json" for c in children)
    assert any(c["sha"] == "aaa" and c["kind"] == "workflow_bundle" for c in children)

    automation_mgmt = by_category["Account Management"]
    assert automation_mgmt.child_count == 2
    assert automation_mgmt.newsletter_pending is True

    apple = by_category["Apple Shortcuts + Siri"]
    assert apple.child_count == 1
    assert apple.ticker_featured is False
    assert apple.newsletter_pending is False

    asyncio.run(engine.dispose())


def test_migration_is_idempotent(tmp_path):
    """Running the collapse twice doesn't re-collapse already-categorized rows."""
    engine = _make_engine(tmp_path)

    async def setup_and_migrate_twice():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(
                name="repo",
                watch_type="github_repo_artifact_scan",
                target="o/r",
                active=True,
            )
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="artifact:Foo/bar.json:sha1",
                title="repo artifact: Foo/bar.json",
                url="https://github.com/o/r/blob/HEAD/Foo/bar.json",
                status="auto_indexed",
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="artifact:Foo/baz.json:sha2",
                title="repo artifact: Foo/baz.json",
                url="https://github.com/o/r/blob/HEAD/Foo/baz.json",
                status="auto_indexed",
            ))
            await session.commit()
            sid = source.id

        # First migration: collapses 2 -> 1
        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)
        async with _maker(engine)() as session:
            first = (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == sid)
            )).scalars().all()
        assert len(first) == 1
        assert first[0].category == "Foo"
        assert first[0].child_count == 2

        # Second migration: no-op
        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)
        async with _maker(engine)() as session:
            second = (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == sid)
            )).scalars().all()
        assert len(second) == 1
        assert second[0].id == first[0].id  # same row, unchanged

    asyncio.run(setup_and_migrate_twice())
    asyncio.run(engine.dispose())


def test_migration_skips_non_artifact_sources(tmp_path):
    """RSS / release sources already have one row per signal; the
    migration must leave them alone."""
    engine = _make_engine(tmp_path)

    async def setup_and_run():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            rss = DiscoverySource(
                name="platform_blog", watch_type="rss_watch",
                target="https://platform.example.com/feed", active=True,
            )
            session.add(rss)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=rss.id,
                finding_type="post",
                external_id="post:abc",
                title="Why MSPs should automate the sales process",
                url="https://platform.example.com/post1",
                status="auto_indexed",
            ))
            await session.commit()
            sid = rss.id

        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)

        async with _maker(engine)() as session:
            rows = (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == sid)
            )).scalars().all()
            return rows

    rows = asyncio.run(setup_and_run())
    assert len(rows) == 1
    assert rows[0].category == ""  # unchanged
    assert rows[0].title == "Why MSPs should automate the sales process"

    asyncio.run(engine.dispose())


def test_migration_uses_title_fallback_and_skips_top_level_files(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup_and_run():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(
                name="example_author_automation",
                watch_type="github_repo_artifact_scan",
                target="example-author/automation-bundles",
                active=True,
            )
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="legacy:readme",
                title="example-author/automation-bundles artifact: README.md",
                url="https://github.com/example-author/automation-bundles/blob/HEAD/README.md",
                status="auto_indexed",
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="legacy:timezest",
                title="example-author/automation-bundles artifact: TimeZest/workflow.json",
                url="https://github.com/example-author/automation-bundles/blob/HEAD/TimeZest/workflow.json",
                status="auto_indexed",
            ))
            await session.commit()
            sid = source.id

        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)

        async with _maker(engine)() as session:
            return (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == sid)
            )).scalars().all()

    rows = asyncio.run(setup_and_run())
    by_external_id = {row.external_id: row for row in rows}
    assert len(rows) == 2
    assert by_external_id["legacy:readme"].category == ""
    assert by_external_id["category:TimeZest"].category == "TimeZest"
    assert by_external_id["category:TimeZest"].child_count == 1

    asyncio.run(engine.dispose())


def test_migration_folds_leftover_files_into_existing_category_row(tmp_path):
    engine = _make_engine(tmp_path)

    async def setup_and_run():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = _maker(engine)
        async with maker() as session:
            source = DiscoverySource(
                name="repo",
                watch_type="github_repo_artifact_scan",
                target="o/r",
                active=True,
            )
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="category:Foo",
                title="repo/Foo",
                url="https://github.com/o/r/tree/HEAD/Foo",
                raw_payload=json.dumps({
                    "children": [{
                        "external_id": "artifact:Foo/a.json:sha1",
                        "title": "repo artifact: Foo/a.json",
                        "url": "https://github.com/o/r/blob/HEAD/Foo/a.json",
                    }],
                    "category": "Foo",
                }),
                status="auto_indexed",
                category="Foo",
                child_count=1,
                ticker_featured=True,
            ))
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="repo_artifact",
                external_id="artifact:Foo/b.json:sha2",
                title="repo artifact: Foo/b.json",
                url="https://github.com/o/r/blob/HEAD/Foo/b.json",
                status="auto_indexed",
            ))
            await session.commit()
            sid = source.id

        async with engine.begin() as conn:
            await _collapse_artifact_finds_into_categories(conn)

        async with _maker(engine)() as session:
            return (await session.execute(
                select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == sid)
            )).scalars().all()

    rows = asyncio.run(setup_and_run())
    assert len(rows) == 1
    row = rows[0]
    assert row.external_id == "category:Foo"
    assert row.category == "Foo"
    assert row.child_count == 2
    assert row.ticker_featured is True
    children = json.loads(row.raw_payload)["children"]
    assert {child["external_id"] for child in children} == {
        "artifact:Foo/a.json:sha1",
        "artifact:Foo/b.json:sha2",
    }
    assert {child["path"] for child in children} == {"Foo/a.json", "Foo/b.json"}

    asyncio.run(engine.dispose())
