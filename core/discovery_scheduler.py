"""Discovery scheduler.

Reconciles the static `WATCHLIST_SEED` against the `discovery_source` table,
selects sources that are due, dispatches them to the right handler, persists
the new findings deduped against prior runs, and updates per-source poll state.

No LLM in this path. Deterministic, observable, cheap to run. Higher-level
agents (scout, classifier, digest builder) live in separate modules and read
from these tables; they never inline themselves into the polling loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from core.db import (
    DISCOVERY_AUDIT_FIND_SEEN,
    DISCOVERY_AUDIT_POLL_FAILED,
    DISCOVERY_AUDIT_POLL_STARTED,
    DISCOVERY_AUDIT_POLL_SUCCEEDED,
    DISCOVERY_AUDIT_SOURCE_ADDED,
    DISCOVERY_AUDIT_SOURCE_UPDATED,
    DiscoveryAudit,
    DiscoveryFind,
    DiscoverySource,
)
from core.discovery_handlers import (
    DiscoverySourceLike,
    FindCandidate,
    PollResult,
    get_handler,
    registered_watch_types,
)
from kb.watchlist import WatchedSource, refresh_seconds, validate_watchlist
from kb.watchlist_seed import WATCHLIST_SEED

logger = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_POLL_CONCURRENCY = 8
DEFAULT_SOURCE_TIMEOUT_SECONDS = 60


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class PollSummary:
    """Per-source outcome for one scheduler pass."""

    source_id: str
    source_name: str
    watch_type: str
    status: str
    new_finds: int = 0
    repeat_finds: int = 0
    error: str = ""


@dataclass
class SchedulerRunReport:
    """Aggregated outcome of one scheduler pass."""

    started_at: datetime
    finished_at: datetime
    sources_due: int
    sources_polled: int
    sources_failed: int
    new_finds: int
    repeat_finds: int
    per_source: list[PollSummary]

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "sources_due": self.sources_due,
            "sources_polled": self.sources_polled,
            "sources_failed": self.sources_failed,
            "new_finds": self.new_finds,
            "repeat_finds": self.repeat_finds,
            "per_source": [vars(s) for s in self.per_source],
        }


async def reconcile_watchlist_seed(
    session: AsyncSession,
    seed: Iterable[WatchedSource] = WATCHLIST_SEED,
) -> tuple[int, int]:
    """Bring `discovery_source` in line with the static seed.

    Returns (created_count, updated_count). Existing rows whose target,
    cadence, or active flag drifted from the seed get patched. Rows whose
    seed entry was removed are left untouched (deactivation is an operator
    decision recorded in audit).
    """

    seed_tuple = tuple(seed)
    validate_watchlist(seed_tuple)
    created = 0
    updated = 0
    now = datetime.now(timezone.utc)

    for entry in seed_tuple:
        result = await session.execute(
            select(DiscoverySource).where(DiscoverySource.name == entry.name)
        )
        existing = result.scalar_one_or_none()
        desired_interval = refresh_seconds(entry)

        if existing is None:
            row = DiscoverySource(
                name=entry.name,
                watch_type=entry.watch_type,
                target=entry.target,
                refresh_interval_seconds=desired_interval,
                importance_floor=entry.importance_floor,
                active=entry.active,
            )
            session.add(row)
            await session.flush()
            await _audit(
                session,
                source_id=row.id,
                event_type=DISCOVERY_AUDIT_SOURCE_ADDED,
                actor="scheduler",
                after_state=_serialize_seed(entry),
                reason="seed reconciliation",
            )
            created += 1
            continue

        drift = {}
        if existing.target != entry.target:
            drift["target"] = (existing.target, entry.target)
            existing.target = entry.target
        if existing.refresh_interval_seconds != desired_interval:
            drift["refresh_interval_seconds"] = (
                existing.refresh_interval_seconds,
                desired_interval,
            )
            existing.refresh_interval_seconds = desired_interval
        if existing.watch_type != entry.watch_type:
            drift["watch_type"] = (existing.watch_type, entry.watch_type)
            existing.watch_type = entry.watch_type
        if existing.importance_floor != entry.importance_floor:
            drift["importance_floor"] = (existing.importance_floor, entry.importance_floor)
            existing.importance_floor = entry.importance_floor
        if existing.active != entry.active:
            drift["active"] = (existing.active, entry.active)
            existing.active = entry.active

        if drift:
            existing.updated_at = now
            await _audit(
                session,
                source_id=existing.id,
                event_type=DISCOVERY_AUDIT_SOURCE_UPDATED,
                actor="scheduler",
                before_state=json.dumps({k: v[0] for k, v in drift.items()}, default=str),
                after_state=json.dumps({k: v[1] for k, v in drift.items()}, default=str),
                reason="seed reconciliation",
            )
            updated += 1

    await session.commit()
    return created, updated


async def select_due_sources(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    limit: int = 200,
) -> list[DiscoverySource]:
    """Return active sources whose last poll is older than their cadence."""

    current = _as_aware_utc(now or datetime.now(timezone.utc))
    result = await session.execute(
        select(DiscoverySource).where(DiscoverySource.active.is_(True)).limit(limit)
    )
    rows = list(result.scalars().all())
    due: list[DiscoverySource] = []
    for row in rows:
        if row.last_polled_at is None:
            due.append(row)
            continue
        elapsed = (current - _as_aware_utc(row.last_polled_at)).total_seconds()
        if elapsed >= row.refresh_interval_seconds:
            due.append(row)
    return due


async def run_one_pass(
    session: AsyncSession,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
    seed: Iterable[WatchedSource] = WATCHLIST_SEED,
    poll_concurrency: int = DEFAULT_POLL_CONCURRENCY,
    per_source_timeout_seconds: float = DEFAULT_SOURCE_TIMEOUT_SECONDS,
    key_store: Optional[ApiKeyStore] = None,
    admin_token: str = "",
) -> SchedulerRunReport:
    """Reconcile seed, poll due sources, record findings, return a report."""

    started = now or datetime.now(timezone.utc)
    await reconcile_watchlist_seed(session, seed)

    due = await select_due_sources(session, now=started)
    summaries: list[PollSummary] = []
    sources_polled = 0
    sources_failed = 0
    total_new = 0
    total_repeat = 0

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS)
    try:
        poll_jobs: list[asyncio.Task[tuple[DiscoverySource, PollResult | None, str]]] = []
        semaphore = asyncio.Semaphore(max(1, poll_concurrency))

        for source in due:
            if source.watch_type not in registered_watch_types():
                summary = PollSummary(
                    source_id=source.id,
                    source_name=source.name,
                    watch_type=source.watch_type,
                    status="error",
                    error=f"No handler registered for {source.watch_type}",
                )
                summaries.append(summary)
                sources_failed += 1
                await _record_failed_poll(session, source, summary.error)
                continue

            await _audit(
                session,
                source_id=source.id,
                event_type=DISCOVERY_AUDIT_POLL_STARTED,
                actor="scheduler",
                reason=f"due (last_polled_at={source.last_polled_at})",
            )
            poll_jobs.append(
                asyncio.create_task(
                    _poll_source(
                        source,
                        client,
                        semaphore,
                        per_source_timeout_seconds=per_source_timeout_seconds,
                        key_store=key_store,
                        admin_token=admin_token,
                    )
                )
            )

        for source, result, error in await asyncio.gather(*poll_jobs):
            if result is None:
                logger.error("Discovery handler failed for %s: %s", source.name, error)
                summary = PollSummary(
                    source_id=source.id,
                    source_name=source.name,
                    watch_type=source.watch_type,
                    status="error",
                    error=error,
                )
                summaries.append(summary)
                sources_failed += 1
                await _record_failed_poll(session, source, summary.error)
                continue

            source_id = source.id
            source_name = source.name
            source_watch_type = source.watch_type
            try:
                new_count, repeat_count = await _persist_finds(session, source, result.finds)
                await _record_successful_poll(session, source, result)
            except Exception as exc:  # pragma: no cover - defensive persistence boundary
                await session.rollback()
                error_message = f"result persistence failed: {exc}"
                logger.exception("Discovery result handling failed for %s", source_name)
                summary = PollSummary(
                    source_id=source_id,
                    source_name=source_name,
                    watch_type=source_watch_type,
                    status="error",
                    error=error_message,
                )
                summaries.append(summary)
                sources_failed += 1
                try:
                    fresh_source = await session.get(DiscoverySource, source_id)
                    if fresh_source is not None:
                        await _record_failed_poll(session, fresh_source, error_message)
                except Exception:
                    await session.rollback()
                    logger.exception("Failed to record discovery poll failure for %s", source_name)
                continue
            sources_polled += 1
            total_new += new_count
            total_repeat += repeat_count
            summaries.append(
                PollSummary(
                    source_id=source.id,
                    source_name=source.name,
                    watch_type=source.watch_type,
                    status=result.status,
                    new_finds=new_count,
                    repeat_finds=repeat_count,
                    error=result.error,
                )
            )
    finally:
        if owns_client:
            await client.aclose()

    finished = datetime.now(timezone.utc)
    return SchedulerRunReport(
        started_at=started,
        finished_at=finished,
        sources_due=len(due),
        sources_polled=sources_polled,
        sources_failed=sources_failed,
        new_finds=total_new,
        repeat_finds=total_repeat,
        per_source=summaries,
    )


# Maps watch_type to the key-store provider_id that holds its auth token.
_WATCH_TYPE_TO_PROVIDER: dict[str, str] = {
    "github_repo_watch": "github",
    "github_repo_artifact_scan": "github",
    "github_topic_watch": "github",
    "github_user_org_watch": "github",
    "saved_search": "brave",
}

async def _poll_source(
    source: DiscoverySource,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    *,
    per_source_timeout_seconds: float,
    key_store: Optional[ApiKeyStore] = None,
    admin_token: str = "",
) -> tuple[DiscoverySource, PollResult | None, str]:
    handler = get_handler(source.watch_type)
    provider_id = _WATCH_TYPE_TO_PROVIDER.get(source.watch_type, "")
    auth_token = ""
    if provider_id and key_store and admin_token:
        auth_token = await key_store.get_key(provider_id, admin_token) or ""
    like = DiscoverySourceLike(
        name=source.name,
        watch_type=source.watch_type,
        target=source.target,
        etag=source.etag,
        last_modified=source.last_modified,
        auth_token=auth_token,
    )
    async with semaphore:
        try:
            result = await asyncio.wait_for(
                handler(like, client),
                timeout=per_source_timeout_seconds,
            )
            return source, result, ""
        except TimeoutError:
            return source, None, f"handler timed out after {per_source_timeout_seconds:g}s"
        except Exception as exc:  # pragma: no cover - defensive
            return source, None, f"handler crashed: {exc}"


_FIRST_CLASS_CONTENT_MARKERS = (
    "release",
    "changelog",
    "tagged",
    "v1.",
    "v2.",
    "v3.",
    "version ",
    ".bundle.json",
    "schema",
    "index.md",
    "readme",
    "post:",
    "blog",
    "newsletter",
    "tutorial",
)


def _is_routine_artifact_noise(candidate: FindCandidate, source: DiscoverySource) -> bool:
    """Decide whether this new find is per-file artifact noise.

    User feedback (2026-05-14): "I do NOT need to approve like, 'Oh this .ps1
    in this repo is new'." Per-file diffs inside tracked artifact scans roll
    up under their source for activity tracking; they don't become individual
    Chroma documents or operator approval items.

    First-class events (releases, RSS posts, new repos from tracked creators,
    bundle/schema/docs additions, anything explicitly high importance) escape
    the noise filter and proceed to auto-ingest.
    """
    if source.watch_type != "github_repo_artifact_scan":
        return False
    if candidate.importance_signal == "high":
        return False
    text = f"{candidate.title} {candidate.summary_text}".lower()
    if any(marker in text for marker in _FIRST_CLASS_CONTENT_MARKERS):
        return False
    return True


def _initial_status_for(candidate: FindCandidate, source: DiscoverySource) -> str:
    """Pick the initial status for a freshly-stored find.

    new_source_candidate: pending (the one remaining operator approval gate).
    artifact-scan noise: source_activity (rolled up, never individually surfaced).
    everything else from an approved watched source: auto_indexed (the
    approved-source contract is that its content streams into KB automatically).
    """
    if candidate.finding_type == "new_source_candidate":
        return "pending"
    if _is_routine_artifact_noise(candidate, source):
        return "source_activity"
    return "auto_indexed"


async def _auto_ingest_if_eligible(record: DiscoveryFind, source: DiscoverySource) -> None:
    """Publish a first-class event into library and index it in Chroma.

    The architecture: library (the configured corpus GitHub repo + the public
    /library site) is the canonical curated library. Chroma is the vector
    index over library content that the terminal MCP queries for grounded
    answers. An approved source's new content flows into BOTH places
    automatically; the operator never gates per-find ingestion.

    Two independent writes, both fail-soft:
    - library publish writes a markdown file with title/URL/summary/source
      metadata into the GitHub repo. Skipped silently if the GitHub App
      credentials aren't configured (LibraryPublisherConfigError); skipped on
      transient GitHub failures.
    - Chroma index writes a reference doc so the terminal can cite the find
      immediately, even if library repo publish hasn't happened yet.

    The two flags on the find (published_to_library_repo, ingested_into_chroma)
    move independently so a partial outage in one path doesn't block the
    other. A future reconcile sweep can retry whichever flag is still False.
    """
    if record.status != "auto_indexed":
        return

    now = datetime.now(timezone.utc)
    record.decided_at = now
    record.reviewer = "scheduler:auto_ingest"

    # library repo publish first: it's the canonical store. The Chroma index
    # exists to make library queryable; we still write Chroma below regardless
    # so the LLM has the reference even if the publisher is unconfigured.
    try:
        from kb.library_publisher import (
            LibraryPublisherConfigError,
            LibraryPublisherError,
            publish_find_to_library,
        )
        result = await publish_find_to_library(record, source)
        record.published_to_library_repo = True
        record.library_target_path = result.path
        record.library_file_url = result.public_url or result.commit_url
        record.library_promotion_error = ""
        record.promoted_at = now
    except ImportError:
        pass  # publisher module unavailable; treat as unconfigured
    except Exception as exc:  # noqa: BLE001 - never block persistence on publish failure
        record.library_promotion_error = type(exc).__name__
        # LibraryPublisherConfigError is the "creds absent" path: log at debug
        # so a development VPS without library creds doesn't spam warnings.
        try:
            from kb.library_publisher import LibraryPublisherConfigError as _ConfigErr
            if isinstance(exc, _ConfigErr):
                logger.debug(
                    "library publisher unconfigured; skipping repo publish for find %s",
                    record.id,
                )
            else:
                logger.warning(
                    "library publish failed for find %s from source %s: %s",
                    record.id, source.name, exc,
                )
        except ImportError:
            logger.warning(
                "library publish failed for find %s from source %s: %s",
                record.id, source.name, exc,
            )

    # Chroma index second. Cheap, local, fast.
    try:
        from kb.discovery_ingest import ingest_approved_find_into_chroma
        await asyncio.to_thread(ingest_approved_find_into_chroma, record, source)
        record.ingested_into_chroma = True
    except Exception as exc:  # noqa: BLE001 - never block persistence on index failure
        logger.warning(
            "Chroma index failed for find %s from source %s: %s",
            record.id, source.name, exc,
        )


async def _persist_finds(
    session: AsyncSession,
    source: DiscoverySource,
    candidates: list[FindCandidate],
) -> tuple[int, int]:
    new_count = 0
    repeat_count = 0
    now = datetime.now(timezone.utc)
    for candidate in candidates:
        result = await session.execute(
            select(DiscoveryFind).where(
                DiscoveryFind.discovery_source_id == source.id,
                DiscoveryFind.external_id == candidate.external_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.last_seen_at = now
            # Rollup candidates carry the live child list and the
            # upstream timestamp on every poll. Refresh them so the
            # admin UI shows "TimeZest now has 12 files, last updated
            # May 5" without waiting for an external_id change.
            if candidate.category:
                existing.category = candidate.category
                existing.child_count = candidate.child_count
                existing.raw_payload = json.dumps(
                    candidate.raw_payload, ensure_ascii=False, default=str,
                )
                if candidate.last_upstream_updated_at is not None:
                    existing.last_upstream_updated_at = candidate.last_upstream_updated_at
            repeat_count += 1
            continue
        initial_status = _initial_status_for(candidate, source)
        record = DiscoveryFind(
            discovery_source_id=source.id,
            finding_type=candidate.finding_type,
            external_id=candidate.external_id,
            title=candidate.title[:512],
            url=candidate.url[:1024],
            summary_text=candidate.summary_text[:4000],
            raw_payload=json.dumps(candidate.raw_payload, ensure_ascii=False, default=str),
            importance_signal=candidate.importance_signal,
            status=initial_status,
            category=candidate.category,
            child_count=candidate.child_count,
            last_upstream_updated_at=candidate.last_upstream_updated_at,
        )
        session.add(record)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            repeat_count += 1
            continue
        # First-class events ingest into Chroma immediately. The contract is
        # that an approved watched source's content streams to KB without
        # operator review; only new-source-candidate finds gate at the queue.
        await _auto_ingest_if_eligible(record, source)
        await _audit(
            session,
            find_id=record.id,
            source_id=source.id,
            event_type=DISCOVERY_AUDIT_FIND_SEEN,
            actor="scheduler",
            after_state=json.dumps(
                {
                    "title": record.title,
                    "url": record.url,
                    "finding_type": record.finding_type,
                    "importance_signal": record.importance_signal,
                    "initial_status": initial_status,
                    "auto_ingested": record.ingested_into_chroma,
                },
                default=str,
            ),
        )
        new_count += 1
    await session.commit()
    return new_count, repeat_count


async def _record_successful_poll(
    session: AsyncSession, source: DiscoverySource, result: PollResult
) -> None:
    now = datetime.now(timezone.utc)
    source.last_polled_at = now
    source.last_status = result.status
    source.last_error = ""
    source.consecutive_failures = 0
    if result.etag:
        source.etag = result.etag
    if result.last_modified:
        source.last_modified = result.last_modified
    source.updated_at = now
    await _audit(
        session,
        source_id=source.id,
        event_type=DISCOVERY_AUDIT_POLL_SUCCEEDED,
        actor="scheduler",
        after_state=result.status,
    )
    await session.commit()


async def _record_failed_poll(
    session: AsyncSession, source: DiscoverySource, error: str
) -> None:
    now = datetime.now(timezone.utc)
    source.last_polled_at = now
    source.last_status = "error"
    source.last_error = error[:1024]
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.updated_at = now
    await _audit(
        session,
        source_id=source.id,
        event_type=DISCOVERY_AUDIT_POLL_FAILED,
        actor="scheduler",
        reason=error[:1024],
    )
    await session.commit()


async def _audit(
    session: AsyncSession,
    *,
    event_type: str,
    actor: str,
    find_id: Optional[str] = None,
    source_id: Optional[str] = None,
    before_state: str = "",
    after_state: str = "",
    reason: str = "",
) -> None:
    record = DiscoveryAudit(
        find_id=find_id,
        source_id=source_id,
        event_type=event_type,
        actor=actor,
        before_state=before_state[:2048],
        after_state=after_state[:2048],
        reason=reason[:2048],
    )
    session.add(record)
    await session.flush()


def _serialize_seed(entry: WatchedSource) -> str:
    return json.dumps(
        {
            "name": entry.name,
            "watch_type": entry.watch_type,
            "target": entry.target,
            "refresh_cadence": entry.refresh_cadence,
            "importance_floor": entry.importance_floor,
            "active": entry.active,
        },
        default=str,
    )
