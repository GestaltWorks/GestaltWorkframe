from dataclasses import replace
from pathlib import Path

import pytest

from kb.source_registry import CorpusSource, source_metadata, validate_source_registry


def _source(name: str = "client_kb") -> CorpusSource:
    return CorpusSource(
        name=name,
        path=Path("corpus/client"),
        source_type="directory",
        description="Client runbooks and SOPs.",
        canonical_url="https://client.example/kb",
        provenance="Approved client internal source registry.",
        license_notes="Client-owned internal material; not for public display without approval.",
        attribution="Client internal operations team.",
        trust_tier="client_private_corpus",
        refresh_policy="scheduled_reindex_after_review",
        display_policy="private_internal_only",
        retrieval_policy="approved_for_grounded_retrieval",
        curriculum_policy="not_approved_by_default",
        agent_access_policy="read_only_filesystem; no secret-bearing paths",
        secret_handling="agents_do_not_see_secrets",
        public_display=False,
        retrieval=True,
        curriculum=False,
    )


def test_source_metadata_normalizes_paths_and_policy_fields():
    source = _source()

    metadata = source_metadata(source, r"runbooks\new user.md", ".md")

    assert metadata["source"] == "runbooks/new user.md"
    assert metadata["source_name"] == "client_kb"
    assert metadata["canonical_url"] == "https://client.example/kb"
    assert metadata["retrieval_policy"] == "approved_for_grounded_retrieval"
    assert metadata["agent_access_policy"] == "read_only_filesystem; no secret-bearing paths"
    assert metadata["secret_handling"] == "agents_do_not_see_secrets"
    assert metadata["public_display"] is False
    assert metadata["retrieval"] is True
    assert metadata["curriculum"] is False


def test_validate_source_registry_accepts_complete_client_source():
    validate_source_registry((_source(),))


def test_validate_source_registry_rejects_duplicate_and_unsupported_sources():
    bad_type = replace(_source("bad"), source_type="spreadsheet")

    with pytest.raises(ValueError) as exc:
        validate_source_registry((_source(), _source(), bad_type))

    assert "duplicate source name: client_kb" in str(exc.value)
    assert "bad has unsupported source_type: spreadsheet" in str(exc.value)


def test_validate_source_registry_requires_public_canonical_url():
    public_source = replace(_source("public_client_kb"), public_display=True, canonical_url="")

    with pytest.raises(ValueError, match="public_display requires canonical_url"):
        validate_source_registry((public_source,))
