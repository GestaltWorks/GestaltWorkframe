"""Discovery queue operations.

Minimal read/list/decision API for M1: no UI yet, so this module is what the
CLI runner and admin endpoint use to list finds and (later) apply decisions.
The full admin queue page in M2 builds on the same primitives.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from core.db import (
    DISCOVERY_AUDIT_LIBRARY_PROMOTED,
    DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED,
    DISCOVERY_AUDIT_FIND_DECISION,
    DISCOVERY_AUDIT_FIND_UNPUBLISHED,
    DISCOVERY_AUDIT_KB_PURGED,
    DISCOVERY_AUDIT_SOURCE_ADDED,
    DISCOVERY_AUDIT_SOURCE_UPDATED,
    DiscoveryAudit,
    DiscoveryFind,
    DiscoverySource,
)
from kb.watchlist import WatchedSource, refresh_seconds, validate_watchlist
from kb.library_publisher import delete_library_file, publish_find_to_library
from kb.discovery_ingest import ingest_approved_find_into_chroma, purge_discovery_find_from_chroma
from core.discovery_display import enrich_find_display, enrich_source_display
from core.discovery_summary import enrich_discovery_find


ALLOWED_DECISIONS = frozenset({"approve", "reject"})
# Statuses that surface on the public Latest/Updates feed. Phase A added
# `auto_indexed`: approved-source content that streamed in automatically.
# Featured items can come from any of these statuses; `featured` is a flag
# orthogonal to status used by the curation surface.
PUBLIC_FIND_STATUSES = frozenset({"approved", "published", "auto_indexed"})


async def list_pending_finds(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return pending discovery finds joined with their source metadata."""

    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.status == "pending")
        .order_by(DiscoveryFind.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = result.all()
    return [_serialize_find(find, source) for find, source in rows]


async def list_recent_finds(
    session: AsyncSession,
    *,
    limit: int = 50,
    status: str | None = None,
    include_activity: bool = False,
) -> list[dict[str, Any]]:
    """Return recent finds, optionally filtered by status."""

    limit = max(1, min(limit, 250))
    query_limit = limit if include_activity or status != "pending" else min(limit * 5, 1000)
    statement = (
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .order_by(DiscoveryFind.created_at.desc())
        .limit(query_limit)
    )
    if status == "reviewed":
        statement = statement.where(DiscoveryFind.status.in_(("approved", "published", "withdrawn")))
    elif status:
        statement = statement.where(DiscoveryFind.status == status)
    result = await session.execute(statement)
    rows = result.all()
    finds = [_serialize_find(find, source) for find, source in rows]
    if status == "pending" and not include_activity:
        finds = [find for find in finds if find.get("approval_required")]
    return finds[:limit]


async def list_source_health(session: AsyncSession) -> list[dict[str, Any]]:
    """Return a health snapshot of every discovery source."""

    result = await session.execute(
        select(DiscoverySource).order_by(DiscoverySource.name.asc())
    )
    sources = result.scalars().all()
    return [_serialize_source(source) for source in sources]


async def add_watched_source(
    session: AsyncSession,
    watched_source: WatchedSource,
    *,
    notes: str = "",
    actor: str = "api",
) -> dict[str, Any]:
    """Validate and persist an operator-added discovery source."""

    validate_watchlist([watched_source])
    existing = await session.execute(
        select(DiscoverySource).where(DiscoverySource.name == watched_source.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"Discovery source already exists: {watched_source.name}")

    source = DiscoverySource(
        name=watched_source.name.strip(),
        watch_type=watched_source.watch_type.strip(),
        target=watched_source.target.strip(),
        refresh_interval_seconds=refresh_seconds(watched_source),
        importance_floor=watched_source.importance_floor.strip(),
        active=watched_source.active,
        notes=notes[:2048].strip(),
    )
    session.add(source)
    await session.flush()
    session.add(
        DiscoveryAudit(
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_SOURCE_ADDED,
            actor=actor,
            after_state=_source_audit_state(source),
            reason=notes[:2048].strip(),
        )
    )
    await session.commit()
    await session.refresh(source)
    return _serialize_source(source)


async def update_watched_source(
    session: AsyncSession,
    source_id: str,
    *,
    refresh_interval_seconds: int | None = None,
    active: bool | None = None,
    notes: str | None = None,
    importance_floor: str | None = None,
    actor: str = "api",
) -> dict[str, Any]:
    """Update mutable operator fields on a discovery source."""

    result = await session.execute(select(DiscoverySource).where(DiscoverySource.id == source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise LookupError(f"Discovery source not found: {source_id}")

    if refresh_interval_seconds is not None and refresh_interval_seconds < 300:
        raise ValueError("refresh_interval_seconds must be at least 300")
    if importance_floor is not None and importance_floor not in {"low", "normal", "high"}:
        raise ValueError("importance_floor must be one of low, normal, high")

    before_state = _source_audit_state(source)
    if refresh_interval_seconds is not None:
        source.refresh_interval_seconds = refresh_interval_seconds
    if active is not None:
        source.active = active
    if notes is not None:
        source.notes = notes[:2048].strip()
    if importance_floor is not None:
        source.importance_floor = importance_floor
    source.updated_at = datetime.now(timezone.utc)

    session.add(
        DiscoveryAudit(
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_SOURCE_UPDATED,
            actor=actor,
            before_state=before_state,
            after_state=_source_audit_state(source),
            reason=(notes or "")[:2048].strip(),
        )
    )
    await session.commit()
    await session.refresh(source)
    return _serialize_source(source)


async def decide_find(
    session: AsyncSession,
    find_id: str,
    decision: str,
    *,
    reviewer: str,
    notes: str = "",
    ingest_into_chroma: bool = False,
    publish_to_library: bool = True,
) -> dict[str, Any]:
    """Apply an approve/reject decision to a pending find."""

    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"Unsupported decision: {decision!r}")

    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row

    find.ingested_into_chroma = False
    if decision == "approve" and publish_to_library and not find.published_to_library_repo:
        try:
            promoted = await publish_find_to_library(find, source, notes=notes)
        except Exception as exc:
            find.library_promotion_error = _error_summary(exc)
            await session.commit()
            raise
        find.published_to_library_repo = True
        find.library_target_path = promoted.path
        find.library_file_url = promoted.public_url or promoted.commit_url
        find.library_promotion_error = ""
        find.promoted_at = datetime.now(timezone.utc)
    before_state = find.status
    find.status = "approved" if decision == "approve" else "rejected"
    if decision == "approve" and ingest_into_chroma:
        await asyncio.to_thread(ingest_approved_find_into_chroma, find, source)
        find.ingested_into_chroma = True
    find.reviewer = reviewer
    find.decision_notes = notes[:2048]
    find.decided_at = datetime.now(timezone.utc)
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before_state,
            after_state=find.status,
            reason=notes[:2048],
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def list_sources_with_activity(
    session: AsyncSession,
    *,
    window_days: int = 30,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """Return approved sources with a rolled-up activity snapshot.

    The Phase A admin surface is curation, not approval. Per-file artifact
    noise is auto-indexed silently; operators don't need to see every diff.
    What they need is "which sources are active, when, and what worth
    featuring did they produce." This helper assembles that view in one pass:

    - Joins sources to their recent finds (status in {auto_indexed, approved,
      published, source_activity}) within `window_days`.
    - Reports total finds, notable-event count (auto_indexed first-class
      events, excluding the source_activity rollups), example titles, and
      the featured flag.
    - Sorted: featured first, then by most-recent activity.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(window_days, 1))
    statement = (
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.last_seen_at >= cutoff)
        .order_by(DiscoveryFind.last_seen_at.desc())
    )
    result = await session.execute(statement)
    rows = result.all()

    by_source: dict[str, dict[str, Any]] = {}
    for find, source in rows:
        bucket = by_source.setdefault(
            source.id,
            {
                "id": source.id,
                "name": source.name,
                "watch_type": source.watch_type,
                "target": source.target,
                "featured": source.featured,
                "active": source.active,
                "last_polled_at": source.last_polled_at.isoformat() if source.last_polled_at else None,
                "last_activity_at": None,
                "total_finds": 0,
                "notable_finds": 0,
                "featured_finds": 0,
                "sample_titles": [],
                "recent_finds": [],
            },
        )
        bucket["total_finds"] += 1
        if find.status in {"auto_indexed", "approved", "published"} and find.finding_type != "new_source_candidate":
            bucket["notable_finds"] += 1
        if find.featured:
            bucket["featured_finds"] += 1
        ts = find.last_seen_at.isoformat()
        if bucket["last_activity_at"] is None or ts > bucket["last_activity_at"]:
            bucket["last_activity_at"] = ts
        if len(bucket["recent_finds"]) < 8 and find.status != "source_activity":
            bucket["recent_finds"].append({
                "id": find.id,
                "title": find.title,
                "url": find.url,
                "finding_type": find.finding_type,
                "status": find.status,
                "featured": find.featured,
                # Phase 2 split: include the purpose-specific flags so the
                # admin panel can render Feature/Queue-newsletter/Dismiss
                # state per find without a second fetch.
                "ticker_featured": find.ticker_featured,
                "newsletter_pending": find.newsletter_pending,
                "dismissed": find.dismissed,
                "importance_signal": find.importance_signal,
                "last_seen_at": ts,
            })
        if len(bucket["sample_titles"]) < 5 and find.title and find.title not in bucket["sample_titles"]:
            bucket["sample_titles"].append(find.title)

    sources_list = list(by_source.values())
    # Two-pass stable sort: order matters. Secondary key first (most recent
    # activity within a feature-group), then primary (featured first).
    sources_list.sort(key=lambda row: row["last_activity_at"] or "", reverse=True)
    sources_list.sort(key=lambda row: 0 if row["featured"] else 1)
    # Add display_name + display_title on each source and its recent_finds so
    # the admin and public UIs render human-readable strings instead of slugs.
    for row in sources_list:
        enrich_source_display(row)
    return sources_list[:limit]


async def set_find_featured(
    session: AsyncSession,
    find_id: str,
    *,
    featured: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Toggle the `featured` flag on a single discovery find.

    Featured finds are the unit of content for the public Latest feed, the
    Updates page, and the upcoming public newsletter. Unfeaturing leaves
    the find in place and ingested; it just stops appearing on curated
    surfaces.
    """
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    before = "featured" if find.featured else "not_featured"
    find.featured = featured
    find.featured_at = datetime.now(timezone.utc) if featured else None
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before,
            after_state="featured" if featured else "not_featured",
            reason="feature_toggle",
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def set_find_ticker_featured(
    session: AsyncSession,
    find_id: str,
    *,
    featured: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Toggle the Phase 2 `ticker_featured` flag on a single find.

    Featured finds appear in the public LibraryUpdatesTicker for a rolling
    30-day window starting at ticker_featured_at. Unfeaturing clears the
    timestamp and removes the entry from the public surface immediately;
    the underlying row stays in place so re-featuring is one click.

    Also keeps the legacy `featured` flag in sync. Older serializers and
    a handful of tests still read `featured`; mirroring it here means we
    can ship Phase 2 without a global rewrite of every consumer.
    """
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    before = "ticker_featured" if find.ticker_featured else "not_ticker_featured"
    now = datetime.now(timezone.utc)
    find.ticker_featured = featured
    find.ticker_featured_at = now if featured else None
    # Legacy mirror — keep `featured` aligned for any reader that hasn't
    # been migrated to the split model yet.
    find.featured = featured
    find.featured_at = now if featured else None
    if featured:
        find.dismissed = False
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before,
            after_state="ticker_featured" if featured else "not_ticker_featured",
            reason="ticker_feature_toggle",
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def set_find_newsletter_pending(
    session: AsyncSession,
    find_id: str,
    *,
    pending: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Queue or unqueue a single find for the next newsletter issue.

    `newsletter_pending=True` means this find is waiting for the next
    composer pass. When an issue is approved and sent, the composer flips
    pending=False on every included find so the following cycle sees
    only fresh material.
    """
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    before = "newsletter_pending" if find.newsletter_pending else "not_newsletter_pending"
    find.newsletter_pending = pending
    if pending:
        find.dismissed = False
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before,
            after_state="newsletter_pending" if pending else "not_newsletter_pending",
            reason="newsletter_queue_toggle",
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def set_find_dismissed(
    session: AsyncSession,
    find_id: str,
    *,
    dismissed: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Mark a find as explicitly reviewed-and-skipped.

    Dismissed finds stop counting toward the "new content" badge on the
    source card. The find stays in the database and remains searchable
    in admin; it just no longer pesters the curator. Setting dismissed
    also clears ticker_featured / newsletter_pending so the row exits
    public surfaces cleanly in one action.
    """
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    before = "dismissed" if find.dismissed else "not_dismissed"
    find.dismissed = dismissed
    if dismissed:
        find.ticker_featured = False
        find.ticker_featured_at = None
        find.newsletter_pending = False
        find.featured = False
        find.featured_at = None
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before,
            after_state="dismissed" if dismissed else "not_dismissed",
            reason="dismiss_toggle",
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def set_source_featured(
    session: AsyncSession,
    source_id: str,
    *,
    featured: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Toggle the `featured` flag on a discovery source.

    Featured sources get spotlight treatment on the public library page
    (Phase C). The auto-ingest pipeline runs regardless of this flag.
    """
    result = await session.execute(select(DiscoverySource).where(DiscoverySource.id == source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise LookupError(f"Discovery source not found: {source_id}")
    before = "featured" if source.featured else "not_featured"
    source.featured = featured
    source.updated_at = datetime.now(timezone.utc)
    session.add(
        DiscoveryAudit(
            find_id=None,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_SOURCE_UPDATED,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before,
            after_state="featured" if featured else "not_featured",
            reason="feature_toggle",
        )
    )
    await session.commit()
    return _serialize_source(source)


async def list_public_latest_finds(
    session: AsyncSession,
    *,
    limit: int = 25,
    offset: int = 0,
    days: int = 15,
) -> list[dict[str, Any]]:
    """Return approved discovery finds safe for the public updates feed.

    Phase 4 removes the per-find raw feed from public surfaces in favor
    of newsletter issue cards, but this helper is also consumed by the
    public LibraryUpdatesTicker and Phase 4 has not landed yet, so the
    function still returns the recent-decided-at window. The Phase 2
    ticker-specific filter lives in `list_ticker_finds` below; the
    ticker should migrate to that helper once Phase 4 ships.
    """

    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.status.in_(PUBLIC_FIND_STATUSES))
        .where(DiscoveryFind.decided_at.is_not(None))
        .where(DiscoveryFind.decided_at >= since)
        .order_by(DiscoveryFind.decided_at.desc(), DiscoveryFind.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return [_serialize_public_find(find, source) for find, source in result.all()]


# Ticker entries live for 30 days from the moment they were published in
# a sent newsletter. Anything older is filtered out of the public ticker
# but stays in the admin view so re-publishing is trivial via the
# newsletter admin.
TICKER_WINDOW_DAYS = 30

# Maximum items the public ticker carries at once. Per the operator's
# spec: "ticker should only live there for 30 days unless bumped off
# (max 10) by newer content." Newer items push older items off the
# visible rail once this cap is hit.
TICKER_MAX_ITEMS = 10


async def list_ticker_finds(
    session: AsyncSession,
    *,
    limit: int = TICKER_MAX_ITEMS,
) -> list[dict[str, Any]]:
    """Return finds for the public LibraryUpdatesTicker.

    Three feature flags are independent in this model:

    - source.featured -> Strong Signals (permanent source-level spotlight,
      rendered as FeaturedSourcePillars)
    - find.ticker_featured + ticker_featured_at -> appears in this ticker
      for 30 days from ticker_featured_at
    - find.published_in_newsletter_at -> included in a sent newsletter
      (drives the newsletter archive, not this ticker)

    The operator's ticker-feature click is the gating signal for ticker
    visibility. Status governs the discovery feed (/library/latest), not
    the ticker; the two surfaces are orthogonal. This means an item can
    be evergreen-pinned on the ticker even after it's been withdrawn
    from the rolling feed, which matches the way operators actually use
    the surface.

    Filter shape:
    - ticker_featured = true
    - ticker_featured_at >= now - TICKER_WINDOW_DAYS
    - dismissed = false
    - status != 'rejected' (keep explicitly-rejected trash off the
      public surface; everything else is the operator's call)
    - ordered by ticker_featured_at desc
    - limit = min(limit, TICKER_MAX_ITEMS)

    Empty result is the correct answer before the operator has curated
    anything. Frontend renders nothing when finds=[].
    """

    cutoff = datetime.now(timezone.utc) - timedelta(days=TICKER_WINDOW_DAYS)
    safe_limit = max(1, min(limit, TICKER_MAX_ITEMS))

    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.ticker_featured == True)  # noqa: E712
        .where(DiscoveryFind.ticker_featured_at.is_not(None))
        .where(DiscoveryFind.ticker_featured_at >= cutoff)
        .where(DiscoveryFind.dismissed == False)  # noqa: E712
        .where(DiscoveryFind.status != "rejected")
        .order_by(DiscoveryFind.ticker_featured_at.desc())
        .limit(safe_limit)
    )
    return [_serialize_public_find(find, source) for find, source in result.all()]


async def list_finds_for_source(
    session: AsyncSession,
    source_id: str,
    *,
    page: int = 1,
    page_size: int = 20,
    days: int | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    """Paginated, filterable find list scoped to one source.

    Powers the admin "drill into source" view added in Phase 2d. Filters:
    - days: only return finds whose first_seen_at is within N days
    - topic: substring match on review_topic (or fall back to finding_type)

    Returns {finds, page, page_size, total, total_pages}. Page is 1-based.
    """

    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    base = (
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.discovery_source_id == source_id)
    )
    count_base = select(DiscoveryFind).where(DiscoveryFind.discovery_source_id == source_id)
    if days is not None and days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=min(days, 3650))
        base = base.where(DiscoveryFind.first_seen_at >= cutoff)
        count_base = count_base.where(DiscoveryFind.first_seen_at >= cutoff)
    if topic:
        like = f"%{topic.lower()}%"
        base = base.where(DiscoveryFind.title.ilike(like) | DiscoveryFind.summary_text.ilike(like))
        count_base = count_base.where(DiscoveryFind.title.ilike(like) | DiscoveryFind.summary_text.ilike(like))

    total_result = await session.execute(select(func.count()).select_from(count_base.subquery()))
    total = int(total_result.scalar_one())

    page_result = await session.execute(
        base.order_by(DiscoveryFind.first_seen_at.desc()).offset(offset).limit(page_size)
    )
    finds = [_serialize_find(find, source) for find, source in page_result.all()]
    return {
        "finds": finds,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }


async def count_uncurated_finds_per_source(session: AsyncSession) -> dict[str, int]:
    """Map of source_id -> count of finds that need operator review.

    A find is "uncurated" (eligible for the New Content badge) when:
    - status is auto_indexed (first-class event that landed automatically)
    - ticker_featured is False
    - newsletter_pending is False
    - dismissed is False

    The admin discovery panel reads this to decide whether to render the
    "New content" badge on each source card.
    """

    result = await session.execute(
        select(DiscoveryFind.discovery_source_id, func.count(DiscoveryFind.id))
        .where(DiscoveryFind.status == "auto_indexed")
        .where(DiscoveryFind.ticker_featured == False)  # noqa: E712
        .where(DiscoveryFind.newsletter_pending == False)  # noqa: E712
        .where(DiscoveryFind.dismissed == False)  # noqa: E712
        .group_by(DiscoveryFind.discovery_source_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


async def unpublish_find_from_latest(
    session: AsyncSession,
    find_id: str,
    *,
    reviewer: str,
    notes: str = "",
) -> dict[str, Any]:
    find, source = await _find_with_source(session, find_id)
    if find.status == "withdrawn":
        return _serialize_find(find, source)
    if find.status not in PUBLIC_FIND_STATUSES:
        raise ValueError("Only public discovery finds can be unpublished from Latest")
    before_state = find.status
    find.status = "withdrawn"
    find.reviewer = reviewer
    find.decision_notes = notes[:2048]
    find.decided_at = datetime.now(timezone.utc)
    session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_FIND_UNPUBLISHED, actor=f"reviewer:{reviewer}" if reviewer else "reviewer", before_state=before_state, after_state=find.status, reason=notes[:2048]))
    await session.commit()
    return _serialize_find(find, source)


async def unpublish_find_from_library(
    session: AsyncSession,
    find_id: str,
    *,
    reviewer: str,
    notes: str = "",
) -> dict[str, Any]:
    find, source = await _find_with_source(session, find_id)
    before_state = json.dumps({"published_to_library_repo": find.published_to_library_repo, "library_target_path": find.library_target_path}, sort_keys=True)
    if find.published_to_library_repo and find.library_target_path:
        try:
            deleted = await delete_library_file(find.library_target_path, title=find.title)
        except Exception as exc:
            find.library_promotion_error = _error_summary(exc)
            session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED, actor=f"reviewer:{reviewer}" if reviewer else "reviewer", before_state=before_state, after_state=f"delete_failed:{find.library_promotion_error}", reason=notes[:2048]))
            await session.commit()
            raise
        after_state = deleted.commit_url or f"deleted:{deleted.path}"
    else:
        after_state = "not_published"
    find.published_to_library_repo = False
    find.library_target_path = ""
    find.library_file_url = ""
    find.library_promotion_error = ""
    session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_LIBRARY_UNPUBLISHED, actor=f"reviewer:{reviewer}" if reviewer else "reviewer", before_state=before_state, after_state=after_state, reason=notes[:2048]))
    await session.commit()
    return _serialize_find(find, source)


async def purge_find_from_kb(
    session: AsyncSession,
    find_id: str,
    *,
    reviewer: str,
    notes: str = "",
) -> dict[str, Any]:
    find, source = await _find_with_source(session, find_id)
    before_state = "ingested" if find.ingested_into_chroma else "not_ingested"
    try:
        await asyncio.to_thread(purge_discovery_find_from_chroma, find.id)
    except Exception as exc:
        error = _error_summary(exc)
        session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_KB_PURGED, actor=f"reviewer:{reviewer}" if reviewer else "reviewer", before_state=before_state, after_state=f"purge_failed:{error}", reason=notes[:2048]))
        await session.commit()
        raise
    find.ingested_into_chroma = False
    session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_KB_PURGED, actor=f"reviewer:{reviewer}" if reviewer else "reviewer", before_state=before_state, after_state="not_ingested", reason=notes[:2048]))
    await session.commit()
    return _serialize_find(find, source)


