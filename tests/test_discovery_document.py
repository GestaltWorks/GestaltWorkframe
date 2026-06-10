from datetime import datetime, timezone

from gestaltworkframe.core.db import DiscoveryFind
from gestaltworkframe.core.discovery_document import document_for_find


def test_document_for_find_falls_back_when_canonical_json_is_invalid():
    find = DiscoveryFind(
        id="find-1",
        discovery_source_id="source-1",
        finding_type="release",
        external_id="external-1",
        title="Fallback title",
        url="https://example.com/item",
        summary_text="Fallback summary",
        canonical_document_json="{",
        created_at=datetime.now(timezone.utc),
    )

    document = document_for_find(find)

    assert document.doc_id == "discovery:find-1"
    assert "Fallback summary" in document.body_text


def test_document_for_find_falls_back_when_canonical_json_is_too_large(monkeypatch):
    monkeypatch.setattr("gestaltworkframe.core.discovery_document.MAX_CANONICAL_DOCUMENT_JSON_BYTES", 4)
    find = DiscoveryFind(
        id="find-2",
        discovery_source_id="source-1",
        finding_type="release",
        external_id="external-2",
        title="Large fallback",
        url="https://example.com/large",
        summary_text="Large fallback summary",
        canonical_document_json="large-json",
        created_at=datetime.now(timezone.utc),
    )

    document = document_for_find(find)

    assert document.doc_id == "discovery:find-2"
    assert "Large fallback summary" in document.body_text


def test_discovery_find_to_document_normalizes_naive_timestamps():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    find = DiscoveryFind(
        id="find-3",
        discovery_source_id="source-1",
        finding_type="release",
        external_id="external-3",
        title="Naive timestamp",
        url="https://example.com/naive",
        created_at=naive,
        first_seen_at=naive,
        last_seen_at=naive,
    )

    document = document_for_find(find)

    assert document.timestamps.ingested_at.tzinfo is not None
    assert document.timestamps.source_created_at.tzinfo is not None


def test_discovery_find_to_document_falls_back_when_external_id_is_empty():
    find = DiscoveryFind(
        id="find-4",
        discovery_source_id="source-1",
        finding_type="release",
        external_id="",
        title="Missing external id",
        url="https://example.com/missing",
        created_at=datetime.now(timezone.utc),
    )

    document = document_for_find(find)

    assert document.source.external_id == "find-4"