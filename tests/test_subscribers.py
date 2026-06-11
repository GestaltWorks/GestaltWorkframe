"""Unit tests for core/subscribers.py (subscriber CRUD + subscribe_and_reply)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gestaltworkframe.core.db.models  # noqa: F401 - register tables
import gestaltworkframe.core.subscribers as subscribers
from gestaltworkframe.core.subscribers import (
    active_subscribers,
    get_subscriber_by_email,
    get_subscriber_by_token,
    record_autoreply,
    recent_autoreply_exists,
    subscribe_and_reply,
    unsubscribe_by_token,
    upsert_subscriber,
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


# ---------------------------------------------------------------------------
# upsert_subscriber
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_creates_new_subscriber(session_maker):
    async with session_maker() as session:
        sub, created = await upsert_subscriber(
            session, email="A@Example.COM", name="Ada", role="student", topics=("general", "edu")
        )
        await session.commit()

    assert created is True
    assert sub.email == "a@example.com"  # normalized
    assert sub.source_role == "student"
    assert set(sub.topics.split("|")) == {"edu", "general"}
    assert sub.unsubscribe_token


@pytest.mark.asyncio
async def test_upsert_updates_existing_and_unions_topics(session_maker):
    async with session_maker() as session:
        await upsert_subscriber(session, email="x@y.com", name="X", role="student", topics=("general",))
        await session.commit()
        sub, created = await upsert_subscriber(
            session, email="x@y.com", name="X2", role="automation_engineer", topics=("auto",)
        )
        await session.commit()

    assert created is False
    assert sub.name == "X2"
    assert set(sub.topics.split("|")) == {"auto", "general"}


@pytest.mark.asyncio
async def test_upsert_resubscribes_and_rotates_token(session_maker):
    async with session_maker() as session:
        sub, _ = await upsert_subscriber(session, email="z@y.com", name="Z", role="student")
        await session.commit()
        old_token = sub.unsubscribe_token
        sub.unsubscribed_at = datetime.now(timezone.utc)
        session.add(sub)
        await session.commit()

        again, created = await upsert_subscriber(session, email="z@y.com", name="Z", role="student")
        await session.commit()

    assert created is False
    assert again.unsubscribed_at is None
    assert again.unsubscribe_token != old_token


# ---------------------------------------------------------------------------
# get_* / unsubscribe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_subscriber_by_email_normalizes(session_maker):
    async with session_maker() as session:
        await upsert_subscriber(session, email="Cap@Y.com", name="C", role="student")
        await session.commit()
        found = await get_subscriber_by_email(session, "  cap@y.com ")

    assert found is not None
    assert found.email == "cap@y.com"


@pytest.mark.asyncio
async def test_get_subscriber_by_token_empty_and_found(session_maker):
    async with session_maker() as session:
        sub, _ = await upsert_subscriber(session, email="t@y.com", name="T", role="student")
        await session.commit()

        assert await get_subscriber_by_token(session, "") is None
        assert await get_subscriber_by_token(session, "   ") is None
        found = await get_subscriber_by_token(session, sub.unsubscribe_token)

    assert found is not None
    assert found.email == "t@y.com"


@pytest.mark.asyncio
async def test_unsubscribe_by_token_minimizes_fields_and_is_idempotent(session_maker):
    async with session_maker() as session:
        sub, _ = await upsert_subscriber(session, email="u@y.com", name="U", role="student", topics=("edu",))
        await session.commit()
        token = sub.unsubscribe_token

        first = await unsubscribe_by_token(session, token)
        await session.commit()
        first_time = first.unsubscribed_at

        second = await unsubscribe_by_token(session, token)
        await session.commit()

    assert first.unsubscribed_at is not None
    assert first.name == "" and first.source_role == "" and first.topics == ""
    # Idempotent: the timestamp is not overwritten on the second call.
    assert second.unsubscribed_at == first_time


@pytest.mark.asyncio
async def test_unsubscribe_by_token_unknown_returns_none(session_maker):
    async with session_maker() as session:
        assert await unsubscribe_by_token(session, "no-such-token") is None


# ---------------------------------------------------------------------------
# recent_autoreply_exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_autoreply_exists_counts_sent_not_failed(session_maker):
    async with session_maker() as session:
        sub, _ = await upsert_subscriber(session, email="r@y.com", name="R", role="student")
        await session.commit()

        assert await recent_autoreply_exists(session, subscriber_id=sub.id) is False

        await record_autoreply(
            session, subscriber_id=sub.id, contact_id="c1", role="student",
            template_id="welcome", status="failed",
        )
        await session.commit()
        # 'failed' rows do not count.
        assert await recent_autoreply_exists(session, subscriber_id=sub.id) is False

        await record_autoreply(
            session, subscriber_id=sub.id, contact_id="c1", role="student",
            template_id="welcome", status="sent",
        )
        await session.commit()
        assert await recent_autoreply_exists(session, subscriber_id=sub.id) is True


# ---------------------------------------------------------------------------
# active_subscribers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_subscribers_excludes_unsubscribed_and_filters_topic(session_maker):
    async with session_maker() as session:
        await upsert_subscriber(session, email="a@y.com", name="A", role="student", topics=("edu",))
        await upsert_subscriber(session, email="b@y.com", name="B", role="student", topics=("auto",))
        gone, _ = await upsert_subscriber(session, email="c@y.com", name="C", role="student", topics=("edu",))
        await session.commit()
        await unsubscribe_by_token(session, gone.unsubscribe_token)
        await session.commit()

        all_active = await active_subscribers(session)
        edu_only = await active_subscribers(session, topic="EDU")

    assert {s.email for s in all_active} == {"a@y.com", "b@y.com"}
    assert {s.email for s in edu_only} == {"a@y.com"}


# ---------------------------------------------------------------------------
# subscribe_and_reply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_and_reply_success(session_maker, monkeypatch):
    async def fake_send(role, name, email, token):
        return "sent", "welcome", ""

    monkeypatch.setattr(subscribers, "send_auto_reply", fake_send)

    async with session_maker() as session:
        sub, status, template, error = await subscribe_and_reply(
            session, name="Ada", email="ada@y.com", role="student", contact_id="c1"
        )

    assert sub is not None
    assert (status, template, error) == ("sent", "welcome", "")


@pytest.mark.asyncio
async def test_subscribe_and_reply_cooldown_suppresses_second_send(session_maker, monkeypatch):
    calls = []

    async def fake_send(role, name, email, token):
        calls.append(email)
        return "sent", "welcome", ""

    monkeypatch.setattr(subscribers, "send_auto_reply", fake_send)

    async with session_maker() as session:
        await subscribe_and_reply(session, name="Ada", email="dup@y.com", role="student", contact_id="c1")
        sub, status, template, error = await subscribe_and_reply(
            session, name="Ada", email="dup@y.com", role="student", contact_id="c2"
        )

    # The second submit within the cooldown skips the outbound email.
    assert calls == ["dup@y.com"]
    assert status == "skipped"
    assert template == "cooldown"
    assert error == "auto_reply_cooldown_active"


@pytest.mark.asyncio
async def test_subscribe_and_reply_upsert_failure_returns_marker(session_maker, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(subscribers, "upsert_subscriber", boom)

    async with session_maker() as session:
        result = await subscribe_and_reply(session, name="A", email="a@y.com", role="student")

    assert result == (None, "", "", "upsert_failed")
