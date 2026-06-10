from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from gestalt_connector_protocol import BodyStructured, Document, HeadingSection, ParagraphSection, SourceMetadata, Timestamps

from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource


logger = logging.getLogger(__name__)
MAX_CANONICAL_DOCUMENT_JSON_BYTES = 2_000_000


def discovery_find_to_document(find: DiscoveryFind, source: DiscoverySource | None = None) -> Document:
    payload = _raw_payload(find)
    source_name = source.name if source else "discovery"
    body = _body_text(find, payload)
    return Document(
        doc_id=f"discovery:{find.id}",
        source=SourceMetadata(
            connector_id="discovery",
            connector_name=source_name,
            source_system="discovery",
            source_type=find.finding_type,
            source_url=find.url,
            external_id=find.external_id or str(find.id),
            parent_external_id=find.discovery_source_id,
            title=find.title,
            labels={"status": find.status, "importance": find.importance_signal},
        ),
        body_text=body,
        body_structured=BodyStructured(
            sections=[
                HeadingSection(text=find.title, level=1),
                ParagraphSection(text=find.summary_text or _payload_summary(payload)),
            ]
        ),
        tags=["discovery", find.finding_type, source_name, find.importance_signal],
        timestamps=Timestamps(
            source_created_at=_aware(find.first_seen_at),
            source_updated_at=_aware(find.last_seen_at),
            ingested_at=_aware(find.created_at),
            last_seen_at=_aware(find.last_seen_at),
        ),
    )


def document_json_for_find(find: DiscoveryFind, source: DiscoverySource | None = None) -> str:
    return discovery_find_to_document(find, source).model_dump_json()


def document_for_find(find: DiscoveryFind, source: DiscoverySource | None = None) -> Document:
    if find.canonical_document_json:
        if len(find.canonical_document_json.encode("utf-8")) > MAX_CANONICAL_DOCUMENT_JSON_BYTES:
            logger.warning("Canonical document JSON for discovery find %s is too large; regenerating", find.id)
            return discovery_find_to_document(find, source)
        try:
            return Document.model_validate_json(find.canonical_document_json)
        except Exception:
            logger.warning("Invalid canonical document JSON for discovery find %s; regenerating", find.id)
    return discovery_find_to_document(find, source)


def _raw_payload(find: DiscoveryFind) -> dict[str, object]:
    try:
        parsed = json.loads(find.raw_payload or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_text(find: DiscoveryFind, payload: dict[str, object]) -> str:
    parts = [find.title, find.summary_text, _payload_summary(payload), find.url]
    return "\n".join(part for part in parts if part).strip() or find.title


def _payload_summary(payload: dict[str, object]) -> str:
    for key in ("description", "summary", "body", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
