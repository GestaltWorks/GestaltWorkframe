"""Admin discovery endpoints and request schemas.

Discovery is large enough to warrant its own admin module: source CRUD, manual
scheduler trigger, finding review, promotion pipelines, and maintenance actions
for unpublishing or purging approved content. All token-gated through
`require_admin_token`.

Request schemas mirror the underlying domain objects:
- DiscoverySourceCreate validates the seed-shaped WatchedSource and runs
  validate_watchlist() so SSRF / target-shape rejection happens at insert.
- DiscoverySourcePatch is the mutable subset of an existing source.
- DiscoveryDecisionRequest covers approve/reject with optional Chroma ingest
  and library publish flags.
- DiscoveryLibraryPromotionRequest / DiscoverySourcePromotionRequest are the
  explicit promote-after-approve paths.
- DiscoveryMaintenanceRequest covers unpublish/purge operations.

`/admin/api/discovery/run-once` is process-wide rate-limited so a misbehaving
cron or admin button cannot stack scheduler passes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.services import require_admin_token
from core.db import get_session
from core.discovery_digest import send_discovery_digest
from core.discovery_queue import (
    add_watched_source,
    create_manual_find,
    decide_find,
    list_recent_finds,
    list_source_health,
    list_sources_with_activity,
    promote_find_to_library,
    promote_find_to_source,
    purge_find_from_kb,
    count_uncurated_finds_per_source,
    list_finds_for_source,
    set_find_dismissed,
    set_find_featured,
    set_find_newsletter_pending,
    set_find_ticker_featured,
    set_source_featured,
    unpublish_find_from_library,
    unpublish_find_from_latest,
    update_watched_source,
)
from core.url_metadata import (
    ExtractedMetadata,
    MetadataExtractError,
    extract_url_metadata,
)
from core.discovery_scheduler import run_one_pass
from core.discovery_summary import summarize_discovery_finds
from kb.library_publisher import LibraryPublisherConfigError, LibraryPublisherError
from kb.target_safety import validate_public_https_url
from kb.watchlist import CADENCE_SECONDS, WatchedSource, validate_watchlist


logger = logging.getLogger(__name__)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_intake_text(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()

DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS = int(os.getenv("DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS", "300"))

# Process-wide guard against repeated scheduler triggers. Module globals keep
# the rate limit cheap and stateless across requests within a single worker.
# Multi-worker uvicorn would need a shared store; today the single-worker
# deploy posture makes this sufficient.
_discovery_run_once_lock = asyncio.Lock()
_discovery_run_once_last_started_at = 0.0


class DiscoverySourceCreate(BaseModel):
    name: str
    watch_type: str
    target: str
    description: str
    refresh_cadence: str
    canonical_url: str
    provenance: str
    license_notes: str
    attribution: str
    trust_tier: str
    display_policy: str
    retrieval_policy: str
    curriculum_policy: str
    agent_access_policy: str
    secret_handling: str
    importance_floor: str = "normal"
    active: bool = True
    notes: str = Field(default="", max_length=2048)

    @field_validator("*")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return clean_intake_text(value)
        return value

    @model_validator(mode="after")
    def validate_watched_source(self) -> "DiscoverySourceCreate":
        validate_watchlist([self.to_watched_source()])
        return self

    def to_watched_source(self) -> WatchedSource:
        data = self.model_dump(exclude={"notes"})
        return WatchedSource(**data)


class DiscoverySourcePatch(BaseModel):
    refresh_cadence: str | None = Field(default=None, min_length=3, max_length=40)
    refresh_interval_seconds: int | None = Field(default=None, ge=300, le=2_592_000)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2048)
    importance_floor: str | None = Field(default=None, min_length=3, max_length=20)

    @field_validator("refresh_cadence")
    @classmethod
    def validate_refresh_cadence(cls, value: str | None) -> str | None:
        if value is not None and value not in CADENCE_SECONDS:
            raise ValueError(f"Unsupported refresh_cadence: {value}")
        return value

    @field_validator("importance_floor")
    @classmethod
    def validate_importance_floor(cls, value: str | None) -> str | None:
        if value is not None and value not in {"low", "normal", "high"}:
            raise ValueError("importance_floor must be one of low, normal, high")
        return value

    @field_validator("notes", mode="before")
    @classmethod
    def clean_notes(cls, value: object) -> object:
        if value is None:
            return None
        return clean_intake_text(str(value))

    def interval_seconds(self) -> int | None:
        if self.refresh_interval_seconds is not None:
            return self.refresh_interval_seconds
        if self.refresh_cadence is not None:
            return CADENCE_SECONDS[self.refresh_cadence]
        return None


class DiscoveryDecisionRequest(BaseModel):
    notes: str = Field(default="", max_length=2048)
    reviewer: str = Field(default="admin", max_length=120)
    ingest_into_chroma: bool = False
    publish_to_library: bool = False

    @field_validator("notes", "reviewer", mode="before")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))


class DiscoveryLibraryPromotionRequest(BaseModel):
    notes: str = Field(default="", max_length=2048)
    reviewer: str = Field(default="admin", max_length=120)
    target_path: str = Field(default="", max_length=512)

    @field_validator("notes", "reviewer", "target_path", mode="before")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))


class DiscoverySourcePromotionRequest(BaseModel):
    notes: str = Field(default="", max_length=2048)
    reviewer: str = Field(default="admin", max_length=120)
    refresh_cadence: Literal["hourly", "every_6h", "daily", "weekly"] = "daily"
    add_artifact_scan: bool = True

    @field_validator("notes", "reviewer", mode="before")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))


class DiscoveryMaintenanceRequest(BaseModel):
    notes: str = Field(default="", max_length=2048)
    reviewer: str = Field(default="admin", max_length=120)

    @field_validator("notes", "reviewer", mode="before")
    @classmethod
    def clean_strings(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))


router = APIRouter(prefix="/admin/api/discovery", tags=["admin", "discovery"])


@router.post("/run-once")
async def admin_discovery_run_once(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Trigger one pass of the discovery scheduler.

    Reconciles the static watchlist seed against `discovery_source`, polls every
    due source, persists new findings deduped against prior runs, and returns
    the run report. No LLM in this path. Per-source rate limits and conditional
    fetch are handled inside the scheduler.
    """

    global _discovery_run_once_last_started_at
    async with _discovery_run_once_lock:
        now = time.monotonic()
        elapsed = now - _discovery_run_once_last_started_at
        if (
            DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS > 0
            and _discovery_run_once_last_started_at > 0
            and elapsed < DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS
        ):
            retry_after = max(1, int(DISCOVERY_RUN_ONCE_MIN_INTERVAL_SECONDS - elapsed))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Discovery scheduler was triggered recently. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        _discovery_run_once_last_started_at = now

    report = await run_one_pass(session)
    payload = report.to_dict()
    try:
        payload["digest_status"] = await send_discovery_digest(session)
    except Exception:
        logger.exception("Discovery digest email failed")
        payload["digest_status"] = "error"
    return payload


