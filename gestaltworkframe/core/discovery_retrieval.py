"""Retrieval context for approved discovery finds."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_
from sqlmodel import select

from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource, async_session_maker
from gestaltworkframe.core.discovery_document import document_for_find

logger = logging.getLogger(__name__)

LATEST_TERMS = ("latest", "recent", "new", "what's new", "discovery", "discoveries", "library latest")
PUBLIC_STATUSES = ("approved", "published")


@dataclass(frozen=True)
class DiscoveryContext:
    content: str
    cloud_llm_eligible: bool = True


async def approved_discovery_context(query: str, *, limit: int = 3) -> str:
    """Return approved discovery finds when the query asks for recent signals."""

    return (await approved_discovery_context_result(query, limit=limit)).content


async def approved_discovery_context_result(query: str, *, limit: int = 3) -> DiscoveryContext:
    """Return approved discovery context and its aggregate privacy eligibility."""

    if not _wants_latest_discoveries(query):
        return DiscoveryContext("")
    rows = []
    try:
        async with async_session_maker() as session:
            statement = (
                select(DiscoveryFind, DiscoverySource)
                .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
                .where(DiscoveryFind.status.in_(PUBLIC_STATUSES))
                .where(_query_filter(query))
                .order_by(DiscoveryFind.decided_at.desc(), DiscoveryFind.created_at.desc())
                .limit(max(1, min(limit, 10)))
            )
            rows = (await session.execute(statement)).all()
    except Exception:
        logger.exception("Approved discovery retrieval failed")
        return DiscoveryContext("")
    if not rows:
        return DiscoveryContext("")
    lines = ["Approved latest discovery context:"]
    cloud_llm_eligible = True
    for index, (find, source) in enumerate(rows, start=1):
        document = document_for_find(find, source)
        cloud_llm_eligible = cloud_llm_eligible and document.privacy.cloud_llm_eligible
        lines.append(
            f"Result {index}\n"
            f"Source: discovery/{find.id} ({source.name})\n"
            f"Link: {find.url}\n"
            f"Content:\n{document.body_text}"
        )
    return DiscoveryContext("\n\n".join(lines), cloud_llm_eligible=cloud_llm_eligible)


def _wants_latest_discoveries(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in LATEST_TERMS)


def _query_filter(query: str):
    terms = [term for term in query.lower().replace("?", " ").split() if len(term) >= 4][:5]
    if not terms:
        return True
    clauses = []
    for term in terms:
        like = f"%{term}%"
        clauses.extend(
            [
                DiscoveryFind.title.ilike(like),
                DiscoveryFind.summary_text.ilike(like),
                DiscoverySource.name.ilike(like),
            ]
        )
    return or_(*clauses)