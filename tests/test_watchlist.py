from dataclasses import replace

import pytest

from kb.target_safety import validate_discovery_target, validate_public_https_url
from kb.watchlist import (
    ALLOWED_REFRESH_CADENCES,
    ALLOWED_WATCH_TYPES,
    CADENCE_SECONDS,
    WatchedSource,
    refresh_seconds,
    validate_watchlist,
)
from kb.watchlist_seed import WATCHLIST_SEED


def _watch(name: str = "test_repo", watch_type: str = "github_repo_watch") -> WatchedSource:
    return WatchedSource(
        name=name,
        watch_type=watch_type,
        target="example/repo" if watch_type == "github_repo_watch" else "https://example.test/feed.xml",
        description="test description",
        refresh_cadence="daily",
        canonical_url="https://example.test",
        provenance="test provenance",
        license_notes="test license",
        attribution="test attribution",
        trust_tier="test_tier",
        display_policy="test_display",
        retrieval_policy="test_retrieval",
        curriculum_policy="test_curriculum",
        agent_access_policy="read_only",
        secret_handling="no_secrets",
        importance_floor="normal",
    )


def test_validate_watchlist_accepts_seed():
    validate_watchlist(WATCHLIST_SEED)


def test_validate_watchlist_rejects_duplicate_names():
    with pytest.raises(ValueError) as exc:
        validate_watchlist((_watch(), _watch()))
    assert "duplicate watched source name: test_repo" in str(exc.value)


def test_validate_watchlist_rejects_unknown_watch_type():
    bad = replace(_watch(), watch_type="ldap_directory_scan")
    with pytest.raises(ValueError, match="unsupported watch_type: ldap_directory_scan"):
        validate_watchlist((bad,))


def test_validate_watchlist_rejects_unknown_refresh_cadence():
    bad = replace(_watch(), refresh_cadence="continuous")
    with pytest.raises(ValueError, match="unsupported refresh_cadence: continuous"):
        validate_watchlist((bad,))


def test_validate_watchlist_rejects_unknown_importance_floor():
    bad = replace(_watch(), importance_floor="critical")
    with pytest.raises(ValueError, match="unsupported importance_floor: critical"):
        validate_watchlist((bad,))


def test_validate_watchlist_requires_target_and_attribution():
    missing_target = replace(_watch(), target="")
    missing_attribution = replace(_watch("attr"), attribution="")
    with pytest.raises(ValueError) as exc:
        validate_watchlist((missing_target,))
    assert "requires target" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        validate_watchlist((missing_attribution,))
    assert "requires attribution" in str(exc.value)


def test_validate_watchlist_rejects_private_url_targets():
    bad = replace(_watch(watch_type="rss_feed"), target="http://127.0.0.1:8080/feed.xml")

    with pytest.raises(ValueError) as exc:
        validate_watchlist((bad,))

    assert "must use https://" in str(exc.value)


def test_validate_watchlist_rejects_github_url_targets():
    bad = replace(_watch(), target="https://github.com/example/repo")

    with pytest.raises(ValueError) as exc:
        validate_watchlist((bad,))

    assert "must be a GitHub owner/repo name" in str(exc.value)


def test_validate_watchlist_rejects_youtube_url_targets():
    bad = replace(_watch(watch_type="youtube_channel_watch"), target="https://www.youtube.com/@msp4msps")

    with pytest.raises(ValueError) as exc:
        validate_watchlist((bad,))

    assert "not a URL" in str(exc.value)


def test_refresh_seconds_maps_cadences():
    for cadence in ALLOWED_REFRESH_CADENCES:
        seconds = refresh_seconds(replace(_watch(), refresh_cadence=cadence))
        assert seconds == CADENCE_SECONDS[cadence]


def test_seed_contains_only_supported_types():
    for entry in WATCHLIST_SEED:
        assert entry.watch_type in ALLOWED_WATCH_TYPES


def test_seed_contains_sample_github_repo():
    names = {entry.name for entry in WATCHLIST_SEED}
    assert "sample_github_repo" in names


def test_seed_contains_sample_github_artifact_rescan():
    by_name = {entry.name: entry for entry in WATCHLIST_SEED}
    rescan = by_name["sample_github_repo_artifacts"]
    assert rescan.watch_type == "github_repo_artifact_scan"
    assert rescan.target == "octocat/Hello-World"
    assert rescan.refresh_cadence == "weekly"


def test_seed_contains_sample_rss_feed():
    by_name = {entry.name: entry for entry in WATCHLIST_SEED}
    assert by_name["sample_rss_feed"].watch_type == "rss_feed"


def test_validate_discovery_target_closed_by_default_for_unknown_type():
    # Defense in depth: if a future watch_type lands in ALLOWED_WATCH_TYPES
    # without a matching validator branch, the SSRF guard should fail closed
    # rather than letting unconstrained input through to the handler.
    with pytest.raises(ValueError, match="no target validator"):
        validate_discovery_target("imap_mailbox_watch", "anything", source_name="future")


def test_validate_discovery_target_every_allowed_type_has_a_validator():
    # Every entry in ALLOWED_WATCH_TYPES must have a corresponding branch in
    # validate_discovery_target. The test feeds each watch_type a clearly bad
    # target and asserts that the rejection comes from the type-specific
    # branch (not the closed-by-default fallthrough).
    bad_inputs = {
        "github_repo_watch": ("not a repo", "owner/repo"),
        "github_repo_artifact_scan": ("not a repo", "owner/repo"),
        "github_topic_watch": ("not_a_topic!", "topic slug"),
        "github_user_org_watch": ("has spaces", "user or org name"),
        "rss_feed": ("http://10.0.0.1/feed", "https"),
        "subreddit_watch": ("https://reddit.com/r/msp", "subreddit name"),
        "youtube_channel_watch": ("https://youtube.com/@x", "not a URL"),
        "web_diff": ("ftp://example.com/page", "https"),
        "saved_search": ("https://google.com/?q=x", "saved search query"),
    }
    for watch_type in ALLOWED_WATCH_TYPES:
        assert watch_type in bad_inputs, f"{watch_type} has no bad-input fixture"
        target, expected_fragment = bad_inputs[watch_type]
        with pytest.raises(ValueError, match=expected_fragment):
            validate_discovery_target(watch_type, target, source_name="check")


def test_validate_public_https_url_rejects_loopback_and_private_addresses():
    bad_addresses = [
        "http://example.com/feed",  # not https
        "https://localhost/feed",
        "https://127.0.0.1/feed",
        "https://192.0.2.4:8080/feed",
        "https://192.168.1.1/",
        "https://server.internal/",
        "https://user:pass@example.com/",
    ]
    for bad in bad_addresses:
        with pytest.raises(ValueError):
            validate_public_https_url(bad, source_name="check")
