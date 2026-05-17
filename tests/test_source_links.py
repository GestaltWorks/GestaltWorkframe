from kb.source_links import SourceLinkProfile, library_entry_url, public_source_url


def test_public_source_url_uses_configured_source_profile():
    profile = SourceLinkProfile(
        source_name="client_kb",
        repository_url="https://github.com/example/client-kb",
        branch="stable",
        library_url="https://client.example/kb",
    )
    metadata = {"source_name": "client_kb", "source": "runbooks/new user.md"}

    assert public_source_url(metadata, profiles=(profile,)) == "https://github.com/example/client-kb/blob/stable/runbooks/new%20user.md"
    assert library_entry_url(metadata, profiles=(profile,)) == "https://client.example/kb?source=runbooks%2Fnew+user.md"


def test_public_source_url_falls_back_to_canonical_url():
    metadata = {"source_name": "client_kb", "source": "local.md", "canonical_url": "https://docs.example/landing"}

    assert public_source_url(metadata, profiles=()) == "https://docs.example/landing"
    assert library_entry_url(metadata, profiles=()) == ""
