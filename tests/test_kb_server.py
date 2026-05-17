from types import SimpleNamespace

import pytest

import mcp_servers.kb_server as kb_server
from kb import retrieval_format, source_links
from kb.source_links import SourceLinkProfile


def _doc(source: str, content: str, doc_type: str = ".md", source_name: str = "test"):
    return SimpleNamespace(metadata={"source": source, "type": doc_type, "source_name": source_name}, page_content=content)


class _VectorStore:
    def __init__(self, results=None, raises: Exception | None = None) -> None:
        self.results = results or []
        self.raises = raises
        self.calls = []

    def similarity_search_with_score(self, query: str, k: int):
        self.calls.append({"query": query, "k": k})
        if self.raises:
            raise self.raises
        return self.results


class _Collection:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


def test_kb_search_bounds_negative_result_count(monkeypatch):
    store = _VectorStore([
        (_doc("a.md", "first"), 0.1),
        (_doc("b.md", "second"), 0.2),
    ])
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    result = kb_server.kb_search("ctx", num_results=-5)

    assert store.calls[0]["k"] == 20
    assert "Result 1" in result
    assert "Result 2" not in result


def test_kb_search_caps_large_result_count(monkeypatch):
    docs = [(_doc(f"doc-{idx}.md", f"content {idx}"), float(idx)) for idx in range(20)]
    store = _VectorStore(docs)
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    result = kb_server.kb_search("ctx", num_results=999)

    assert store.calls[0]["k"] == 50
    assert result.count("Result ") == kb_server.MAX_NUM_RESULTS


def test_kb_search_dedupes_source_and_content(monkeypatch):
    store = _VectorStore([
        (_doc("same.md", "duplicate content"), 0.1),
        (_doc("same.md", "duplicate content"), 0.2),
    ])
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    result = kb_server.kb_search("ctx", num_results=5)

    assert result.count("Result ") == 1


def test_kb_search_includes_public_library_links(monkeypatch):
    profile = SourceLinkProfile(
        source_name="library",
        repository_url="https://github.com/example-org/library-repo",
        library_url="https://example.com/library",
    )
    monkeypatch.setattr(source_links, "SOURCE_LINK_PROFILES", (profile,))
    monkeypatch.setattr(retrieval_format, "public_source_url", lambda md: source_links.public_source_url(md, (profile,)))
    monkeypatch.setattr(retrieval_format, "library_entry_url", lambda md: source_links.library_entry_url(md, (profile,)))
    store = _VectorStore([
        (_doc("workflows/demo bundle.json", "importable bundle", ".json", source_name="library"), 0.1),
    ])
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    result = kb_server.kb_search("library workflow bundle", num_results=1)

    assert "Internal source path: workflows/demo bundle.json" in result
    assert "Source type: .json" in result
    assert "Source URL: https://github.com/example-org/library-repo/blob/main/workflows/demo%20bundle.json" in result
    assert "Library entry: https://example.com/library?source=workflows%2Fdemo+bundle.json" in result


def test_kb_search_returns_generic_error(monkeypatch, caplog):
    store = _VectorStore(raises=RuntimeError("secret local path B:\\private\\db"))
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    result = kb_server.kb_search("ctx")

    assert result == "Error searching knowledge base."
    assert "secret local path" not in result
    assert "Knowledge base search failed" in caplog.text


def test_kb_search_returns_no_results_message(monkeypatch):
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: _VectorStore([]))

    assert kb_server.kb_search("missing") == "No relevant information found in the knowledge base."


def test_get_vectorstore_initializes_once_under_lock(monkeypatch):
    calls = []

    class FakeEmbeddings:
        def __init__(self, model_name: str) -> None:
            calls.append(("embeddings", model_name))

    class FakeChroma:
        def __init__(self, persist_directory: str, embedding_function) -> None:
            calls.append(("chroma", persist_directory, embedding_function.__class__.__name__))

    monkeypatch.setattr(kb_server, "_vectorstore", None)
    monkeypatch.setattr(kb_server, "HuggingFaceEmbeddings", FakeEmbeddings)
    monkeypatch.setattr(kb_server, "Chroma", FakeChroma)

    first = kb_server.get_vectorstore()
    second = kb_server.get_vectorstore()

    assert first is second
    assert [call[0] for call in calls] == ["embeddings", "chroma"]


def test_vectorstore_document_count_reads_chroma_collection(monkeypatch):
    store = _VectorStore([])
    store._collection = _Collection(42)
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: store)

    assert kb_server.vectorstore_document_count() == 42


def test_vectorstore_document_count_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(kb_server, "get_vectorstore", lambda: _VectorStore([]))

    assert kb_server.vectorstore_document_count() is None


def test_overview_rerank_uses_portable_source_patterns():
    score = kb_server._rerank_score(
        "what is this platform",
        "skills/automation/README.md",
        "This is an automation platform for building workflows.",
        2.0,
    )
    api_score = kb_server._rerank_score(
        "what is this platform",
        "reference/openapi/schema.json",
        "Automation APIs",
        2.0,
    )

    assert score < 2.0
    assert api_score > score


def test_workflow_library_rerank_boosts_importable_bundle_sources():
    index_score = kb_server._rerank_score(
        "library workflow examples import bundle",
        "INDEX.md",
        "## Tier 2: workflow libraries (bundle.json, ready to import)\nDrop the `.bundle.json` into Automation via Automations → Workflows → Import Bundle.",
        1.0,
    )
    schema_score = kb_server._rerank_score(
        "library workflow examples import bundle",
        "repos/shiftnerd__OpenAPISchemas/jamfproclassic.json",
        "allowed file extension html",
        1.0,
    )

    assert index_score < 0
    assert schema_score > index_score