async def promote_find_to_library(
    session: AsyncSession,
    find_id: str,
    *,
    reviewer: str,
    notes: str = "",
    target_path: str = "",
) -> dict[str, Any]:
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    if find.status not in PUBLIC_FIND_STATUSES:
        raise ValueError("Only approved discovery finds can be promoted to library")
    if find.published_to_library_repo and find.library_file_url:
        return _serialize_find(find, source)

    try:
        promoted = await publish_find_to_library(
            find,
            source,
            notes=notes,
            target_path=target_path,
        )
    except Exception as exc:
        find.library_promotion_error = _error_summary(exc)
        await session.commit()
        raise

    find.published_to_library_repo = True
    find.library_target_path = promoted.path
    find.library_file_url = promoted.public_url or promoted.commit_url
    find.library_promotion_error = ""
    find.promoted_at = datetime.now(timezone.utc)
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_LIBRARY_PROMOTED,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            after_state=promoted.public_url or promoted.commit_url,
            reason=notes[:2048],
        )
    )
    await session.commit()
    return _serialize_find(find, source)


async def _find_with_source(session: AsyncSession, find_id: str) -> tuple[DiscoveryFind, DiscoverySource]:
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    return row


async def promote_find_to_source(
    session: AsyncSession,
    find_id: str,
    *,
    reviewer: str,
    notes: str = "",
    refresh_cadence: str = "daily",
    add_artifact_scan: bool = True,
) -> dict[str, Any]:
    result = await session.execute(
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.id == find_id)
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"Discovery find not found: {find_id}")
    find, source = row
    if find.finding_type != "new_source_candidate":
        raise ValueError("Only new_source_candidate findings can be promoted to watched sources")
    if find.status != "pending":
        raise ValueError("Only pending source candidates can be promoted to watched sources")

    proposal = _source_candidate_payload(find)
    primary = _watched_source_from_proposal(proposal, refresh_cadence)
    planned = [primary]
    if add_artifact_scan and proposal["watch_type"] == "github_repo_watch":
        artifact = _watched_source_from_proposal({**proposal, "name": f"{proposal['name']}_artifacts", "watch_type": "github_repo_artifact_scan"}, refresh_cadence)
        planned.append(artifact)
    validate_watchlist(planned)
    await _ensure_source_names_available(session, [source.name for source in planned])

    created: list[dict[str, Any]] = []
    for watched_source in planned:
        created.append(await add_watched_source(session, watched_source, notes=notes, actor=f"reviewer:{reviewer}"))

    before_state = find.status
    find.status = "approved"
    find.reviewer = reviewer
    find.decision_notes = notes[:2048]
    find.decided_at = datetime.now(timezone.utc)
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            before_state=before_state,
            after_state="approved_source_promoted",
            reason=notes[:2048],
        )
    )
    await session.commit()
    return {"find": _serialize_find(find, source), "sources": created}


