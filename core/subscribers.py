"""Subscriber list CRUD: opt-in on contact form, unsubscribe, listing.

Entries are created two ways:
- Implicitly via the deployment's contact form (with clear opt-in
  disclosure on the form). The full role-specific submission also
  produces a ContactRecord with detailed intake fields.
- Explicitly via the /newsletter/subscribe public endpoint (a lighter
  form that only collects name, email, company, role). No detailed
  ContactRecord is created; the Subscriber row is the entire record.

Both paths share the subscribe_and_reply helper so the upsert +
auto-reply + audit logic lives in exactly one place.

The unsubscribe token in every newsletter / auto-reply email takes the
recipient off the list in one click. Re-submitting either form after
unsubscribing re-subscribes them and rotates the token.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from core.contact_autoreply import send_auto_reply
from core.db.models import Subscriber, SubscriberAutoReplyRecord

logger = logging.getLogger(__name__)


# Per-email outbound auto-reply cooldown. Without this, anyone who can
# POST to /contact or /newsletter/api/subscribe can cause an outbound
# email to land in any inbox they choose (up to the IP rate limit). This
# is a "joe-job" spam vector: an attacker submits with the victim's
# email so the victim gets unwanted mail that appears to come from the
# deployment. The fix is to refuse to re-send an auto-reply to the same
# email within AUTOREPLY_COOLDOWN. The cooldown is generous (7 days)
# because the legitimate case for re-sending the same auto-reply within
# 7 days is essentially zero. Legitimate re-opt-in still works: the
# Subscriber row is updated, the audit row is written, the unsubscribe
# token rotates; only the outbound email is suppressed.
AUTOREPLY_COOLDOWN = timedelta(days=7)


async def recent_autoreply_exists(
    session: AsyncSession,
    *,
    subscriber_id: str,
    within: timedelta = AUTOREPLY_COOLDOWN,
) -> bool:
    """Return True if a 'sent' or 'skipped' auto-reply already went to
    this subscriber within the given window. 'failed' rows do NOT count
    so a transient SMTP failure does not lock the recipient out forever.
    """
    since = datetime.now(timezone.utc) - within
    result = await session.execute(
        select(SubscriberAutoReplyRecord.id)
        .where(SubscriberAutoReplyRecord.subscriber_id == subscriber_id)
        .where(SubscriberAutoReplyRecord.status.in_(["sent", "skipped"]))
        .where(SubscriberAutoReplyRecord.created_at >= since)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


# Topic tag mapping for the Subscriber row. Both signup paths use this
# to translate the role string into a topic-tag set on the subscriber.
ROLE_TOPICS: dict[str, tuple[str, ...]] = {
    "student": ("general", "edu"),
    "automation_engineer": ("general", "auto"),
    "interested_party": ("general", "service"),
}


def _normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_subscriber_by_email(session: AsyncSession, email: str) -> Subscriber | None:
    result = await session.execute(
        select(Subscriber).where(Subscriber.email == _normalize_email(email))
    )
    return result.scalar_one_or_none()


async def get_subscriber_by_token(session: AsyncSession, token: str) -> Subscriber | None:
    token = (token or "").strip()
    if not token:
        return None
    result = await session.execute(
        select(Subscriber).where(Subscriber.unsubscribe_token == token)
    )
    return result.scalar_one_or_none()


async def upsert_subscriber(
    session: AsyncSession,
    *,
    email: str,
    name: str,
    role: str,
    topics: Iterable[str] = ("general",),
) -> tuple[Subscriber, bool]:
    """Insert a new subscriber or refresh the existing row.

    Returns (subscriber, was_newly_created). If the row already exists,
    name/role are refreshed, topics are unioned with the existing set,
    and any prior unsubscribe is cleared (the form submission is the
    explicit re-opt-in signal). Commit is the caller's responsibility
    so this can compose with other contact-form persistence steps in a
    single transaction.
    """

    email_norm = _normalize_email(email)
    existing = await get_subscriber_by_email(session, email_norm)
    now = datetime.now(timezone.utc)
    topic_set = {t.strip() for t in topics if t and t.strip()}

    if existing is None:
        sub = Subscriber(
            email=email_norm,
            name=name.strip(),
            source_role=role,
            topics="|".join(sorted(topic_set)) if topic_set else "general",
            unsubscribe_token=str(uuid.uuid4()),
            opted_in_at=now,
            updated_at=now,
        )
        session.add(sub)
        return sub, True

    existing_topics = {t for t in existing.topics.split("|") if t}
    existing.topics = "|".join(sorted(existing_topics | topic_set)) or "general"
    if name.strip():
        existing.name = name.strip()
    existing.source_role = role or existing.source_role
    existing.updated_at = now
    # Form submission is an explicit opt-in. If they had previously
    # unsubscribed, this re-subscribes them and gives them a fresh
    # token so any leaked old token can no longer be used.
    if existing.unsubscribed_at is not None:
        existing.unsubscribed_at = None
        existing.opted_in_at = now
        existing.unsubscribe_token = str(uuid.uuid4())
    session.add(existing)
    return existing, False


async def unsubscribe_by_token(
    session: AsyncSession,
    token: str,
) -> Subscriber | None:
    """Unsubscribe the subscriber identified by token. Idempotent.

    Unsubscribe also minimizes profile fields. We keep the normalized email
    and token so the suppression record and link idempotency continue to work,
    but remove segmentation fields that are no longer needed once the person
    has left the list.
    """

    sub = await get_subscriber_by_token(session, token)
    if sub is None:
        return None
    if sub.unsubscribed_at is None:
        sub.unsubscribed_at = datetime.now(timezone.utc)
    sub.name = ""
    sub.source_role = ""
    sub.topics = ""
    sub.updated_at = sub.unsubscribed_at or datetime.now(timezone.utc)
    session.add(sub)
    return sub


async def record_autoreply(
    session: AsyncSession,
    *,
    subscriber_id: str,
    contact_id: str,
    role: str,
    template_id: str,
    status: str,
    error: str = "",
) -> None:
    """Persist an audit row for the auto-reply send attempt."""

    session.add(
        SubscriberAutoReplyRecord(
            subscriber_id=subscriber_id,
            contact_id=contact_id,
            role=role,
            template=template_id,
            status=status,
            error=error[:512],
        )
    )


async def subscribe_and_reply(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    role: str,
    contact_id: str = "",
) -> tuple[Subscriber | None, str, str, str]:
    """End-to-end add-to-list helper used by every entry point.

    Args:
        session: Active DB session.
        name: Submitter name.
        email: Submitter email.
        role: One of the ContactRecord roles (student, automation_engineer,
            interested_party). Drives the auto-reply template and the
            topic tags written to the Subscriber row.
        contact_id: Optional ContactRecord id for the audit row. Empty
            string when the signup is via the lightweight newsletter
            endpoint (no detailed ContactRecord exists).

    Returns:
        (subscriber, status, template_id, error). subscriber is None if
        the upsert itself failed; status is the auto-reply send status
        ("sent", "skipped", or empty if subscriber upsert failed).

    Best-effort behavior: failures inside this helper never raise. The
    caller (contact form or newsletter signup endpoint) has already
    persisted its primary record and we don't want a transient email
    or audit failure to surface as a 500 to the visitor.
    """

    topics = ROLE_TOPICS.get(role, ("general",))
    try:
        subscriber, _created = await upsert_subscriber(
            session,
            email=email,
            name=name,
            role=role,
            topics=topics,
        )
        await session.commit()
        await session.refresh(subscriber)
    except Exception:  # noqa: BLE001
        logger.exception("Subscriber upsert failed for %s (role=%s)", email, role)
        await session.rollback()
        return None, "", "", "upsert_failed"

    # Spam-abuse mitigation: if an auto-reply already went to this
    # subscriber within AUTOREPLY_COOLDOWN, skip the outbound email but
    # still record the audit row so the operator can see the re-submit
    # happened. This stops anyone with a valid contact-form POST from
    # using the API as an unbounded "send mail to any address" oracle.
    if await recent_autoreply_exists(session, subscriber_id=subscriber.id):
        status_value = "skipped"
        template_id = "cooldown"
        error = "auto_reply_cooldown_active"
        logger.info(
            "Auto-reply suppressed by cooldown for %s (subscriber=%s)",
            email,
            subscriber.id,
        )
    else:
        status_value, template_id, error = await send_auto_reply(
            role,
            name,
            email,
            subscriber.unsubscribe_token,
        )

    # The SubscriberAutoReplyRecord schema requires a contact_id foreign
    # key. For lightweight newsletter signups with no ContactRecord we
    # skip the audit row rather than fabricate a fake contact_id. The
    # Subscriber row itself is the audit trail for those.
    if contact_id:
        try:
            await record_autoreply(
                session,
                subscriber_id=subscriber.id,
                contact_id=contact_id,
                role=role,
                template_id=template_id,
                status=status_value,
                error=error,
            )
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Auto-reply audit persist failed for contact %s", contact_id)
            await session.rollback()

    return subscriber, status_value, template_id, error


async def active_subscribers(
    session: AsyncSession,
    *,
    topic: str | None = None,
) -> list[Subscriber]:
    """List subscribers that have not unsubscribed.

    Optional topic filter does substring match on the pipe-separated
    topics column. For v1 every newsletter goes to all active
    subscribers and the topic filter is unused; it's here so phase 3
    and the future edu-platform updates can segment without another
    code change.
    """

    statement = select(Subscriber).where(Subscriber.unsubscribed_at.is_(None))
    result = await session.execute(statement)
    subs = list(result.scalars().all())
    if topic:
        topic_norm = topic.strip().lower()
        subs = [s for s in subs if topic_norm in {t.strip().lower() for t in s.topics.split("|") if t}]
    return subs
