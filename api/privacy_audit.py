from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import os
from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import select

from api.services import require_admin_token
from gestaltworkframe.core.db import DiscoveryFind, async_session_maker
from gestaltworkframe.core.discovery_document import document_for_find


router = APIRouter(tags=["privacy-audit"])
PRIVACY_AUDIT_MAX_ROWS = int(os.getenv("PRIVACY_AUDIT_MAX_ROWS", "10000"))
PRIVACY_AUDIT_PAGE_SIZE = int(os.getenv("PRIVACY_AUDIT_PAGE_SIZE", "500"))


@router.get("/admin/api/privacy/audit.json")
async def privacy_audit(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    counts, refused_7d, scanned = _empty_counts(), 0, 0
    async with async_session_maker() as session:
        while scanned < PRIVACY_AUDIT_MAX_ROWS:
            limit = min(PRIVACY_AUDIT_PAGE_SIZE, PRIVACY_AUDIT_MAX_ROWS - scanned)
            statement = select(DiscoveryFind).order_by(DiscoveryFind.created_at.desc()).offset(scanned).limit(limit)
            batch = list((await session.execute(statement)).scalars())
            if not batch:
                break
            refused_7d += _add_finds(counts, batch, now)
            scanned += len(batch)
    return _payload(counts, refused_7d, now, scanned)


def privacy_audit_payload(finds: list[DiscoveryFind], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    counts = _empty_counts()
    refused_7d = _add_finds(counts, finds, now)
    return _payload(counts, refused_7d, now, len(finds))


def _empty_counts() -> dict[str, dict[str, int]]:
    return defaultdict(lambda: {"cloud_eligible": 0, "local_only": 0, "total": 0})


def _add_finds(counts: dict[str, dict[str, int]], finds: list[DiscoveryFind], now: datetime) -> int:
    cutoff = now - timedelta(days=7)
    refused_7d = 0
    for find in finds:
        document = document_for_find(find)
        connector_id = document.source.connector_id
        counts[connector_id]["total"] += 1
        if document.privacy.cloud_llm_eligible:
            counts[connector_id]["cloud_eligible"] += 1
        else:
            counts[connector_id]["local_only"] += 1
            if _aware(find.created_at) >= cutoff:
                refused_7d += 1
    return refused_7d


def _payload(counts: dict[str, dict[str, int]], refused_7d: int, now: datetime, scanned: int) -> dict[str, Any]:
    return {
        "generated_at": now.isoformat(),
        "per_connector": dict(sorted(counts.items())),
        "rolling_7_day_cloud_refused_count": refused_7d,
        "rolling_7_day_count_may_be_underreported": scanned >= PRIVACY_AUDIT_MAX_ROWS,
        "max_rows_scanned": PRIVACY_AUDIT_MAX_ROWS,
        "rows_scanned": scanned,
    }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
