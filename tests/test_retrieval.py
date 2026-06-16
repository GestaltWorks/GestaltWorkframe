import httpx
import pytest

from gestaltworkframe.core.discovery_retrieval import DiscoveryContext
from gestaltworkframe.core.retrieval import KnowledgeRetriever


@pytest.mark.asyncio
async def test_retriever_uses_online_fallback_when_local_kb_has_no_context(monkeypatch):
    monkeypatch.setattr("gestaltworkframe.core.retrieval.kb_search_with_eligibility", lambda query, limit: ("Error searching knowledge base.", True))

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "ctx"
        assert request.url.params["tool"] == "reference_search"
        return httpx.Response(200, json={"content": "Result 1\nSource: online-backup\nContent:\nCTX docs"})

    retriever = KnowledgeRetriever(fallback_url="https://kb.example/search")

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original_client(transport=httpx.MockTransport(handler), base_url="https://kb.example")

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await retriever.retrieve("ctx", "reference_search")

    assert result.source == "fallback"
    assert "online-backup" in result.content


@pytest.mark.asyncio
async def test_retriever_keeps_local_result_when_fallback_is_unconfigured(monkeypatch):
    monkeypatch.setattr("gestaltworkframe.core.retrieval.kb_search_with_eligibility", lambda query, limit: ("No relevant information found in the knowledge base.", True))

    result = await KnowledgeRetriever(fallback_url="").retrieve("missing", "reference_search")

    assert result.source == "local"
    assert "No relevant information" in result.content


@pytest.mark.asyncio
async def test_retriever_expands_library_workflow_library_queries(monkeypatch):
    calls = []

    def fake_search(query: str, limit: int) -> tuple[str, bool]:
        calls.append(query)
        return ("Result 1\nSource: INDEX.md\nContent:\nDrop the `.bundle.json` into Automation via Automations → Workflows → Import Bundle.", True)

    monkeypatch.setattr("gestaltworkframe.core.retrieval.kb_search_with_eligibility", fake_search)

    result = await KnowledgeRetriever(fallback_url="").retrieve("How do I import a workflow bundle?", "workflow_pattern_search")

    assert result.query == "How do I import a workflow bundle?"
    assert "workflow examples import bundle" in calls[0]
    assert "INDEX.md" in result.content


@pytest.mark.asyncio
async def test_retriever_surfaces_approved_discovery_context(monkeypatch):
    monkeypatch.setattr("gestaltworkframe.core.retrieval.kb_search_with_eligibility", lambda query, limit: ("No relevant information found in the knowledge base.", True))

    async def fake_discovery_context(query: str, *, limit: int = 3) -> DiscoveryContext:
        return DiscoveryContext("Approved latest discovery context:\nResult 1\nSource: discovery/find-1\nContent:\nNew Automation workflow")

    monkeypatch.setattr("gestaltworkframe.core.retrieval.approved_discovery_context_result", fake_discovery_context)

    result = await KnowledgeRetriever(fallback_url="").retrieve("what is latest in LIBRARY", "reference_search")

    assert result.source == "discovery"
    assert "New Automation workflow" in result.content


@pytest.mark.asyncio
async def test_retriever_appends_approved_discovery_context(monkeypatch):
    monkeypatch.setattr("gestaltworkframe.core.retrieval.kb_search_with_eligibility", lambda query, limit: ("Result 1\nSource: docs.md\nContent:\nLibrary docs", True))

    async def fake_discovery_context(query: str, *, limit: int = 3) -> DiscoveryContext:
        return DiscoveryContext("Approved latest discovery context:\nResult 1\nSource: discovery/find-1\nContent:\nNew Automation workflow", cloud_llm_eligible=False)

    monkeypatch.setattr("gestaltworkframe.core.retrieval.approved_discovery_context_result", fake_discovery_context)

    result = await KnowledgeRetriever(fallback_url="").retrieve("latest LIBRARY updates", "reference_search")

    assert result.source == "local+discovery"
    assert "Library docs" in result.content
    assert "New Automation workflow" in result.content
    assert result.cloud_llm_eligible is False


@pytest.mark.asyncio
async def test_retriever_restricted_local_source_is_not_cloud_eligible(monkeypatch):
    # A privacy-restricted local source downgrades cloud-eligibility even with
    # no discovery context (the former TODO: local sources can now downgrade).
    monkeypatch.setattr(
        "gestaltworkframe.core.retrieval.kb_search_with_eligibility",
        lambda query, limit: ("Result 1\nSource: private.md\nContent:\nPrivate", False),
    )

    async def no_discovery(query: str, *, limit: int = 3) -> DiscoveryContext:
        return DiscoveryContext("")

    monkeypatch.setattr(
        "gestaltworkframe.core.retrieval.approved_discovery_context_result", no_discovery
    )

    result = await KnowledgeRetriever(fallback_url="").retrieve("q", "reference_search")
    assert result.source == "local"
    assert result.cloud_llm_eligible is False
