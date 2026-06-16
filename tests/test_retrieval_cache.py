"""Tests for the per-process retrieval cache on KnowledgeRetriever.

The cache is keyed on (query, tool_name, limit). Entries expire after
RETRIEVAL_CACHE_TTL_SECONDS and the cache is bounded to
RETRIEVAL_CACHE_MAX_ENTRIES. Only retrieval results that have usable
context are cached so empty/error responses don't pin the slot.
"""

from __future__ import annotations

import pytest

import gestaltworkframe.core.retrieval as retrieval_mod
from gestaltworkframe.core.discovery_retrieval import DiscoveryContext
from gestaltworkframe.core.retrieval import KnowledgeRetriever, RetrievalResult


@pytest.fixture
def patched_kb(monkeypatch):
    """Replace kb_search with a counter-driven stub and ensure discovery context is empty."""
    calls = {"count": 0, "queries": []}

    def fake_kb_search(query, limit):
        calls["count"] += 1
        calls["queries"].append(query)
        return (f"context-for[{query}]", True)

    async def empty_discovery(query, *, limit=3):
        return DiscoveryContext("")

    monkeypatch.setattr(retrieval_mod, "kb_search_with_eligibility", fake_kb_search)
    monkeypatch.setattr(retrieval_mod, "approved_discovery_context_result", empty_discovery)
    return calls


@pytest.mark.asyncio
async def test_retrieve_caches_repeat_queries(patched_kb):
    retriever = KnowledgeRetriever(fallback_url="")

    first = await retriever.retrieve("how do I import a Automation bundle", "kb_overview", 5)
    second = await retriever.retrieve("how do I import a Automation bundle", "kb_overview", 5)

    assert isinstance(first, RetrievalResult)
    assert first.content == second.content
    # Cache hit -> kb_search invoked exactly once.
    assert patched_kb["count"] == 1


@pytest.mark.asyncio
async def test_retrieve_does_not_cache_across_different_queries(patched_kb):
    retriever = KnowledgeRetriever(fallback_url="")

    await retriever.retrieve("query one", "kb_overview", 5)
    await retriever.retrieve("query two", "kb_overview", 5)
    await retriever.retrieve("query one", "kb_overview", 5)

    # Three calls: two distinct queries each go to kb_search once, the
    # repeat of "query one" hits the cache.
    assert patched_kb["count"] == 2


@pytest.mark.asyncio
async def test_clear_cache_forces_rerun(patched_kb):
    retriever = KnowledgeRetriever(fallback_url="")

    await retriever.retrieve("repeat me", "kb_overview", 5)
    retriever.clear_cache()
    await retriever.retrieve("repeat me", "kb_overview", 5)

    assert patched_kb["count"] == 2


@pytest.mark.asyncio
async def test_cache_eviction_bounds_size(monkeypatch, patched_kb):
    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_CACHE_MAX_ENTRIES", 2)
    retriever = KnowledgeRetriever(fallback_url="")

    await retriever.retrieve("q1", "kb_overview", 5)
    await retriever.retrieve("q2", "kb_overview", 5)
    await retriever.retrieve("q3", "kb_overview", 5)  # evicts q1
    await retriever.retrieve("q1", "kb_overview", 5)  # cache miss -> recompute

    assert patched_kb["count"] == 4
    assert len(retriever._cache) == 2


@pytest.mark.asyncio
async def test_empty_context_not_cached(monkeypatch):
    """Empty/error retrieval results should not pin a cache slot."""
    calls = {"count": 0}

    def fake_kb_search(query, limit):
        calls["count"] += 1
        return ("no relevant information found", True)

    async def empty_discovery(query, *, limit=3):
        return DiscoveryContext("")

    monkeypatch.setattr(retrieval_mod, "kb_search_with_eligibility", fake_kb_search)
    monkeypatch.setattr(retrieval_mod, "approved_discovery_context_result", empty_discovery)

    retriever = KnowledgeRetriever(fallback_url="")
    await retriever.retrieve("nothing-here", "kb_overview", 5)
    await retriever.retrieve("nothing-here", "kb_overview", 5)

    # Both calls hit kb_search because empty results aren't cached.
    assert calls["count"] == 2
