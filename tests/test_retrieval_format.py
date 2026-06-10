from types import SimpleNamespace

import pytest

from gestaltworkframe.kb import retrieval_format, source_links
from gestaltworkframe.kb.retrieval_format import NO_RELEVANT_INFO_MESSAGE, format_search_results
from gestaltworkframe.kb.source_links import SourceLinkProfile


def _doc(source: str, content: str, doc_type: str = ".md", source_name: str = "test"):
    return SimpleNamespace(
        metadata={"source": source, "type": doc_type, "source_name": source_name},
        page_content=content,
    )


@pytest.fixture
def library_profile(monkeypatch):
    profile = SourceLinkProfile(
        source_name="library",
        repository_url="https://github.com/example-org/library-repo",
        library_url="https://example.com/library",
    )
    monkeypatch.setattr(source_links, "SOURCE_LINK_PROFILES", (profile,))
    monkeypatch.setattr(retrieval_format, "public_source_url", lambda md: source_links.public_source_url(md, (profile,)))
    monkeypatch.setattr(retrieval_format, "library_entry_url", lambda md: source_links.library_entry_url(md, (profile,)))
    return profile


def test_format_search_results_returns_shared_empty_message():
    assert format_search_results([]) == NO_RELEVANT_INFO_MESSAGE


def test_format_search_results_includes_source_links_and_library_links(library_profile):
    result = format_search_results([
        (
            _doc(
                "workflows/demo bundle.json",
                "importable bundle",
                ".json",
                source_name="library",
            ),
            0.12345,
        )
    ])

    assert "[Result 1] (relevance: 0.88)" in result
    assert "Source type: .json" in result
    assert "Internal source path: workflows/demo bundle.json" in result
    assert (
        "Source URL: https://github.com/example-org/library-repo/blob/main/"
        "workflows/demo%20bundle.json"
    ) in result
    assert (
        "Library entry: https://example.com/library?source="
        "workflows%2Fdemo+bundle.json"
    ) in result
    assert "Body:\nimportable bundle" in result
