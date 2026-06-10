"""Tests for discovery credential resolution via key_store.

Phase 5: auth_token field on DiscoverySourceLike; _WATCH_TYPE_TO_PROVIDER map;
key_store threading in run_one_pass and _poll_source.
"""

from __future__ import annotations

import pytest

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike
from gestaltworkframe.core.discovery_scheduler import _WATCH_TYPE_TO_PROVIDER


def test_discovery_source_like_has_auth_token_field():
    """DiscoverySourceLike.auth_token defaults to empty string."""
    src = DiscoverySourceLike(name="t", watch_type="github_repo_watch", target="owner/repo")
    assert src.auth_token == ""


def test_discovery_source_like_auth_token_roundtrip():
    """auth_token is preserved on the dataclass."""
    src = DiscoverySourceLike(
        name="t", watch_type="saved_search", target="q", auth_token="tok-abc"
    )
    assert src.auth_token == "tok-abc"


def test_watch_type_to_provider_map_covers_all_github_types():
    github_types = {"github_repo_watch", "github_repo_artifact_scan", "github_topic_watch", "github_user_org_watch"}
    for wt in github_types:
        assert _WATCH_TYPE_TO_PROVIDER.get(wt) == "github", f"{wt} not mapped to github"


def test_watch_type_to_provider_map_covers_saved_search():
    assert _WATCH_TYPE_TO_PROVIDER.get("saved_search") == "brave"


def test_watch_type_to_provider_map_no_entry_for_rss():
    """RSS handler needs no auth token; should not be in the map."""
    assert "rss_feed" not in _WATCH_TYPE_TO_PROVIDER


@pytest.mark.asyncio
async def test_poll_source_injects_auth_token_from_key_store(tmp_path):
    """_poll_source resolves auth_token from key_store when available."""
    from unittest.mock import AsyncMock, MagicMock
    from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, PollResult, register
    from gestaltworkframe.core.discovery_scheduler import _poll_source
    import asyncio
    from gestaltworkframe.core.key_store import ApiKeyStore

    captured: list[DiscoverySourceLike] = []

    async def _fake_handler(src: DiscoverySourceLike, http) -> PollResult:
        captured.append(src)
        return PollResult(finds=[])

    # Temporarily register a test watch_type
    register("_test_type_creds", _fake_handler)

    from gestaltworkframe.core.db import DiscoverySource
    source = MagicMock(spec=DiscoverySource)
    source.name = "test-source"
    source.watch_type = "_test_type_creds"
    source.target = "owner/repo"
    source.etag = ""
    source.last_modified = ""

    key_store = ApiKeyStore(str(tmp_path / "keys.db"))
    await key_store.init()
    await key_store.set_key("_test_type_creds_doesnt_exist", "gh-tok", "admin")

    # Patch _WATCH_TYPE_TO_PROVIDER to map our test type to a provider we can set
    import gestaltworkframe.core.discovery_scheduler as sched_mod
    original_map = sched_mod._WATCH_TYPE_TO_PROVIDER
    sched_mod._WATCH_TYPE_TO_PROVIDER = {**original_map, "_test_type_creds": "github"}

    await key_store.set_key("github", "gh-stored-token", "admin")

    http_client = AsyncMock()
    semaphore = asyncio.Semaphore(1)
    _, result, err = await _poll_source(
        source, http_client, semaphore,
        per_source_timeout_seconds=5.0,
        key_store=key_store,
        admin_token="admin",
    )

    sched_mod._WATCH_TYPE_TO_PROVIDER = original_map

    assert err == ""
    assert result is not None
    assert len(captured) == 1
    assert captured[0].auth_token == "gh-stored-token"


@pytest.mark.asyncio
async def test_poll_source_no_key_store_leaves_auth_token_empty(tmp_path):
    """When key_store=None, auth_token stays empty (env-fallback in handler)."""
    from unittest.mock import AsyncMock, MagicMock
    from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, PollResult, register
    from gestaltworkframe.core.discovery_scheduler import _poll_source
    import asyncio
    from gestaltworkframe.core.db import DiscoverySource

    captured: list[DiscoverySourceLike] = []

    async def _fake_handler2(src: DiscoverySourceLike, http) -> PollResult:
        captured.append(src)
        return PollResult(finds=[])

    register("_test_type_no_ks", _fake_handler2)

    source = MagicMock(spec=DiscoverySource)
    source.name = "test-no-ks"
    source.watch_type = "_test_type_no_ks"
    source.target = "q"
    source.etag = ""
    source.last_modified = ""

    http_client = AsyncMock()
    semaphore = asyncio.Semaphore(1)
    _, result, err = await _poll_source(
        source, http_client, semaphore,
        per_source_timeout_seconds=5.0,
        key_store=None,
        admin_token="",
    )
    assert err == ""
    assert len(captured) == 1
    assert captured[0].auth_token == ""


@pytest.mark.asyncio
async def test_run_one_pass_accepts_key_store_param(tmp_path):
    """run_one_pass accepts key_store and admin_token without error."""
    import inspect
    from gestaltworkframe.core.discovery_scheduler import run_one_pass
    sig = inspect.signature(run_one_pass)
    assert "key_store" in sig.parameters
    assert "admin_token" in sig.parameters
    assert sig.parameters["key_store"].default is None
    assert sig.parameters["admin_token"].default == ""
