import logging
from dataclasses import replace
from pathlib import Path

import pytest

from gestaltworkframe.kb import ingest
from gestaltworkframe.kb.ingest import (
    ANTHROPIC_AI_FLUENCY_4D_SOURCE,
    API_CHEAT_SHEET_SOURCE,
    EXTERNAL_REPORT_SOURCE,
    CorpusSource,
    load_corpus_sources,
    load_directory_source,
    load_external_url_reference_source,
)


def _source(path: Path) -> CorpusSource:
    return CorpusSource(
        name="test_source",
        path=path,
        source_type="directory",
        description="Test corpus.",
        canonical_url="https://example.test/corpus",
        provenance="unit test",
        license_notes="test license",
        attribution="test attribution",
        trust_tier="test_trusted",
        refresh_policy="manual",
        display_policy="public_after_review",
        retrieval_policy="retrieval_allowed",
        curriculum_policy="curriculum_allowed",
        agent_access_policy="read_only; no_secrets",
        secret_handling="agents_do_not_see_secrets",
        public_display=True,
        retrieval=True,
        curriculum=True,
    )


def test_directory_source_metadata_includes_policy_fields(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "workflow.json").write_text('{"name":"demo"}', encoding="utf-8")

    docs = load_directory_source(_source(corpus))

    assert len(docs) == 1
    metadata = docs[0].metadata
    assert metadata["source"] == "workflow.json"
    assert metadata["source_name"] == "test_source"
    assert metadata["type"] == ".json"
    assert metadata["source_type"] == "directory"
    assert metadata["canonical_url"] == "https://example.test/corpus"
    assert metadata["retrieval_policy"] == "retrieval_allowed"
    assert metadata["agent_access_policy"] == "read_only; no_secrets"
    assert metadata["secret_handling"] == "agents_do_not_see_secrets"
    assert metadata["public_display"] is True
    assert metadata["public_url"] == "https://example.test/corpus"


def test_main_corpus_metadata_uses_canonical_url(tmp_path):
    corpus = tmp_path / "corpus"
    workflow_dir = corpus / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "demo bundle.json").write_text('{"name":"demo"}', encoding="utf-8")
    source = replace(ingest.MAIN_CORPUS_SOURCE, path=corpus)

    docs = load_directory_source(source)

    assert docs[0].metadata["source"] == "workflows/demo bundle.json"
    assert docs[0].metadata["public_url"] == ingest.MAIN_CORPUS_SOURCE.canonical_url


def test_missing_directory_source_returns_empty_list(tmp_path):
    docs = load_directory_source(_source(tmp_path / "missing"))

    assert docs == []


def test_directory_source_skips_hidden_parent_dirs(tmp_path):
    corpus = tmp_path / "corpus"
    hidden = corpus / ".git.broken-from-onedrive-clone-attempt"
    hidden.mkdir(parents=True)
    (hidden / "workflow.md").write_text("hidden", encoding="utf-8")

    docs = load_directory_source(_source(corpus))

    assert docs == []


def test_directory_source_logs_ignored_decode_errors(tmp_path, caplog):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "bad.md").write_bytes(b"valid\xfftext")

    with caplog.at_level(logging.DEBUG, logger="gestaltworkframe.kb.ingest"):
        docs = load_directory_source(_source(corpus))

    assert docs[0].page_content == "validtext"
    assert "Ignoring undecodable bytes" in caplog.text


def test_load_corpus_sources_includes_all_configured_sources(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.md").write_text("one", encoding="utf-8")
    (second / "two.md").write_text("two", encoding="utf-8")

    docs = load_corpus_sources((_source(first), _source(second)))

    sources = {doc.metadata["source"] for doc in docs}
    assert sources == {"one.md", "two.md"}


def test_load_corpus_sources_dispatches_external_url_reference():
    docs = load_corpus_sources((EXTERNAL_REPORT_SOURCE,))

    assert len(docs) == 1
    assert docs[0].metadata["source_type"] == "external_url_reference"
    assert docs[0].metadata["type"] == ".url"
    assert EXTERNAL_REPORT_SOURCE.description in docs[0].page_content
    assert EXTERNAL_REPORT_SOURCE.canonical_url in docs[0].page_content


def test_external_url_reference_requires_canonical_url():
    source = replace(EXTERNAL_REPORT_SOURCE, canonical_url="")

    with pytest.raises(ValueError, match="canonical_url"):
        load_external_url_reference_source(source)


def test_main_uses_configured_corpus_sources(monkeypatch, tmp_path):
    calls = {}
    doc = ingest.Document(page_content="hello", metadata={"source": "one.md"})

    def fake_load_corpus_sources():
        calls["loaded"] = True
        return [doc]

    class Splitter:
        def __init__(self, chunk_size, chunk_overlap):
            calls["chunk_size"] = chunk_size
            calls["chunk_overlap"] = chunk_overlap

        def split_documents(self, docs):
            calls["split_docs"] = docs
            return docs

    class FakeChroma:
        @staticmethod
        def from_documents(documents, embedding, persist_directory):
            calls["documents"] = documents
            calls["embedding"] = embedding
            calls["persist_directory"] = persist_directory

    monkeypatch.setattr(ingest, "load_corpus_sources", fake_load_corpus_sources)
    monkeypatch.setattr(ingest, "RecursiveCharacterTextSplitter", Splitter)
    monkeypatch.setattr(ingest, "HuggingFaceEmbeddings", lambda model_name: ("embeddings", model_name))
    monkeypatch.setattr(ingest, "Chroma", FakeChroma)
    monkeypatch.setattr(ingest, "CHROMA_DB_DIR", tmp_path / "chroma")

    ingest.main()

    assert calls["loaded"] is True
    assert calls["chunk_size"] == ingest.DEFAULT_CHUNK_SIZE
    assert calls["chunk_overlap"] == ingest.DEFAULT_CHUNK_OVERLAP
    assert calls["documents"] == [doc]


def test_api_cheat_sheet_is_public_display_source():
    assert API_CHEAT_SHEET_SOURCE.display_policy == "public"
    assert API_CHEAT_SHEET_SOURCE.public_display is True
    assert API_CHEAT_SHEET_SOURCE.retrieval is True


def test_external_report_is_reference_source():
    docs = load_external_url_reference_source(EXTERNAL_REPORT_SOURCE)

    assert len(docs) == 1
    metadata = docs[0].metadata
    assert EXTERNAL_REPORT_SOURCE in ingest.CORPUS_SOURCES
    assert metadata["source_name"] == "sample_external_report"
    assert metadata["type"] == ".url"
    assert metadata["retrieval"] is True
    assert "do_not_invent_report_statistics" in metadata["agent_access_policy"]
    assert EXTERNAL_REPORT_SOURCE.canonical_url in docs[0].page_content


def test_anthropic_ai_fluency_4d_is_reference_source():
    docs = load_external_url_reference_source(ANTHROPIC_AI_FLUENCY_4D_SOURCE)

    assert len(docs) == 1
    metadata = docs[0].metadata
    assert ANTHROPIC_AI_FLUENCY_4D_SOURCE in ingest.CORPUS_SOURCES
    assert metadata["source_name"] == "anthropic_academy_ai_fluency_4d_framework"
    assert metadata["curriculum"] is True
    assert "Delegation, Description, Discernment, and Diligence" in docs[0].page_content
    assert "do_not_republish_pdf_text" in metadata["agent_access_policy"]
