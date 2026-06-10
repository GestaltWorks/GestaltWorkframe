"""Tests for the retention sweep helpers in core/retention.

These build a real in-memory SQLite database, seed it with records at
known ages, and verify the sweep deletes only what falls outside the
configured policy windows. Dry-run mode reports counts without writing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gestaltworkframe.core.db.models  # noqa: F401  - register all tables on SQLModel.metadata
from gestaltworkframe.core.db.models import (
    ChatUsageRecord,
    ContactNotificationRecord,
    ContactRecord,
    Conversation,
    IntakeRecord,
    MessageRecord,
    NewsletterDelivery,
    NewsletterIssue,
    Subscriber,
    SubscriberAutoReplyRecord,
    TerminalIntakeRecord,
)
from gestaltworkframe.core.retention import (
    RetentionPolicy,
    RetentionSweepSummary,
    sweep,
)


def _now() -> datetime:
    return datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


def _aged(days: int) -> datetime:
    return _now() - timedelta(days=days)


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


async def _seed(session_maker) -> None:
    """Seed two of each operational record type: one fresh, one stale."""
    async with session_maker() as session:
        # Fresh + stale conversations and child rows.
        fresh_conv = Conversation(id="conv-fresh", mode="automator", created_at=_aged(10))
        stale_conv = Conversation(id="conv-stale", mode="automator", created_at=_aged(200))
        session.add_all([fresh_conv, stale_conv])
        issue = NewsletterIssue(
            id="issue-1",
            slug="issue-1",
            period_start=_aged(20),
            period_end=_aged(10),
            created_at=_aged(10),
        )
        subscriber = Subscriber(
            id="sub-1",
            email="sub@example.com",
            name="Subscriber",
            source_role="student",
        )
        session.add_all([
            MessageRecord(conversation_id="conv-fresh", role="user", content="hi", created_at=_aged(10)),
            MessageRecord(conversation_id="conv-stale", role="user", content="old", created_at=_aged(200)),
            IntakeRecord(conversation_id="conv-fresh", selected_mode="automator", created_at=_aged(10)),
            IntakeRecord(conversation_id="conv-stale", selected_mode="automator", created_at=_aged(200)),
            ChatUsageRecord(conversation_id="conv-fresh", ip_address="1.1.1.1", created_at=_aged(10)),
            ChatUsageRecord(conversation_id="conv-stale", ip_address="2.2.2.2", created_at=_aged(200)),
            # Orphan usage row, no conversation.
            ChatUsageRecord(conversation_id=None, ip_address="3.3.3.3", created_at=_aged(200)),
            # Terminal intake: fresh, stale-and-unlinked (should purge), stale-but-linked (should NOT purge).
            TerminalIntakeRecord(terminal_session_id="t-fresh", selected_mode="automator", created_at=_aged(10)),
            TerminalIntakeRecord(terminal_session_id="t-stale-orphan", selected_mode="automator", created_at=_aged(365)),
            TerminalIntakeRecord(terminal_session_id="t-stale-linked", selected_mode="automator", conversation_id="conv-fresh", created_at=_aged(365)),
            # Contact notifications.
            ContactRecord(id="contact-1", role="practitioner", name="X", email="x@example.com", data='{"note":"private"}', ip_address="9.9.9.9", created_at=_aged(800)),
            ContactNotificationRecord(contact_id="contact-1", status="sent", created_at=_aged(10)),
            ContactNotificationRecord(contact_id="contact-1", status="sent", created_at=_aged(200)),
            issue,
            subscriber,
            SubscriberAutoReplyRecord(subscriber_id="sub-1", contact_id="contact-1", role="student", template="student_v1", status="sent", created_at=_aged(10)),
            SubscriberAutoReplyRecord(subscriber_id="sub-1", contact_id="contact-1", role="student", template="student_v1", status="sent", created_at=_aged(45)),
            NewsletterDelivery(issue_id="issue-1", subscriber_id="sub-1", channel="email", status="sent", created_at=_aged(10)),
            NewsletterDelivery(issue_id="issue-1", subscriber_id="sub-1", channel="email", status="sent", created_at=_aged(120)),
            NewsletterDelivery(issue_id="issue-1", subscriber_id="", channel="web", status="sent", created_at=_aged(120)),
        ])
        await session.commit()


async def _count(session_maker, model) -> int:
    from sqlalchemy import func, select
    async with session_maker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _one(session_maker, model):
    from sqlalchemy import select
    async with session_maker() as session:
        return (await session.execute(select(model))).scalars().one()


@pytest.mark.asyncio
async def test_sweep_deletes_only_stale_records(session_maker):
    await _seed(session_maker)
    policy = RetentionPolicy()

    summary = await sweep(policy, session_maker, now=_now())

    assert isinstance(summary, RetentionSweepSummary)
    assert summary.dry_run is False
    assert summary.chat_conversations_deleted == 1
    assert summary.chat_messages_deleted == 1
    assert summary.chat_intake_deleted == 1
    assert summary.chat_usage_deleted == 2  # 1 linked stale + 1 orphan stale
    assert summary.terminal_intake_deleted == 1  # only the unlinked-stale one
    assert summary.contact_notifications_deleted == 1
    assert summary.subscriber_autoreplies_deleted == 1
    assert summary.newsletter_deliveries_deleted == 1
    assert summary.contact_records_anonymized == 1

    # Verify the fresh records are still present.
    assert await _count(session_maker, Conversation) == 1
    assert await _count(session_maker, MessageRecord) == 1
    assert await _count(session_maker, IntakeRecord) == 1
    assert await _count(session_maker, ChatUsageRecord) == 1
    # Two terminal intake rows remain: the fresh one and the stale-but-linked one.
    assert await _count(session_maker, TerminalIntakeRecord) == 2
    assert await _count(session_maker, ContactNotificationRecord) == 1
    assert await _count(session_maker, SubscriberAutoReplyRecord) == 1
    # Two delivery rows remain: fresh subscriber email + old non-subscriber web publication row.
    assert await _count(session_maker, NewsletterDelivery) == 2
    # ContactRecord is minimized, not purged.
    assert await _count(session_maker, ContactRecord) == 1
    contact = await _one(session_maker, ContactRecord)
    assert contact.name == "[deleted]"
    assert contact.email == "deleted+contact-1@anon.invalid"
    assert contact.data == "{}"
    assert contact.ip_address == ""


@pytest.mark.asyncio
async def test_dry_run_reports_counts_without_deleting(session_maker):
    await _seed(session_maker)
    policy = RetentionPolicy()

    summary = await sweep(policy, session_maker, dry_run=True, now=_now())

    assert summary.dry_run is True
    assert summary.chat_conversations_deleted == 1
    assert summary.terminal_intake_deleted == 1
    assert summary.contact_notifications_deleted == 1
    assert summary.subscriber_autoreplies_deleted == 1
    assert summary.newsletter_deliveries_deleted == 1
    assert summary.contact_records_anonymized == 1

    # Nothing actually deleted.
    assert await _count(session_maker, Conversation) == 2
    assert await _count(session_maker, MessageRecord) == 2
    assert await _count(session_maker, TerminalIntakeRecord) == 3
    assert await _count(session_maker, ContactNotificationRecord) == 2
    assert await _count(session_maker, SubscriberAutoReplyRecord) == 2
    assert await _count(session_maker, NewsletterDelivery) == 3
    contact = await _one(session_maker, ContactRecord)
    assert contact.email == "x@example.com"


@pytest.mark.asyncio
async def test_zero_days_policy_skips_table(session_maker):
    """`days=0` is interpreted as 'delete nothing for this table', not 'delete everything'."""
    await _seed(session_maker)
    policy = RetentionPolicy(
        chat_days=0,
        terminal_intake_days=0,
        contact_notification_days=0,
        subscriber_autoreply_days=0,
        newsletter_delivery_days=0,
        contact_record_days=0,
    )

    summary = await sweep(policy, session_maker, now=_now())

    assert summary.chat_conversations_deleted == 0
    assert summary.chat_messages_deleted == 0
    assert summary.terminal_intake_deleted == 0
    assert summary.contact_notifications_deleted == 0
    assert summary.subscriber_autoreplies_deleted == 0
    assert summary.newsletter_deliveries_deleted == 0
    assert summary.contact_records_anonymized == 0
    assert await _count(session_maker, Conversation) == 2
    assert await _count(session_maker, TerminalIntakeRecord) == 3
    assert await _count(session_maker, ContactNotificationRecord) == 2
    assert await _count(session_maker, SubscriberAutoReplyRecord) == 2
    assert await _count(session_maker, NewsletterDelivery) == 3


def test_policy_from_env_uses_defaults_when_unset(monkeypatch):
    for var in (
        "RETENTION_CHAT_DAYS",
        "RETENTION_TERMINAL_INTAKE_DAYS",
        "RETENTION_CONTACT_NOTIFICATION_DAYS",
        "RETENTION_SUBSCRIBER_AUTOREPLY_DAYS",
        "RETENTION_NEWSLETTER_DELIVERY_DAYS",
        "RETENTION_CONTACT_RECORD_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)

    policy = RetentionPolicy.from_env()

    assert policy.chat_days == 30
    assert policy.terminal_intake_days == 30
    assert policy.contact_notification_days == 30
    assert policy.subscriber_autoreply_days == 30
    assert policy.newsletter_delivery_days == 90
    assert policy.contact_record_days == 730


def test_policy_from_env_clamps_negative_to_zero(monkeypatch):
    monkeypatch.setenv("RETENTION_CHAT_DAYS", "-5")
    monkeypatch.setenv("RETENTION_TERMINAL_INTAKE_DAYS", "30")
    monkeypatch.setenv("RETENTION_CONTACT_NOTIFICATION_DAYS", "junk")
    monkeypatch.setenv("RETENTION_SUBSCRIBER_AUTOREPLY_DAYS", "15")
    monkeypatch.setenv("RETENTION_NEWSLETTER_DELIVERY_DAYS", "60")
    monkeypatch.setenv("RETENTION_CONTACT_RECORD_DAYS", "365")

    policy = RetentionPolicy.from_env()

    assert policy.chat_days == 0  # negative clamped
    assert policy.terminal_intake_days == 30
    assert policy.contact_notification_days == 30  # invalid -> default
    assert policy.subscriber_autoreply_days == 15
    assert policy.newsletter_delivery_days == 60
    assert policy.contact_record_days == 365
