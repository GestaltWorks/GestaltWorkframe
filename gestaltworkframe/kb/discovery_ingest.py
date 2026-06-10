"""Promotion helpers for approved discovery finds."""

from __future__ import annotations

from langchain_core.documents import Document

from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource
from mcp_servers.kb_server import get_vectorstore


def discovery_find_document(find: DiscoveryFind, source: DiscoverySource) -> Document:
    content = (
        f"Approved discovery find: {find.title}\n"
        f"Source: {source.name} ({source.watch_type})\n"
        f"URL: {find.url}\n"
        f"Finding type: {find.finding_type}\n"
        f"Importance: {find.importance_signal}\n"
        f"Summary: {find.summary_text or 'No summary available.'}\n"
    )
    return Document(
        page_content=content,
        metadata={
            "source": f"discovery/{find.id}",
            "source_name": source.name,
            "corpus": "approved_discovery",
            "type": ".url",
            "source_type": "discovery_find",
            "canonical_url": find.url,
            "provenance": f"Discovery source {source.name}: {source.target}",
            "license_notes": "Approved discovery link and summary only unless separately reviewed.",
            "attribution": source.name,
            "trust_tier": "approved_public_discovery",
            "refresh_policy": "discovery_review_queue",
            "display_policy": "public_after_source_review",
            "retrieval_policy": "approved_for_grounded_retrieval_as_reference_metadata",
            "curriculum_policy": "not_approved_by_default",
            "agent_access_policy": "read_only_public_url_reference",
            "secret_handling": "public_url_only",
            "public_display": True,
            "retrieval": True,
            "curriculum": False,
        },
    )


def ingest_approved_find_into_chroma(find: DiscoveryFind, source: DiscoverySource) -> None:
    """Add an approved discovery find to the active Chroma collection."""

    document = discovery_find_document(find, source)
    get_vectorstore().add_documents([document])


def purge_discovery_find_from_chroma(find_id: str) -> None:
    """Remove one discovery find from the active Chroma collection."""

    vectorstore = get_vectorstore()
    collection = getattr(vectorstore, "_collection", None)
    if collection is not None:
        collection.delete(where={"source": f"discovery/{find_id}"})
        return
    vectorstore.delete(where={"source": f"discovery/{find_id}"})