def _source_candidate_payload(find: DiscoveryFind) -> dict[str, str]:
    try:
        payload = json.loads(find.raw_payload or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Source candidate payload is invalid JSON") from exc
    proposal = payload.get("proposal") if isinstance(payload, dict) else None
    if not isinstance(proposal, dict):
        raise ValueError("Source candidate payload does not include a proposal")
    name = str(proposal.get("name") or find.title).strip()
    watch_type = str(proposal.get("watch_type") or "").strip()
    target = str(proposal.get("target") or find.url).strip()
    reason = str(proposal.get("reason") or find.summary_text or "").strip()
    if not name or not watch_type or not target:
        raise ValueError("Source candidate proposal requires name, watch_type, and target")
    return {"name": name, "watch_type": watch_type, "target": target, "reason": reason}


def _watched_source_from_proposal(proposal: dict[str, str], refresh_cadence: str) -> WatchedSource:
    name = _source_name(proposal["name"])
    target = proposal["target"].strip()
    watch_type = proposal["watch_type"].strip()
    canonical_url = _candidate_canonical_url(watch_type, target)
    return WatchedSource(
        name=name,
        watch_type=watch_type,
        target=target,
        description=proposal.get("reason") or f"Discovery-promoted source {name}.",
        refresh_cadence=refresh_cadence,
        canonical_url=canonical_url,
        provenance=f"Promoted from discovery source candidate: {canonical_url}",
        license_notes="Link and summarize only unless upstream licensing explicitly permits reuse.",
        attribution="Upstream source owner.",
        trust_tier="public_reviewed_source",
        display_policy="public_after_source_review",
        retrieval_policy="approved_for_grounded_retrieval_after_review",
        curriculum_policy="not_approved_by_default",
        agent_access_policy="read_only_public_source_only",
        secret_handling="no_credentials_required",
        importance_floor="normal",
    )


def _source_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return (slug or "discovery_source")[:80]


def _candidate_canonical_url(watch_type: str, target: str) -> str:
    if watch_type in {"github_repo_watch", "github_repo_artifact_scan"}:
        return f"https://github.com/{target}"
    if watch_type == "subreddit_watch":
        return f"https://www.reddit.com/r/{target.strip('/')}"
    return target


async def _ensure_source_names_available(session: AsyncSession, names: list[str]) -> None:
    result = await session.execute(select(DiscoverySource).where(DiscoverySource.name.in_(names)))
    existing = [source.name for source in result.scalars().all()]
    if existing:
        raise ValueError(f"Discovery source already exists: {', '.join(sorted(existing))}")


# Manual curation: the synthetic DiscoverySource that owns operator-pasted URLs.
# These finds never come from a poll cycle; they exist purely so the operator
# can add a hand-picked URL with an OG/Twitter card preview directly into the
# newsletter pipeline. The row is created lazily on first use and is never
# polled (watch_type "manual_curation" is not in ALLOWED_WATCH_TYPES so the
# scheduler skips it; active=False is belt-and-suspenders on top of that).
MANUAL_CURATION_SOURCE_NAME = "manual_curation"
MANUAL_CURATION_WATCH_TYPE = "manual_curation"


async def _ensure_manual_curation_source(session: AsyncSession) -> DiscoverySource:
    """Get-or-create the singleton DiscoverySource that owns manual paste finds.

    Idempotent. The row is created with active=False so the scheduler never
    schedules a poll on it; the synthetic watch_type would also fail
    validate_watchlist if it ever leaked into seed-driven code.
    """
    result = await session.execute(
        select(DiscoverySource).where(DiscoverySource.name == MANUAL_CURATION_SOURCE_NAME)
    )
    source = result.scalar_one_or_none()
    if source is not None:
        return source
    source = DiscoverySource(
        name=MANUAL_CURATION_SOURCE_NAME,
        watch_type=MANUAL_CURATION_WATCH_TYPE,
        target="manual",
        refresh_interval_seconds=86400,
        importance_floor="normal",
        active=False,
        notes="Synthetic source owning operator-pasted custom URLs for the newsletter pipeline.",
    )
    session.add(source)
    await session.flush()
    session.add(
        DiscoveryAudit(
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_SOURCE_ADDED,
            actor="system:manual_curation_bootstrap",
            after_state=_source_audit_state(source),
            reason="Manual curation singleton",
        )
    )
    return source


async def create_manual_find(
    session: AsyncSession,
    *,
    url: str,
    title: str,
    description: str,
    image_url: str,
    source_label: str,
    queue_for_newsletter: bool,
    reviewer: str,
) -> dict[str, Any]:
    """Persist an operator-pasted URL as a DiscoveryFind under manual_curation.

    The find lands at status="auto_indexed" so it is eligible for the public
    /library/latest feed and the newsletter composer's pending-queue pull. The
    `external_id` is the URL itself, which is unique-constrained against the
    manual_curation source so re-pasting the same URL is a no-op rather than
    a duplicate row.

    Raises ValueError on duplicate (caller surfaces as 409).
    """
    url = url.strip()
    title = title.strip()
    if not url or not title:
        raise ValueError("Manual find requires both url and title")

    source = await _ensure_manual_curation_source(session)

    # Dedup: if the same URL was already pasted, return the existing row
    # rather than failing. The operator pasted twice; that's a no-op, not
    # an error.
    existing = await session.execute(
        select(DiscoveryFind).where(
            DiscoveryFind.discovery_source_id == source.id,
            DiscoveryFind.external_id == url,
        )
    )
    duplicate = existing.scalar_one_or_none()
    if duplicate is not None:
        if queue_for_newsletter and not duplicate.newsletter_pending:
            duplicate.newsletter_pending = True
            duplicate.dismissed = False
            await session.commit()
        return _serialize_find(duplicate, source)

    payload = {
        "manual_curation": True,
        "image_url": image_url.strip(),
        "source_label": source_label.strip(),
        "added_by": reviewer,
    }
    find = DiscoveryFind(
        discovery_source_id=source.id,
        finding_type="manual_url",
        external_id=url,
        title=title[:300],
        url=url,
        summary_text=description.strip()[:2000],
        raw_payload=json.dumps(payload),
        importance_signal="normal",
        status="auto_indexed",
        reviewer=reviewer,
        decided_at=datetime.now(timezone.utc),
        newsletter_pending=bool(queue_for_newsletter),
    )
    session.add(find)
    await session.flush()
    session.add(
        DiscoveryAudit(
            find_id=find.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_DECISION,
            actor=f"reviewer:{reviewer}" if reviewer else "reviewer",
            after_state="manual_added",
            reason="Operator pasted custom URL",
        )
    )
    await session.commit()
    await session.refresh(find)
    return _serialize_find(find, source)


def _error_summary(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {str(exc).splitlines()[0]}"[:512]


def _source_audit_state(source: DiscoverySource) -> str:
    return json.dumps(
        {
            "active": source.active,
            "importance_floor": source.importance_floor,
            "refresh_interval_seconds": source.refresh_interval_seconds,
            "notes": source.notes[:256],
        },
        sort_keys=True,
    )


def _serialize_find(find: DiscoveryFind, source: DiscoverySource) -> dict[str, Any]:
    try:
        payload = json.loads(find.raw_payload) if find.raw_payload else {}
    except json.JSONDecodeError:
        payload = {"_raw": find.raw_payload}
    return enrich_find_display(enrich_discovery_find({
        "id": find.id,
        "discovery_source_id": find.discovery_source_id,
        "source_name": source.name,
        "watch_type": source.watch_type,
        "finding_type": find.finding_type,
        "external_id": find.external_id,
        "title": find.title,
        "url": find.url,
        "summary_text": find.summary_text,
        "importance_signal": find.importance_signal,
        "status": find.status,
        "reviewer": find.reviewer,
        "decision_notes": find.decision_notes,
        "decided_at": find.decided_at.isoformat() if find.decided_at else None,
        "first_seen_at": find.first_seen_at.isoformat(),
        "last_seen_at": find.last_seen_at.isoformat(),
        "ingested_into_chroma": find.ingested_into_chroma,
        "published_to_library_repo": find.published_to_library_repo,
        "library_target_path": find.library_target_path,
        "library_file_url": find.library_file_url,
        "library_promotion_error": find.library_promotion_error,
        "promoted_at": find.promoted_at.isoformat() if find.promoted_at else None,
        "featured": find.featured,
        "featured_at": find.featured_at.isoformat() if find.featured_at else None,
        # Phase 2 curation split. New consumers should read these
        # purpose-specific flags; `featured` is retained as a mirror.
        "ticker_featured": find.ticker_featured,
        "ticker_featured_at": find.ticker_featured_at.isoformat() if find.ticker_featured_at else None,
        "newsletter_pending": find.newsletter_pending,
        "newsletter_issue_id": find.newsletter_issue_id,
        "dismissed": find.dismissed,
        "published_in_newsletter_at": find.published_in_newsletter_at.isoformat() if find.published_in_newsletter_at else None,
        # Category rollup fields. Empty / 0 / None for sources that
        # already emit one signal per row (RSS, releases, etc.); set
        # for github_repo_artifact_scan rollup rows.
        "category": find.category,
        "child_count": find.child_count,
        "last_upstream_updated_at": (
            find.last_upstream_updated_at.isoformat() if find.last_upstream_updated_at else None
        ),
        "source_featured": source.featured,
        "raw_payload": payload,
    }))


def _serialize_source(source: DiscoverySource) -> dict[str, Any]:
    return enrich_source_display({
        "id": source.id,
        "name": source.name,
        "watch_type": source.watch_type,
        "target": source.target,
        "refresh_interval_seconds": source.refresh_interval_seconds,
        "importance_floor": source.importance_floor,
        "active": source.active,
        "featured": source.featured,
        "last_polled_at": source.last_polled_at.isoformat() if source.last_polled_at else None,
        "last_status": source.last_status,
        "last_error": source.last_error,
        "notes": source.notes,
        "consecutive_failures": source.consecutive_failures,
        "etag": bool(source.etag),
        "last_modified": bool(source.last_modified),
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat() if source.updated_at else None,
    })


def _serialize_public_find(find: DiscoveryFind, source: DiscoverySource) -> dict[str, Any]:
    enriched = enrich_find_display(enrich_discovery_find({
        "id": find.id,
        "source_name": source.name,
        "watch_type": source.watch_type,
        "finding_type": find.finding_type,
        "title": find.title,
        "url": find.url,
        "summary_text": find.summary_text,
        "importance_signal": find.importance_signal,
        "decided_at": find.decided_at.isoformat() if find.decided_at else None,
        "first_seen_at": find.first_seen_at.isoformat(),
        "last_seen_at": find.last_seen_at.isoformat() if find.last_seen_at else None,
    }))
    public_keys = (
        "id",
        "source_name",
        "display_source_name",
        "watch_type",
        "finding_type",
        "content_type",
        "review_topic",
        "title",
        "display_title",
        "display_caption",
        "url",
        "summary_text",
        "importance_signal",
        "decided_at",
        "first_seen_at",
        "last_seen_at",
    )
    return {key: enriched.get(key) for key in public_keys}
