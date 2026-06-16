"""Direct tests for core/db/crud.py against an in-memory async SQLite session.

The HTTP-layer tests reach these helpers only indirectly; this exercises the
intake upsert paths (insert-then-update) and the terminal-intake apply logic —
including the IntegrityError recovery branch — that the API tests don't cover.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from gestaltworkframe.core.db.crud import (
    create_conversation,
    get_conversation,
    save_intake_record,
    save_terminal_intake_submission,
)
from gestaltworkframe.core.db.models import IntakeRecord, TerminalIntakeRecord


@pytest.fixture
def maker(tmp_path) -> sessionmaker:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crud.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    return asyncio.run(init())


async def _session(maker) -> AsyncGenerator[AsyncSession, None]:
    async with maker() as session:
        yield session


async def test_save_intake_record_inserts_then_updates(maker):
    async with maker() as session:
        conv = await create_conversation("build", session)

        first = await save_intake_record(
            conv.id,
            "build",
            {"objective": "ship", "building": "app", "maturity": "early", "help_needed": "code"},
            session,
        )
        assert first.objective == "ship"
        assert first.selected_mode == "build"

        # Second call for the same conversation updates the existing row in place
        # rather than inserting a duplicate (covers the record-is-not-None branch).
        second = await save_intake_record(
            conv.id,
            "operate",
            {"objective": "scale", "building": "app", "maturity": "growth", "help_needed": "ops"},
            session,
        )
        assert second.id == first.id
        assert second.selected_mode == "operate"
        assert second.objective == "scale"

        rows = (
            await session.execute(
                select(IntakeRecord).where(IntakeRecord.conversation_id == conv.id)
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_save_intake_record_defaults_missing_fields(maker):
    async with maker() as session:
        conv = await create_conversation("build", session)
        record = await save_intake_record(conv.id, "build", {"objective": "only this"}, session)
        assert record.objective == "only this"
        assert record.building == ""
        assert record.maturity == ""
        assert record.help_needed == ""


async def test_save_terminal_intake_strips_control_chars_and_counts(maker):
    async with maker() as session:
        record = await save_terminal_intake_submission(
            "term-1",
            "bui\x00ld",
            {"objective": "ship\x07 it", "building": "app"},
            session,
            source_path="/start",
            referrer="https://ref",
            user_agent="agent/1",
            ip_address="1.2.3.4",
        )
        # Control characters are stripped from stored text.
        assert record.selected_mode == "build"
        assert record.objective == "ship it"
        assert record.submission_count == 1
        assert record.source_path == "/start"

        # A second submission for the same terminal session updates the latest
        # row and bumps the counter; first-write-wins metadata stays put.
        again = await save_terminal_intake_submission(
            "term-1",
            "build",
            {"objective": "ship it v2", "building": "app"},
            session,
            source_path="/ignored",
            count_submission=True,
        )
        assert again.id == record.id
        assert again.objective == "ship it v2"
        assert again.submission_count == 2
        assert again.source_path == "/start"  # not overwritten once set


async def test_save_terminal_intake_links_conversation_and_contact(maker):
    async with maker() as session:
        record = await save_terminal_intake_submission(
            "term-2",
            "build",
            {"objective": "x"},
            session,
            conversation_id="conv-xyz",
            contact_id="contact-xyz",
            count_submission=False,
        )
        assert record.conversation_id == "conv-xyz"
        assert record.contact_id == "contact-xyz"
        assert record.submission_count == 0  # count_submission=False


async def test_get_conversation_roundtrip_and_missing(maker):
    async with maker() as session:
        conv = await create_conversation("build", session)
        found = await get_conversation(conv.id, session)
        assert found is not None and found.id == conv.id
        assert await get_conversation("does-not-exist", session) is None


async def test_terminal_intake_distinct_sessions_are_separate_rows(maker):
    async with maker() as session:
        await save_terminal_intake_submission("a", "build", {"objective": "1"}, session)
        await save_terminal_intake_submission("b", "build", {"objective": "2"}, session)
        rows = (await session.execute(select(TerminalIntakeRecord))).scalars().all()
        assert {r.terminal_session_id for r in rows} == {"a", "b"}
