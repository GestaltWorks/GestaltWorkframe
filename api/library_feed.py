"""Public library feed endpoint.

`/library/latest.json` returns approved discovery findings as a public JSON
feed. LibraryUpdatesTicker on the landing/about pages and LibraryLatestFeed on
/library/latest both consume it. The response is cached at the edge for
5 minutes with a 30-minute stale-while-revalidate window so a hot feed
doesn't hammer the SQLite store on every page render.

The window/limit/offset query params are clamped server-side. Approved
public statuses only - rejected and pending finds never appear here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import HTTPException
from sqlmodel import select

from gestaltworkframe.core.db import DiscoverySource, get_session
from gestaltworkframe.core.discovery_queue import (
    list_public_latest_finds,
    list_sources_with_activity,
    list_ticker_finds,
)
from gestaltworkframe.core.newsletter import (
    get_issue_detail,
    list_issues as list_newsletter_issues,
)


router = APIRouter(tags=["library"])


@router.get("/library/latest.json")
async def library_latest_feed(
    response: Response,
    limit: int = 25,
    offset: int = 0,
    days: int = 15,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    safe_limit = min(max(limit, 1), 100)
    safe_offset = max(offset, 0)
    safe_days = min(max(days, 1), 365)
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    finds = await list_public_latest_finds(
        session, limit=safe_limit, offset=safe_offset, days=safe_days,
    )
    return {
        "finds": finds,
        "limit": safe_limit,
        "offset": safe_offset,
        "days": safe_days,
        "title": "Updates and Additions",
    }


# Phase 4 newsletter archive: public list of sent issues plus per-slug
# detail. The /library/latest page renders issue cards from /library/issues.json
# and the per-slug page renders the issue HTML returned by /library/issues/{slug}.json.
# Only issues with status="sent" are exposed publicly.


@router.get("/library/issues.json")
async def library_issues_feed(
    response: Response,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Public newsletter issue archive.

    Filters to sent issues only. Each row carries the public-safe summary
    (slug, subject, period, sent_at, find_count). Per-issue editorial /
    finds / rendered HTML live behind /library/issues/{slug}.json.
    """
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    safe_limit = min(max(limit, 1), 200)
    # public_only=True restricts to status=sent and include_unpublished=False
    # filters out anything an admin has soft-deleted via the Unpublish action.
    issues = await list_newsletter_issues(
        session,
        limit=safe_limit,
        include_unpublished=False,
        public_only=True,
    )
    public_issues = [
        {
            "slug": row["slug"],
            "display_label": row["display_label"],
            "ship_number": row["ship_number"],
            "subject": row["subject"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "sent_at": row["sent_at"],
            "find_count": row["find_count"],
        }
        for row in issues
    ]
    return {"issues": public_issues, "limit": safe_limit}


@router.get("/library/issues/{slug}.json")
async def library_issue_detail_public(
    slug: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Public detail for one sent newsletter issue.

    Resolves the issue by slug, confirms it's been sent (drafts are not
    public), and returns the snapshot finds + rendered HTML the page
    needs to display the issue.
    """
    from sqlmodel import select
    from gestaltworkframe.core.db.models import NewsletterIssue

    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.slug == slug))
    ).scalar_one_or_none()
    if issue is None or issue.status != "sent" or issue.unpublished_at is not None:
        # 404 also covers the unpublished case so unpublished issues
        # are indistinguishable from never-existed to the public.
        raise HTTPException(status_code=404, detail="Newsletter issue not found.")
    detail = await get_issue_detail(session, issue.id)
    # Strip internal-only fields and the preview-token unsubscribe URLs;
    # the public detail uses a generic token-less preview.
    public_payload = {
        "slug": detail["slug"],
        "display_label": detail.get("display_label", ""),
        "ship_number": detail.get("ship_number"),
        "subject": detail["subject"],
        "period_start": detail["period_start"],
        "period_end": detail["period_end"],
        "sent_at": detail["sent_at"],
        "find_count": detail["find_count"],
        "editorial_markdown": detail["editorial_markdown"],
        "finds": detail["finds"],
        "html": detail["html_preview"],
    }
    return {"issue": public_payload}


@router.get("/library/ticker.json")
async def library_ticker_feed(
    response: Response,
    limit: int = 25,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Public LibraryUpdatesTicker source: ticker-featured finds (30-day window).

    Phase 2 introduces an explicit per-find ticker curation flag with a
    rolling 30-day lifetime. The previous /library/latest.json approach
    surfaced everything auto-indexed, which created exactly the noise
    the operator asked us to clean up. This endpoint serves the curated
    subset that earned a manual feature.
    """
    safe_limit = min(max(limit, 1), 100)
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    finds = await list_ticker_finds(session, limit=safe_limit)
    return {
        "finds": finds,
        "limit": safe_limit,
        "window_days": 30,
    }


def _public_source_payload(source_row: dict[str, object]) -> dict[str, object]:
    """Filter the admin sources-with-activity row down to public fields.

    The public library browse surface gets the visible metadata operators
    have curated: name, type, target, featured flag, activity counts,
    sample titles, and a sanitized recent-finds list (no raw payloads,
    no decision notes). Admin-only fields like reviewer, decided_at,
    or library_promotion_error are intentionally absent.
    """
    return {
        "id": source_row["id"],
        "name": source_row["name"],
        "display_name": source_row.get("display_name") or source_row["name"],
        "watch_type": source_row["watch_type"],
        "target": source_row["target"],
        "featured": source_row["featured"],
        "last_activity_at": source_row.get("last_activity_at"),
        "total_finds": source_row.get("total_finds", 0),
        "notable_finds": source_row.get("notable_finds", 0),
        "featured_finds": source_row.get("featured_finds", 0),
        "sample_titles": source_row.get("sample_titles", []),
        "recent_finds": [
            {
                "id": item["id"],
                "title": item["title"],
                "display_title": item.get("display_title") or item["title"],
                "url": item["url"],
                "finding_type": item["finding_type"],
                "featured": item.get("featured", False),
                "last_seen_at": item.get("last_seen_at"),
            }
            for item in source_row.get("recent_finds", [])
        ],
    }


@router.get("/library/sources.json")
async def library_sources_feed(
    response: Response,
    days: int = 60,
    limit: int = 200,
    watch_type: str | None = None,
    featured_only: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Public source directory for the /library browse experience.

    Returns approved sources with rolled-up activity. Used by the
    /library page filter chips and source listing. The window defaults
    to 60 days because the public browse surface should show sources
    that have been active recently, not the all-time list.
    """
    safe_days = min(max(days, 1), 365)
    safe_limit = min(max(limit, 1), 500)
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
    sources = await list_sources_with_activity(session, window_days=safe_days, limit=safe_limit)
    if watch_type:
        sources = [src for src in sources if src["watch_type"] == watch_type]
    if featured_only:
        sources = [src for src in sources if src.get("featured")]
    return {
        "sources": [_public_source_payload(row) for row in sources],
        "days": safe_days,
        "limit": safe_limit,
        "watch_type": watch_type or "",
        "featured_only": featured_only,
    }


@router.get("/library/sources/{source_id}.json")
async def library_source_detail(
    source_id: str,
    response: Response,
    days: int = 90,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Public detail for a single source: identity + recent activity.

    Looks up by id (UUID) or by slugified name so the frontend can link
    via either. 404s if the source doesn't exist or is inactive.
    """
    safe_days = min(max(days, 1), 365)
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"

    # Resolve by id first, then by exact name match.
    statement = select(DiscoverySource).where(DiscoverySource.id == source_id)
    result = await session.execute(statement)
    source = result.scalar_one_or_none()
    if source is None:
        statement = select(DiscoverySource).where(DiscoverySource.name == source_id)
        result = await session.execute(statement)
        source = result.scalar_one_or_none()
    if source is None or not source.active:
        raise HTTPException(status_code=404, detail="Source not found.")

    rollup = await list_sources_with_activity(session, window_days=safe_days, limit=500)
    matching = next((row for row in rollup if row["id"] == source.id), None)
    if matching is None:
        # The source exists but has no recent activity in the window. Return a
        # minimal payload so the frontend can still render the source page.
        matching = {
            "id": source.id,
            "name": source.name,
            "watch_type": source.watch_type,
            "target": source.target,
            "featured": source.featured,
            "last_activity_at": source.last_polled_at.isoformat() if source.last_polled_at else None,
            "total_finds": 0,
            "notable_finds": 0,
            "featured_finds": 0,
            "sample_titles": [],
            "recent_finds": [],
        }
    return {"source": _public_source_payload(matching), "days": safe_days}
