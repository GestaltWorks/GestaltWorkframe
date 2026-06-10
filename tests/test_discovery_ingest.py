from __future__ import annotations

from datetime import datetime, timezone

from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource
from kb.discovery_ingest import discovery_find_document, ingest_approved_find_into_chroma


def _find() -> DiscoveryFind:
    now = datetime.now(timezone.utc)
    return DiscoveryFind(
        id="find-1",
        discovery_source_id="source-1",
        finding_type="post",
        external_id="external-1",
        title="New Automation workflow pattern",
        url="https://example.com/workflow",
        summary_text="A useful public workflow signal.",
        first_seen_at=now,
        last_seen_at=now,
    )


def _source() -> DiscoverySource:
    return DiscoverySource(id="source-1", name="example_feed", watch_type="rss_feed", target="https://example.com/feed.xml")


def test_discovery_find_document_has_retrieval_metadata():
    doc = discovery_find_document(_find(), _source())

    assert "New Automation workflow pattern" in doc.page_content
    assert doc.metadata["corpus"] == "approved_discovery"
    assert doc.metadata["retrieval"] is True
    assert doc.metadata["canonical_url"] == "https://example.com/workflow"


def test_ingest_approved_find_adds_document(monkeypatch):
    calls = []

    class Store:
        def add_documents(self, docs):
            calls.extend(docs)

    monkeypatch.setattr("kb.discovery_ingest.get_vectorstore", lambda: Store())

    ingest_approved_find_into_chroma(_find(), _source())

    assert len(calls) == 1
    assert calls[0].metadata["source"] == "discovery/find-1"