@router.get("/finds")
async def admin_discovery_finds(
    limit: int = Query(default=50, ge=1, le=250),
    status: str | None = None,
    include_activity: bool = Query(default=False),
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    finds = await list_recent_finds(session, limit=limit, status=status, include_activity=include_activity)
    return {"finds": finds, "summary": summarize_discovery_finds(finds)}


@router.post("/finds/{find_id}/approve")
async def admin_discovery_approve_find(
    find_id: str,
    decision: DiscoveryDecisionRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await decide_find(
            session,
            find_id,
            "approve",
            reviewer=decision.reviewer or "admin",
            notes=decision.notes,
            ingest_into_chroma=decision.ingest_into_chroma,
            publish_to_library=decision.publish_to_library,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LibraryPublisherConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LibraryPublisherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"find": find}


@router.post("/finds/{find_id}/reject")
async def admin_discovery_reject_find(
    find_id: str,
    decision: DiscoveryDecisionRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await decide_find(
            session,
            find_id,
            "reject",
            reviewer=decision.reviewer or "admin",
            notes=decision.notes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"find": find}


@router.post("/finds/{find_id}/promote-library")
async def admin_discovery_promote_library(
    find_id: str,
    request_body: DiscoveryLibraryPromotionRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await promote_find_to_library(
            session,
            find_id,
            reviewer=request_body.reviewer or "admin",
            notes=request_body.notes,
            target_path=request_body.target_path,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LibraryPublisherConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LibraryPublisherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"find": find}


@router.post("/finds/{find_id}/promote-source")
async def admin_discovery_promote_source(
    find_id: str,
    request_body: DiscoverySourcePromotionRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await promote_find_to_source(
            session,
            find_id,
            reviewer=request_body.reviewer or "admin",
            notes=request_body.notes,
            refresh_cadence=request_body.refresh_cadence,
            add_artifact_scan=request_body.add_artifact_scan,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/finds/{find_id}/unpublish-latest")
async def admin_discovery_unpublish_latest(
    find_id: str,
    request_body: DiscoveryMaintenanceRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await unpublish_find_from_latest(session, find_id, reviewer=request_body.reviewer or "admin", notes=request_body.notes)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"find": find}


@router.post("/finds/{find_id}/unpublish-library")
async def admin_discovery_unpublish_library(
    find_id: str,
    request_body: DiscoveryMaintenanceRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await unpublish_find_from_library(session, find_id, reviewer=request_body.reviewer or "admin", notes=request_body.notes)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LibraryPublisherConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LibraryPublisherError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"find": find}


@router.post("/finds/{find_id}/purge-kb")
async def admin_discovery_purge_kb(
    find_id: str,
    request_body: DiscoveryMaintenanceRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await purge_find_from_kb(session, find_id, reviewer=request_body.reviewer or "admin", notes=request_body.notes)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Retrieval index purge failed") from exc
    return {"find": find}


@router.get("/sources")
async def admin_discovery_sources(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    sources = await list_source_health(session)
    return {"sources": sources}


@router.post("/sources")
async def admin_discovery_create_source(
    source: DiscoverySourceCreate,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        created = await add_watched_source(
            session,
            source.to_watched_source(),
            notes=source.notes,
            actor="api",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": created}


@router.patch("/sources/{source_id}")
async def admin_discovery_update_source(
    source_id: str,
    patch: DiscoverySourcePatch,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        source = await update_watched_source(
            session,
            source_id,
            refresh_interval_seconds=patch.interval_seconds(),
            active=patch.active,
            notes=patch.notes,
            importance_floor=patch.importance_floor,
            actor="api",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source": source}


# Phase A curation surface: sources-with-activity is the rolled-up source-centric
# view that replaces the per-find approval queue for everything except
# new_source_candidate. Per-file artifact noise is invisible here; what an
# operator sees is "which approved sources are active, when, what notable
# items did they produce, and is the source itself featured."
@router.get("/sources-with-activity")
async def admin_discovery_sources_with_activity(
    window_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=250, ge=1, le=1000),
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    sources = await list_sources_with_activity(session, window_days=window_days, limit=limit)
    return {"sources": sources, "window_days": window_days}


class DiscoveryFeatureRequest(BaseModel):
    featured: bool
    reviewer: str = Field(default="admin", max_length=100)


@router.post("/sources/{source_id}/feature")
async def admin_discovery_feature_source(
    source_id: str,
    request_body: DiscoveryFeatureRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        source = await set_source_featured(
            session,
            source_id,
            featured=request_body.featured,
            reviewer=request_body.reviewer,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"source": source}


@router.post("/finds/{find_id}/feature")
async def admin_discovery_feature_find(
    find_id: str,
    request_body: DiscoveryFeatureRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        find = await set_find_featured(
            session,
            find_id,
            featured=request_body.featured,
            reviewer=request_body.reviewer,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"find": find}


# Phase 2 curation split: ticker-feature (30-day decay), newsletter-queue,
# and dismiss are the three new per-find actions exposed to the admin
# panel. They share the DiscoveryFeatureRequest body shape (bool + reviewer)
# under role-specific field names: featured/pending/dismissed. Reusing the
# shape keeps the frontend handlers symmetric.


class DiscoveryTickerFeatureRequest(BaseModel):
    featured: bool
    reviewer: str = Field(default="admin", max_length=100)


@router.post("/finds/{find_id}/ticker-feature")
async def admin_discovery_ticker_feature_find(
    find_id: str,
    request_body: DiscoveryTickerFeatureRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Toggle the Phase 2 ticker_featured flag (30-day rolling window)."""
    try:
        find = await set_find_ticker_featured(
            session,
            find_id,
            featured=request_body.featured,
            reviewer=request_body.reviewer,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"find": find}


class DiscoveryNewsletterQueueRequest(BaseModel):
    pending: bool
    reviewer: str = Field(default="admin", max_length=100)


@router.post("/finds/{find_id}/newsletter-queue")
async def admin_discovery_queue_for_newsletter(
    find_id: str,
    request_body: DiscoveryNewsletterQueueRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Legacy generic queue toggle.

    Retained for backwards compatibility with the previous admin UI
    that just flipped a boolean. New callers should hit the
    /assign-issue endpoint below, which targets a specific issue.
    `pending=true` here assigns the find to the most imminent open
    draft (creating one if none exists) so the behavior matches what
    the old UI used to do.
    """
    from core.newsletter import (
        assign_find_to_issue as _assign_find_to_issue,
        create_empty_issue as _create_empty_issue,
        list_assignable_issues as _list_assignable_issues,
    )

    if not request_body.pending:
        try:
            find = await _assign_find_to_issue(session, find_id, None)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"find": (await _serialize_find_shim(find, session))}

    upcoming = await _list_assignable_issues(session)
    target_issue = upcoming[0] if upcoming else await _create_empty_issue(session)
    try:
        find = await _assign_find_to_issue(session, find_id, target_issue.id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"find": (await _serialize_find_shim(find, session)), "issue_id": target_issue.id}


class DiscoveryAssignIssueRequest(BaseModel):
    """Per-issue tagging. issue_id=null untags the find from whatever
    issue currently holds it. issue_id pointing at a closed (sent /
    skipped) issue is rejected so historical issues stay immutable.
    """
    issue_id: str | None = Field(default=None, max_length=200)
    reviewer: str = Field(default="admin", max_length=100)


@router.post("/finds/{find_id}/assign-issue")
async def admin_discovery_assign_to_issue(
    find_id: str,
    request_body: DiscoveryAssignIssueRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Tag a find for a specific newsletter issue or clear the
    assignment. Powers the per-find dropdown on /admin/discovery.
    """
    from core.newsletter import assign_find_to_issue as _assign_find_to_issue

    try:
        find = await _assign_find_to_issue(session, find_id, request_body.issue_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"find": (await _serialize_find_shim(find, session))}


async def _serialize_find_shim(find, session):
    """Re-serialize a DiscoveryFind so the response matches the existing
    /newsletter-queue shape. Routes through the queue module's
    serializer to keep field coverage consistent."""
    from core.discovery_queue import _serialize_find
    from core.db import DiscoverySource
    from sqlmodel import select as _select

    source = (
        await session.execute(
            _select(DiscoverySource).where(DiscoverySource.id == find.discovery_source_id)
        )
    ).scalar_one()
    return _serialize_find(find, source)


class DiscoveryDismissRequest(BaseModel):
    dismissed: bool
    reviewer: str = Field(default="admin", max_length=100)


@router.post("/finds/{find_id}/dismiss")
async def admin_discovery_dismiss_find(
    find_id: str,
    request_body: DiscoveryDismissRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Mark a find as reviewed-and-skipped so it stops counting toward
    the New Content badge on the source card. Also clears any active
    ticker_featured / newsletter_pending flags in one operation."""
    try:
        find = await set_find_dismissed(
            session,
            find_id,
            dismissed=request_body.dismissed,
            reviewer=request_body.reviewer,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"find": find}


@router.get("/sources/{source_id}/finds")
async def admin_discovery_source_finds(
    source_id: str,
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=200),
    days: int | None = Query(default=None, ge=1, le=3650),
    topic: str | None = Query(default=None, max_length=200),
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Paginated, filterable find list for one source.

    Powers the Phase 2 admin drilldown: click into a source card and see
    all of its content, with date and topic filters and pagination
    controls. Returns the find rows in the same shape as the items list
    so the frontend can reuse its render code.
    """
    return await list_finds_for_source(
        session,
        source_id,
        page=page,
        page_size=page_size,
        days=days,
        topic=topic,
    )


@router.get("/uncurated-counts")
async def admin_discovery_uncurated_counts(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Map of source_id -> count of finds awaiting curation review.

    Drives the "New content" badge on each source card in the admin
    discovery panel. A source has new content when it has auto_indexed
    finds that are not ticker_featured, not newsletter_pending, and not
    dismissed.
    """
    counts = await count_uncurated_finds_per_source(session)
    return {"counts": counts}


# ---------------------------------------------------------------------------
# Manual URL paste flow (Phase B).
#
# Two endpoints behind require_admin_token:
#   1. POST /admin/api/discovery/extract-metadata
#      Server-side fetch of a public https URL with SSRF guards. Returns an
#      OG/Twitter card preview dict the operator can edit before save. The
#      fetch never burns user-controlled credit; metadata extraction is
#      string parsing.
#   2. POST /admin/api/discovery/manual-find
#      Persist the operator-edited preview as a DiscoveryFind under the
#      synthetic `manual_curation` source. Optionally flags it
#      newsletter_pending so it joins the next composer pull.
# ---------------------------------------------------------------------------


class DiscoveryExtractMetadataRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)

    @field_validator("url", mode="before")
    @classmethod
    def clean_url(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))


def _serialize_metadata(metadata: ExtractedMetadata) -> dict[str, Any]:
    return {
        "url": metadata.url,
        "title": metadata.title,
        "description": metadata.description,
        "image_url": metadata.image_url,
        "source_name": metadata.source_name,
        "raw_html_length": metadata.raw_html_length,
    }


@router.post("/extract-metadata")
async def admin_discovery_extract_metadata(
    request_body: DiscoveryExtractMetadataRequest,
    _: None = Depends(require_admin_token),
):
    """SSRF-safe fetch of a public URL; returns an editable OG preview dict."""
    try:
        metadata = await extract_url_metadata(request_body.url)
    except MetadataExtractError as exc:
        # Operator-visible 4xx: bad URL, blocked host, non-HTML response,
        # timeout, redirect-to-private. The extractor module already maps
        # every expected failure to MetadataExtractError.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"metadata": _serialize_metadata(metadata)}


class DiscoveryManualFindRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    image_url: str = Field(default="", max_length=2048)
    source_label: str = Field(default="", max_length=200)
    queue_for_newsletter: bool = True
    reviewer: str = Field(default="admin", max_length=120)

    @field_validator(
        "url",
        "title",
        "description",
        "image_url",
        "source_label",
        "reviewer",
        mode="before",
    )
    @classmethod
    def clean_strings(cls, value: object) -> object:
        return clean_intake_text(str(value or ""))

    @field_validator("url")
    @classmethod
    def url_must_be_https(cls, value: str) -> str:
        try:
            return validate_public_https_url(value, source_name="manual discovery URL", field="url")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("image_url")
    @classmethod
    def image_url_must_be_http_or_blank(cls, value: str) -> str:
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("image_url must be an http(s) URL or blank")
        return value


@router.post("/manual-find")
async def admin_discovery_create_manual_find(
    request_body: DiscoveryManualFindRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Persist an operator-pasted URL as a DiscoveryFind for the newsletter pipeline."""
    try:
        find = await create_manual_find(
            session,
            url=request_body.url,
            title=request_body.title,
            description=request_body.description,
            image_url=request_body.image_url,
            source_label=request_body.source_label,
            queue_for_newsletter=request_body.queue_for_newsletter,
            reviewer=request_body.reviewer or "admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"find": find}
