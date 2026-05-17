"""Retention policy: scheduled purge/minimization of stale operational records.

The public site collects operational data for chat, intake, contact,
newsletter, and delivery workflows. None of it is needed indefinitely. This
module provides scoped helpers that delete short-lived records and minimize
older business records after configurable windows.

What this module deliberately does NOT delete outright:
- ContactRecord: business records of people who reached out. Stale rows are
  anonymized after the configured window so foreign keys and aggregate counts
  survive without retaining personal contact details.
- DiscoverySource / DiscoveryFind / DiscoveryAudit: corpus state. Old
  findings are a record of what was approved/rejected and feed library.
  Pruning would lose discovery history.

All windows are configurable via env. Defaults reflect the privacy
policy text and are conservative (longer rather than shorter), so a
deploy with the defaults can never violate a stricter promised window
without a deliberate env override.

Usage:
    from core.retention import RetentionPolicy, sweep
    summary = await sweep(RetentionPolicy.from_env(), async_session_maker)

The sweep is idempotent and read-write safe to run concurrently with
the API: SQLite's BEGIN IMMEDIATE in the async session serializes
writers, and the queries are bounded by a created_at index.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from core.db.models import (
    ChatUsageRecord,
    ContactNotificationRecord,
    ContactRecord,
    Conversation,
    IntakeRecord,
    MessageRecord,
    NewsletterDelivery,
    SubscriberAutoReplyRecord,
    TerminalIntakeRecord,
)


logger = logging.getLogger(__name__)


# Progressive privacy defaults. Override via env only when the public privacy
# policy or a DPA allows the longer window.
DEFAULT_CHAT_RETENTION_DAYS = 30
DEFAULT_TERMINAL_INTAKE_RETENTION_DAYS = 30
DEFAULT_CONTACT_NOTIFICATION_RETENTION_DAYS = 30
DEFAULT_SUBSCRIBER_AUTOREPLY_RETENTION_DAYS = 30
DEFAULT_NEWSLETTER_DELIVERY_RETENTION_DAYS = 90
DEFAULT_CONTACT_RECORD_RETENTION_DAYS = 730
ANONYMIZED_CONTACT_EMAIL_PREFIX = "deleted+"


def _env_days(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("retention: invalid env value for %s=%r, using default %s", name, raw, default)
        return default
    return max(value, 0)


@dataclass(frozen=True)
class RetentionPolicy:
    """Number of days to keep each operational record type."""

    chat_days: int = DEFAULT_CHAT_RETENTION_DAYS
    terminal_intake_days: int = DEFAULT_TERMINAL_INTAKE_RETENTION_DAYS
    contact_notification_days: int = DEFAULT_CONTACT_NOTIFICATION_RETENTION_DAYS
    subscriber_autoreply_days: int = DEFAULT_SUBSCRIBER_AUTOREPLY_RETENTION_DAYS
    newsletter_delivery_days: int = DEFAULT_NEWSLETTER_DELIVERY_RETENTION_DAYS
    contact_record_days: int = DEFAULT_CONTACT_RECORD_RETENTION_DAYS

    @classmethod
    def from_env(cls) -> "RetentionPolicy":
        return cls(
            chat_days=_env_days("RETENTION_CHAT_DAYS", DEFAULT_CHAT_RETENTION_DAYS),
            terminal_intake_days=_env_days("RETENTION_TERMINAL_INTAKE_DAYS", DEFAULT_TERMINAL_INTAKE_RETENTION_DAYS),
            contact_notification_days=_env_days("RETENTION_CONTACT_NOTIFICATION_DAYS", DEFAULT_CONTACT_NOTIFICATION_RETENTION_DAYS),
            subscriber_autoreply_days=_env_days("RETENTION_SUBSCRIBER_AUTOREPLY_DAYS", DEFAULT_SUBSCRIBER_AUTOREPLY_RETENTION_DAYS),
            newsletter_delivery_days=_env_days("RETENTION_NEWSLETTER_DELIVERY_DAYS", DEFAULT_NEWSLETTER_DELIVERY_RETENTION_DAYS),
            contact_record_days=_env_days("RETENTION_CONTACT_RECORD_DAYS", DEFAULT_CONTACT_RECORD_RETENTION_DAYS),
        )

    def snapshot(self) -> dict[str, int]:
        return {
            "chat_days": self.chat_days,
            "terminal_intake_days": self.terminal_intake_days,
            "contact_notification_days": self.contact_notification_days,
            "subscriber_autoreply_days": self.subscriber_autoreply_days,
            "newsletter_delivery_days": self.newsletter_delivery_days,
            "contact_record_days": self.contact_record_days,
        }


@dataclass
class RetentionSweepSummary:
    """Count of records removed per table, plus the run timestamp."""

    ran_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chat_conversations_deleted: int = 0
    chat_messages_deleted: int = 0
    chat_intake_deleted: int = 0
    chat_usage_deleted: int = 0
    terminal_intake_deleted: int = 0
    contact_notifications_deleted: int = 0
    subscriber_autoreplies_deleted: int = 0
    newsletter_deliveries_deleted: int = 0
    contact_records_anonymized: int = 0
    dry_run: bool = False
    policy: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "dry_run": self.dry_run,
            "policy": self.policy,
            "deleted": {
                "conversation": self.chat_conversations_deleted,
                "message": self.chat_messages_deleted,
                "conversation_intake": self.chat_intake_deleted,
                "chat_usage": self.chat_usage_deleted,
                "terminal_intake": self.terminal_intake_deleted,
                "contact_notification": self.contact_notifications_deleted,
                "subscriber_autoreply": self.subscriber_autoreplies_deleted,
                "newsletter_delivery": self.newsletter_deliveries_deleted,
                "contact_record_anonymized": self.contact_records_anonymized,
            },
        }


def _cutoff(days: int, *, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


ANONYMIZED_CONTACT_EMAIL_DOMAIN = "anon.invalid"


def _anonymized_contact_email(record_id: str) -> str:
    return f"{ANONYMIZED_CONTACT_EMAIL_PREFIX}{record_id}@{ANONYMIZED_CONTACT_EMAIL_DOMAIN}"


async def sweep(
    policy: RetentionPolicy,
    session_maker: async_sessionmaker,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RetentionSweepSummary:
    """Delete operational records older than the policy windows.

    A `days=0` policy entry is interpreted as "delete nothing for this table"
    and the corresponding query is skipped, so a misconfigured env cannot
    accidentally wipe everything.

    Order: chat (intake + messages + usage) before conversation parent rows,
    so the foreign key cascade is clean even on backends that don't enforce
    cascades. Terminal intake and contact notifications are independent.
    """

    summary = RetentionSweepSummary(dry_run=dry_run, policy=policy.snapshot(), ran_at=now or datetime.now(timezone.utc))

    async with session_maker() as session:
        if policy.chat_days > 0:
            cutoff = _cutoff(policy.chat_days, now=now)
            conv_ids_q = select(Conversation.id).where(Conversation.created_at < cutoff)
            conv_ids = list((await session.execute(conv_ids_q)).scalars())
            if conv_ids:
                summary.chat_conversations_deleted = len(conv_ids)
                msg_count = (await session.execute(
                    select(func.count()).select_from(MessageRecord).where(MessageRecord.conversation_id.in_(conv_ids))
                )).scalar_one()
                intake_count = (await session.execute(
                    select(func.count()).select_from(IntakeRecord).where(IntakeRecord.conversation_id.in_(conv_ids))
                )).scalar_one()
                usage_count = (await session.execute(
                    select(func.count()).select_from(ChatUsageRecord).where(ChatUsageRecord.conversation_id.in_(conv_ids))
                )).scalar_one()
                summary.chat_messages_deleted = int(msg_count)
                summary.chat_intake_deleted = int(intake_count)
                summary.chat_usage_deleted = int(usage_count)
                if not dry_run:
                    await session.execute(delete(MessageRecord).where(MessageRecord.conversation_id.in_(conv_ids)))
                    await session.execute(delete(IntakeRecord).where(IntakeRecord.conversation_id.in_(conv_ids)))
                    # ChatUsageRecord rows may reference a conversation OR be orphan (no conversation_id).
                    # Delete the linked rows alongside the conversation; orphan-usage rows are bounded by their own age cutoff.
                    await session.execute(delete(ChatUsageRecord).where(ChatUsageRecord.conversation_id.in_(conv_ids)))
                    await session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
            # Orphan chat_usage rows (no conversation_id) older than the cutoff still need pruning.
            orphan_q = select(func.count()).select_from(ChatUsageRecord).where(
                ChatUsageRecord.conversation_id.is_(None),
                ChatUsageRecord.created_at < cutoff,
            )
            orphan_count = int((await session.execute(orphan_q)).scalar_one())
            summary.chat_usage_deleted += orphan_count
            if orphan_count and not dry_run:
                await session.execute(delete(ChatUsageRecord).where(
                    ChatUsageRecord.conversation_id.is_(None),
                    ChatUsageRecord.created_at < cutoff,
                ))

        if policy.terminal_intake_days > 0:
            cutoff = _cutoff(policy.terminal_intake_days, now=now)
            # Unlinked terminal intake only. Once linked to a contact or conversation,
            # the record is a downstream business artifact and the chat/contact
            # retention windows control its lifecycle.
            terminal_q = select(func.count()).select_from(TerminalIntakeRecord).where(
                TerminalIntakeRecord.created_at < cutoff,
                TerminalIntakeRecord.conversation_id.is_(None),
                TerminalIntakeRecord.contact_id.is_(None),
            )
            terminal_count = int((await session.execute(terminal_q)).scalar_one())
            summary.terminal_intake_deleted = terminal_count
            if terminal_count and not dry_run:
                await session.execute(delete(TerminalIntakeRecord).where(
                    TerminalIntakeRecord.created_at < cutoff,
                    TerminalIntakeRecord.conversation_id.is_(None),
                    TerminalIntakeRecord.contact_id.is_(None),
                ))

        if policy.contact_notification_days > 0:
            cutoff = _cutoff(policy.contact_notification_days, now=now)
            notif_q = select(func.count()).select_from(ContactNotificationRecord).where(
                ContactNotificationRecord.created_at < cutoff,
            )
            notif_count = int((await session.execute(notif_q)).scalar_one())
            summary.contact_notifications_deleted = notif_count
            if notif_count and not dry_run:
                await session.execute(delete(ContactNotificationRecord).where(
                    ContactNotificationRecord.created_at < cutoff,
                ))

        if policy.subscriber_autoreply_days > 0:
            cutoff = _cutoff(policy.subscriber_autoreply_days, now=now)
            autoreply_q = select(func.count()).select_from(SubscriberAutoReplyRecord).where(
                SubscriberAutoReplyRecord.created_at < cutoff,
            )
            autoreply_count = int((await session.execute(autoreply_q)).scalar_one())
            summary.subscriber_autoreplies_deleted = autoreply_count
            if autoreply_count and not dry_run:
                await session.execute(delete(SubscriberAutoReplyRecord).where(
                    SubscriberAutoReplyRecord.created_at < cutoff,
                ))

        if policy.newsletter_delivery_days > 0:
            cutoff = _cutoff(policy.newsletter_delivery_days, now=now)
            delivery_q = select(func.count()).select_from(NewsletterDelivery).where(
                NewsletterDelivery.subscriber_id != "",
                NewsletterDelivery.created_at < cutoff,
            )
            delivery_count = int((await session.execute(delivery_q)).scalar_one())
            summary.newsletter_deliveries_deleted = delivery_count
            if delivery_count and not dry_run:
                await session.execute(delete(NewsletterDelivery).where(
                    NewsletterDelivery.subscriber_id != "",
                    NewsletterDelivery.created_at < cutoff,
                ))

        if policy.contact_record_days > 0:
            cutoff = _cutoff(policy.contact_record_days, now=now)
            records = list((await session.execute(
                select(ContactRecord).where(
                    ContactRecord.created_at < cutoff,
                    ContactRecord.email.not_like(f"{ANONYMIZED_CONTACT_EMAIL_PREFIX}%@{ANONYMIZED_CONTACT_EMAIL_DOMAIN}"),
                )
            )).scalars())
            summary.contact_records_anonymized = len(records)
            if records and not dry_run:
                touched_at = now or datetime.now(timezone.utc)
                for record in records:
                    record.name = "[deleted]"
                    record.email = _anonymized_contact_email(record.id)
                    record.data = "{}"
                    record.ip_address = ""
                    record.updated_at = touched_at
                    session.add(record)

        if not dry_run:
            await session.commit()

    if not dry_run:
        logger.info("retention sweep complete: %s", summary.to_dict())
    return summary
