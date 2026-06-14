"""Tests for the watchlist seed canonical-URL builder.

`_canonical_url` maps each watch_type to its public canonical URL. The seed
itself only exercises a couple of branches at import time, so these cover the
rest directly to keep the URL mapping honest.
"""

from __future__ import annotations

from gestaltworkframe.kb import watchlist_seed as ws


def test_canonical_url_per_watch_type():
    assert ws._canonical_url("github_repo_artifact_scan", "octocat/Hello") == "https://github.com/octocat/Hello"
    assert ws._canonical_url("github_topic_watch", "topic:automation") == "https://github.com/topics/automation"
    assert ws._canonical_url("github_user_org_watch", "octocat") == "https://github.com/octocat"
    assert ws._canonical_url("subreddit_watch", "r/python") == "https://www.reddit.com/r/python/"
    assert ws._canonical_url("subreddit_watch", "python") == "https://www.reddit.com/r/python/"
    assert ws._canonical_url("youtube_channel_watch", "@handle") == "https://www.youtube.com/@handle"
    assert ws._canonical_url("youtube_channel_watch", "c/Channel") == "https://www.youtube.com/c/Channel"
    assert ws._canonical_url("web_diff", "https://example.com/page") == "https://example.com/page"
    assert ws._canonical_url("saved_search", "home automation") == "https://search.brave.com/search?q=home+automation"


def test_canonical_url_unknown_type_returns_target():
    assert ws._canonical_url("mystery_watch", "raw-target") == "raw-target"


def test_seed_is_built_and_valid():
    # Building runs validate_watchlist at import; confirm the public seed is
    # non-empty and every entry carries the required identity fields.
    assert len(ws.WATCHLIST_SEED) >= 1
    for entry in ws.WATCHLIST_SEED:
        assert entry.name and entry.watch_type and entry.target
