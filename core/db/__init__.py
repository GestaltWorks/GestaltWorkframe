"""Database package: engine, models, migrations, CRUD helpers.

Historically this lived in a single 543-line core/db.py. The split is
purely organizational - every public symbol is re-exported from this
package so existing `from core.db import X` imports keep working.

Module layout:
- engine.py:     async engine, session maker, env-driven URL, get_session
- models.py:     all SQLModel table classes plus DISCOVERY_AUDIT_* constants
- migrations.py: PRAGMA-based additive migrations + init_db
- crud.py:       conversation/intake/message/usage data access helpers
"""

from core.db.crud import (
    add_chat_usage_event,
    add_chat_usage_event_in_new_session,
    add_message,
    add_message_in_new_session,
    chat_usage_snapshot,
    create_conversation,
    get_conversation,
    get_messages,
    save_intake_record,
    save_terminal_intake_submission,
)
from core.db.engine import (
    DEFAULT_SQLITE_PATH,
    async_session_maker,
    database_url_from_env,
    engine,
    get_session,
    sqlite_url,
)
from core.db.migrations import init_db
from core.db.models import (
    ChatUsageRecord,
    ContactNotificationRecord,
    ContactRecord,
    Conversation,
    DISCOVERY_AUDIT_LIBRARY_PROMOTED,
    DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED,
    DISCOVERY_AUDIT_EVENTS,
    DISCOVERY_AUDIT_FIND_DECISION,
    DISCOVERY_AUDIT_FIND_SEEN,
    DISCOVERY_AUDIT_FIND_UNPUBLISHED,
    DISCOVERY_AUDIT_KB_PURGED,
    DISCOVERY_AUDIT_POLL_FAILED,
    DISCOVERY_AUDIT_POLL_STARTED,
    DISCOVERY_AUDIT_POLL_SUCCEEDED,
    DISCOVERY_AUDIT_SOURCE_ADDED,
    DISCOVERY_AUDIT_SOURCE_UPDATED,
    DiscoveryAudit,
    DiscoveryFind,
    DiscoverySource,
    IntakeRecord,
    MessageRecord,
    NewsletterDelivery,
    NewsletterIssue,
    Subscriber,
    SubscriberAutoReplyRecord,
    TerminalIntakeRecord,
)

__all__ = [
    # engine
    "DEFAULT_SQLITE_PATH",
    "async_session_maker",
    "database_url_from_env",
    "engine",
    "get_session",
    "sqlite_url",
    # models
    "ChatUsageRecord",
    "ContactNotificationRecord",
    "ContactRecord",
    "Conversation",
    "DiscoveryAudit",
    "DiscoveryFind",
    "DiscoverySource",
    "IntakeRecord",
    "MessageRecord",
    "NewsletterDelivery",
    "NewsletterIssue",
    "Subscriber",
    "SubscriberAutoReplyRecord",
    "TerminalIntakeRecord",
    # audit constants
    "DISCOVERY_AUDIT_LIBRARY_PROMOTED",
    "DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED",
    "DISCOVERY_AUDIT_EVENTS",
    "DISCOVERY_AUDIT_FIND_DECISION",
    "DISCOVERY_AUDIT_FIND_SEEN",
    "DISCOVERY_AUDIT_FIND_UNPUBLISHED",
    "DISCOVERY_AUDIT_KB_PURGED",
    "DISCOVERY_AUDIT_POLL_FAILED",
    "DISCOVERY_AUDIT_POLL_STARTED",
    "DISCOVERY_AUDIT_POLL_SUCCEEDED",
    "DISCOVERY_AUDIT_SOURCE_ADDED",
    "DISCOVERY_AUDIT_SOURCE_UPDATED",
    # migrations
    "init_db",
    # crud
    "add_chat_usage_event",
    "add_chat_usage_event_in_new_session",
    "add_message",
    "add_message_in_new_session",
    "chat_usage_snapshot",
    "create_conversation",
    "get_conversation",
    "get_messages",
    "save_intake_record",
    "save_terminal_intake_submission",
]
