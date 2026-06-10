"""SQLModel table definitions and audit-event constants.

Importing this module is enough to register every table with SQLModel's
metadata; `migrations.init_db` reads that registry. Keep schema changes
additive (new columns with defaults) so the PRAGMA-based migration helpers
can apply them in place to existing SQLite deployments.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class Conversation(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    mode: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IntakeRecord(SQLModel, table=True):
    __tablename__ = "conversation_intake"
    __table_args__ = (Index("ix_conversation_intake_mode_created", "selected_mode", "created_at"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    selected_mode: str = Field(index=True)
    objective: str = Field(default="")
    building: str = Field(default="")
    maturity: str = Field(default="")
    help_needed: str = Field(default="")
    data: str = Field(default="{}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = Field(default=None)


class TerminalIntakeRecord(SQLModel, table=True):
    __tablename__ = "terminal_intake"
    __table_args__ = (
        UniqueConstraint("terminal_session_id", name="uq_terminal_intake_session"),
        Index("ix_terminal_intake_session_created", "terminal_session_id", "created_at"),
        Index("ix_terminal_intake_mode_created", "selected_mode", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    terminal_session_id: str = Field(index=True)
    conversation_id: str | None = Field(default=None, foreign_key="conversation.id", index=True)
    contact_id: str | None = Field(default=None, foreign_key="contactrecord.id", index=True)
    selected_mode: str = Field(index=True)
    objective: str = Field(default="")
    building: str = Field(default="")
    maturity: str = Field(default="")
    help_needed: str = Field(default="")
    source_path: str = Field(default="")
    referrer: str = Field(default="")
    user_agent: str = Field(default="")
    ip_address: str = Field(default="", index=True)
    data: str = Field(default="{}")  # Raw sanitized answer payload for later handoff templates.
    submission_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = Field(default=None)


class MessageRecord(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    conversation_id: str = Field(foreign_key="conversation.id", index=True)
    role: str
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatUsageRecord(SQLModel, table=True):
    __tablename__ = "chat_usage"
    __table_args__ = (
        Index("ix_chat_usage_ip_created", "ip_address", "created_at"),
        Index("ix_chat_usage_session_created", "session_key", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    ip_address: str = Field(default="", index=True)
    session_key: str = Field(default="", index=True)
    conversation_id: str | None = Field(default=None, foreign_key="conversation.id", index=True)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContactRecord(SQLModel, table=True):
    __table_args__ = (Index("ix_contactrecord_email_role", "email", "role"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    role: str
    name: str
    email: str = Field(index=True)
    data: str = Field(default="{}")  # JSON blob of role-specific fields
    ip_address: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = Field(default=None)


class ContactNotificationRecord(SQLModel, table=True):
    __tablename__ = "contact_notification"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    contact_id: str = Field(foreign_key="contactrecord.id", index=True)
    # Kept for future Discord/webhook notifications without another migration.
    channel: str = Field(default="email")
    status: str
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Newsletter subscriber list.
#
# Created when someone submits the contact form (implicit opt-in with clear
# disclosure on the form). One row per email regardless of how many times
# they submit; the most recent role wins for segmentation. The unsubscribe
# token is a random UUID surfaced in every newsletter email and on the
# /newsletter/unsubscribe page; clicking it sets unsubscribed_at and
# excludes the row from all future sends.
#
# topics is a pipe-separated tag set ("edu|general", "auto|general"). Future
# segmentation (e.g. edu-only updates about the education platform) reads
# this list. Unsubscribing clears the non-essential profile/segmentation
# fields and leaves only the suppression email, token, and timestamps.
#
# No PII beyond name + email is stored here. Contact-form custom fields
# stay on ContactRecord; this table is the audience list only.
class Subscriber(SQLModel, table=True):
    __tablename__ = "subscriber"
    __table_args__ = (
        UniqueConstraint("email", name="uq_subscriber_email"),
        UniqueConstraint("unsubscribe_token", name="uq_subscriber_unsubscribe_token"),
        Index("ix_subscriber_active", "unsubscribed_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    email: str = Field(index=True)
    name: str = Field(default="")
    # Pipe-separated topic tags. Future-proofing for per-topic segmentation.
    topics: str = Field(default="general")
    # Last contact-form role this subscriber arrived through. Useful for
    # editorial decisions (skew copy for student-heavy cycles, etc.).
    source_role: str = Field(default="")
    # Random opaque token used to authenticate unsubscribe clicks. Stored
    # in the URL: /newsletter/unsubscribe?token=<uuid>. Random per row so
    # one leaked token only unsubscribes that one address.
    unsubscribe_token: str = Field(default_factory=lambda: str(uuid.uuid4()), index=True)
    opted_in_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Null while active; timestamp once the subscriber clicks unsubscribe.
    # Newsletter send queries filter on unsubscribed_at IS NULL.
    unsubscribed_at: datetime | None = Field(default=None, index=True)
    # Last time we touched this row (re-opted-in via a new form submit
    # after a prior unsubscribe, or last successful delivery confirmation).
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Newsletter issue: one row per 10-day cycle. Composer snapshots the
# newsletter_pending DiscoveryFinds into finds_json at draft time so the
# operator can take their time on the editorial without the find list
# shifting underneath them. status transitions: draft -> awaiting_approval
# -> approved -> sending -> sent (or skipped if the cycle had no pending
# finds). Issue numbers are now ship-gated: ship_number is assigned only
# at successful send time, and display_label is sticky-from-creation
# (e.g. "0a", "0b", "1", "1a", ...) so unsent drafts never consume a real
# issue number. See core/newsletter.py::next_display_label for the
# label-computer rule.
class NewsletterIssue(SQLModel, table=True):
    __tablename__ = "newsletter_issue"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_newsletter_issue_slug"),
        UniqueConstraint("display_label", name="uq_newsletter_issue_display_label"),
        UniqueConstraint("ship_number", name="uq_newsletter_issue_ship_number"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    # Monotonic integer assigned ONLY at successful send. Null while the
    # issue is a draft / awaiting_approval / approved / sending / skipped.
    # This is the number readers see ("Issue 7"). The atomic increment
    # lives in _dispatch_issue so two concurrent dispatches can't collide.
    ship_number: int | None = Field(default=None, index=True)
    # Sticky display identifier set at creation. For unsent issues it
    # carries a base-26 letter suffix anchored to the current
    # max(ship_number) at creation time (e.g. "0a", "0b", "1c"). At send
    # time _dispatch_issue overwrites this with str(ship_number) so the
    # public label transitions from "1c" -> "2".
    display_label: str = Field(default="", index=True)
    # Human-friendly URL slug, e.g. "2026-05-15-cycle". Used in /library/latest/<slug>.
    slug: str = Field(index=True)
    # Window the issue covers. Both timestamps are at compose time, not
    # send time, so editorial decisions can shift the actual send date
    # without distorting the window.
    period_start: datetime
    period_end: datetime
    # draft | awaiting_approval | approved | sent | skipped
    status: str = Field(default="draft", index=True)
    # Operator-authored markdown intro shown above the find cards. Empty
    # is allowed; renderers handle that case.
    editorial_markdown: str = Field(default="")
    # Snapshot of included find rows at compose time. JSON array of
    # _serialize_public_find dicts so the issue is immutable after compose
    # regardless of what happens to the underlying find rows later.
    finds_json: str = Field(default="[]")
    # Subject line for the email send. Generated by the composer; the
    # operator can override before approval.
    subject: str = Field(default="")
    approved_by: str = Field(default="")
    approved_at: datetime | None = Field(default=None)
    # Operator's intended send date, set when the issue is created.
    # Defaults to last_issue.target_send_at + 10 days for auto-paced
    # cycles. Editable until the operator approves. The daily cron
    # uses this timestamp to send the approval reminder 24h ahead.
    target_send_at: datetime | None = Field(default=None, index=True)
    # When the approval-reminder email was last sent for this issue.
    # Used to de-dupe so the daily cron doesn't spam the operator if
    # they don't react immediately.
    approval_email_sent_at: datetime | None = Field(default=None)
    # When the issue is supposed to ship. Set by the approval endpoint
    # to target_send_at (or now + 30 min if target_send_at is in the
    # past). The dispatcher polls for approved issues whose
    # scheduled_send_at <= now and ships them, flipping status to sent.
    # While status=approved and scheduled_send_at > now the operator
    # can cancel the send, which pops the issue back to awaiting_approval.
    scheduled_send_at: datetime | None = Field(default=None, index=True)
    sent_at: datetime | None = Field(default=None)
    # When set, the issue is hidden from the public archive and the
    # ticker but kept in the DB for audit / re-publish. Only applies
    # to issues that have already shipped (sent or sending). Setting
    # this for a scheduled issue is treated as cancel-and-hide.
    unpublished_at: datetime | None = Field(default=None, index=True)
    # Operator notes that don't show in the public issue (private memo).
    notes: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NewsletterDelivery(SQLModel, table=True):
    """One row per (issue, subscriber, channel) send attempt.

    Channels:
    - email: M365 Graph send to subscriber.email
    - web:   issue published to /library/latest/<slug> (one row per issue)
    - linkedin: LinkedIn post (Phase 7; recorded with the linkedin-post id
      or a manual_paste marker if posted by hand)
    """

    __tablename__ = "newsletter_delivery"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    issue_id: str = Field(foreign_key="newsletter_issue.id", index=True)
    # Empty for web/linkedin (no per-subscriber target).
    subscriber_id: str = Field(default="", index=True)
    channel: str = Field(index=True)
    status: str = Field(default="pending")  # pending | sent | failed | skipped
    sent_at: datetime | None = Field(default=None)
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SubscriberAutoReplyRecord(SQLModel, table=True):
    """Audit log of auto-reply emails sent on contact form submission.

    Mirrors ContactNotificationRecord which logs INTERNAL handoff emails
    (form -> operator). This table logs the OUTBOUND replies (deployment
    -> form submitter) so we can answer 'did this person get a
    confirmation email' from the database, separate from the internal
    handoff status.
    """

    __tablename__ = "subscriber_autoreply"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    subscriber_id: str = Field(foreign_key="subscriber.id", index=True)
    contact_id: str = Field(foreign_key="contactrecord.id", index=True)
    role: str
    template: str
    status: str
    error: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Discovery subsystem tables.
#
# `DiscoverySource` is the persisted runtime state of one watcher declared in
# `kb/watchlist_seed.py`. Static seed fields (description, license, attribution,
# policy) live on the seed dataclass; rows here only carry operational state
# (last poll, etag, status). On boot the scheduler reconciles seed entries into
# rows, inserting new ones and updating cadence/active flips. Findings flow to
# `DiscoveryFind`. Every state transition is audited on `DiscoveryAudit`.
#
# No discovery row holds secret values. Tokens (GitHub PAT, etc.) live in env
# and the provider registry; the row only records that an authenticated request
# was made when relevant.
class DiscoverySource(SQLModel, table=True):
    __tablename__ = "discovery_source"
    __table_args__ = (
        UniqueConstraint("name", name="uq_discovery_source_name"),
        Index("ix_discovery_source_due", "active", "last_polled_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str = Field(index=True)
    watch_type: str = Field(index=True)
    target: str
    refresh_interval_seconds: int = Field(default=86400)
    importance_floor: str = Field(default="normal")
    active: bool = Field(default=True, index=True)
    last_polled_at: datetime | None = Field(default=None, index=True)
    last_status: str = Field(default="")
    last_error: str = Field(default="")
    notes: str = Field(default="")
    etag: str = Field(default="")
    last_modified: str = Field(default="")
    consecutive_failures: int = Field(default=0)
    # Curation flag: when true, this source is highlighted on the public /library
    # surface and is eligible for source-level feature treatment (spotlight,
    # newsletter intros). Operator-controlled via the admin discovery panel.
    # The auto-ingest pipeline runs regardless of this flag.
    featured: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime | None = Field(default=None)


class DiscoveryFind(SQLModel, table=True):
    __tablename__ = "discovery_find"
    __table_args__ = (
        UniqueConstraint(
            "discovery_source_id",
            "external_id",
            name="uq_discovery_find_source_external",
        ),
        Index("ix_discovery_find_status_created", "status", "created_at"),
        Index("ix_discovery_find_source_created", "discovery_source_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    discovery_source_id: str = Field(foreign_key="discovery_source.id", index=True)
    finding_type: str = Field(index=True)
    external_id: str  # Stable identifier for dedup: GitHub release id, RSS guid, etc.
    title: str
    url: str
    summary_text: str = Field(default="")
    raw_payload: str = Field(default="{}")  # JSON-serialized handler payload
    canonical_document_json: str = Field(default="")
    importance_signal: str = Field(default="normal")  # low | normal | high
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = Field(default="pending", index=True)  # pending | approved | rejected | withdrawn | merged_into_corpus | published
    decision_notes: str = Field(default="")
    reviewer: str = Field(default="")
    decided_at: datetime | None = Field(default=None)
    ingested_into_chroma: bool = Field(default=False)
    published_to_library_repo: bool = Field(default=False)
    library_target_path: str = Field(default="")
    library_file_url: str = Field(default="")
    library_promotion_error: str = Field(default="")
    promoted_at: datetime | None = Field(default=None)
    # Legacy curation flag, retained for backwards compatibility with
    # serializers and tests that read it. The Phase 2 model splits curation
    # into two purpose-specific flags below; the migration helper backfills
    # ticker_featured=True for any row where this legacy featured=True. New
    # code should set ticker_featured / newsletter_pending directly and treat
    # this field as a deprecated mirror.
    featured: bool = Field(default=False, index=True)
    featured_at: datetime | None = Field(default=None)

    # Phase 2 curation split:
    #
    # ticker_featured: this find appears in the public LibraryUpdatesTicker
    #   for a rolling 30-day window starting at ticker_featured_at. After
    #   30 days the public serializer filters it out automatically; the row
    #   stays curated in the admin view so re-featuring is trivial.
    # ticker_featured_at: timestamp the ticker_featured flag was last set
    #   to True; used to compute the 30-day expiry. Set by the
    #   /admin/api/discovery/finds/{id}/ticker-feature endpoint.
    # newsletter_pending: this find is queued for the next newsletter
    #   issue. When an issue is approved and sent, all finds it included
    #   have newsletter_pending flipped back to False so the next cycle
    #   sees only fresh material.
    # dismissed: admin has explicitly dismissed this find as not worth
    #   curating. Used to stop the "new content" badge on the source card
    #   from re-flagging the same row over and over.
    ticker_featured: bool = Field(default=False, index=True)
    ticker_featured_at: datetime | None = Field(default=None)
    newsletter_pending: bool = Field(default=False, index=True)
    dismissed: bool = Field(default=False, index=True)

    # Per-issue assignment. When set, this find is tagged for a specific
    # NewsletterIssue. The find shows up in that issue's Compose view;
    # the operator can untag it (clearing this column) or move it to a
    # different issue. After the issue is sent, the FK stays for
    # historical traceability ("what was in issue #5?") but the find
    # also has published_in_newsletter_at stamped so downstream surfaces
    # have a clean timestamp to filter on.
    #
    # The legacy `newsletter_pending` boolean above is now derived: any
    # find with a non-null newsletter_issue_id whose issue status is
    # still draft / awaiting_approval / approved is "pending" for the
    # operator's purposes. We keep the boolean column for one rev so
    # existing serializers and tests don't break; new code should read
    # newsletter_issue_id directly.
    newsletter_issue_id: str | None = Field(
        default=None,
        foreign_key="newsletter_issue.id",
        index=True,
    )
    # Stamped automatically when a NewsletterIssue containing this find
    # is approved+sent. Drives the public ticker: items with a non-null
    # timestamp inside the trailing 30 days appear (max 10, newest
    # first). The per-find ticker_featured flag above is now a deprecated
    # mirror that older code paths still reference; new logic should
    # treat published_in_newsletter_at as the source of truth.
    published_in_newsletter_at: datetime | None = Field(default=None, index=True)

    # Category rollup. For sources that emit one row per leaf file
    # (currently github_repo_artifact_scan), `category` is the first path
    # segment under the source root. The handler writes one find per
    # category instead of one per file; the leaf list lives in
    # raw_payload.children. Empty string for sources where each find is
    # already its own first-class signal (RSS posts, GitHub releases,
    # subreddit posts, etc.).
    category: str = Field(default="")
    # Number of leaf items represented by this category find. 0 for
    # non-rollup rows. The admin UI surfaces this as "N files".
    child_count: int = Field(default=0)
    # When the upstream source last changed. For repos: latest commit
    # date touching the category folder. For RSS: post pub_date. For
    # releases: release published_at. Distinct from last_seen_at which
    # is when the scheduler last polled successfully.
    last_upstream_updated_at: datetime | None = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


DISCOVERY_AUDIT_SOURCE_ADDED = "source_added"
DISCOVERY_AUDIT_SOURCE_UPDATED = "source_updated"
DISCOVERY_AUDIT_POLL_STARTED = "poll_started"
DISCOVERY_AUDIT_POLL_SUCCEEDED = "poll_succeeded"
DISCOVERY_AUDIT_POLL_FAILED = "poll_failed"
DISCOVERY_AUDIT_FIND_SEEN = "find_seen"
DISCOVERY_AUDIT_FIND_DECISION = "find_decision"
DISCOVERY_AUDIT_LIBRARY_PROMOTED = "library_promoted"
DISCOVERY_AUDIT_FIND_UNPUBLISHED = "find_unpublished"
DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED = "library_unpublished"
DISCOVERY_AUDIT_KB_PURGED = "kb_purged"

DISCOVERY_AUDIT_EVENTS = frozenset(
    {
        DISCOVERY_AUDIT_SOURCE_ADDED,
        DISCOVERY_AUDIT_SOURCE_UPDATED,
        DISCOVERY_AUDIT_POLL_STARTED,
        DISCOVERY_AUDIT_POLL_SUCCEEDED,
        DISCOVERY_AUDIT_POLL_FAILED,
        DISCOVERY_AUDIT_FIND_SEEN,
        DISCOVERY_AUDIT_FIND_DECISION,
        DISCOVERY_AUDIT_LIBRARY_PROMOTED,
        DISCOVERY_AUDIT_FIND_UNPUBLISHED,
        DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED,
        DISCOVERY_AUDIT_KB_PURGED,
    }
)


class DiscoveryAudit(SQLModel, table=True):
    __tablename__ = "discovery_audit"
    __table_args__ = (
        Index("ix_discovery_audit_find_created", "find_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    find_id: str | None = Field(default=None, foreign_key="discovery_find.id", index=True)
    source_id: str | None = Field(default=None, foreign_key="discovery_source.id", index=True)
    event_type: str  # one of DISCOVERY_AUDIT_EVENTS
    actor: str = Field(default="scheduler")  # scheduler | reviewer:<id> | api | scout
    before_state: str = Field(default="")
    after_state: str = Field(default="")
    reason: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
