from __future__ import annotations

import json

import httpx
import pytest

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike
from gestaltworkframe.core.discovery_handlers.github_repo import poll


_RELEASE_PAYLOAD = [
    {
        "id": 184293,
        "tag_name": "v1.2.0",
        "name": "v1.2.0 — Stable",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-05-10T18:00:00Z",
        "html_url": "https://github.com/example/repo/releases/tag/v1.2.0",
        "body": "Bug fixes and new filters\nAdds new schema validator",
        "author": {"login": "octocat"},
    }
]
_COMMITS_PAYLOAD = [
    {
        "sha": "abc1234567890",
        "html_url": "https://github.com/example/repo/commit/abc1234567890",
        "commit": {
            "message": "Refactor scheduler loop",
            "author": {"name": "Dev User", "date": "2026-05-11T12:00:00Z"},
        },
        "author": {"login": "devuser"},
    }
]


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _source(target: str = "example/repo", etag: str = "", last_modified: str = "") -> DiscoverySourceLike:
    return DiscoverySourceLike(
        name="example_repo",
        watch_type="github_repo_watch",
        target=target,
        etag=etag,
        last_modified=last_modified,
    )


@pytest.mark.asyncio
async def test_poll_returns_release_and_commit_finds():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=_RELEASE_PAYLOAD)
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200,
                json=_COMMITS_PAYLOAD,
                headers={"ETag": '"abc"', "Last-Modified": "Mon, 11 May 2026 12:00:00 GMT"},
            )
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.status == "ok"
    assert result.error == ""
    assert result.etag == '"abc"'
    assert result.last_modified == "Mon, 11 May 2026 12:00:00 GMT"
    finding_types = {find.finding_type for find in result.finds}
    assert finding_types == {"release", "commit_delta"}
    release = next(find for find in result.finds if find.finding_type == "release")
    assert release.external_id == "release:184293"
    assert release.url == "https://github.com/example/repo/releases/tag/v1.2.0"
    assert release.importance_signal == "high"
    commit = next(find for find in result.finds if find.finding_type == "commit_delta")
    assert commit.external_id == "commit:abc1234567890"
    assert "Refactor scheduler loop" in commit.title


@pytest.mark.asyncio
async def test_poll_handles_304_not_modified_on_commits():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/commits"):
            assert request.headers["If-None-Match"] == '"prev"'
            return httpx.Response(304)
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await poll(_source(etag='"prev"'), client)

    assert result.finds == []
    assert result.status == "not_modified"
    assert result.etag == '"prev"'


@pytest.mark.asyncio
async def test_poll_rejects_malformed_target():
    async with httpx.AsyncClient() as client:
        result = await poll(_source(target="not-a-valid-repo"), client)

    assert result.finds == []
    assert result.status == "error"
    assert "Invalid github_repo_watch target" in result.error


@pytest.mark.asyncio
async def test_poll_returns_error_on_http_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(handler) as client:
        result = await poll(_source(), client)

    assert result.finds == []
    assert result.status == "error"
    assert "500" in result.error


@pytest.mark.asyncio
async def test_release_excerpt_is_persisted_in_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=_RELEASE_PAYLOAD)
        return httpx.Response(200, json=[])

    async with _client(handler) as client:
        result = await poll(_source(), client)

    release = next(find for find in result.finds if find.finding_type == "release")
    assert release.raw_payload["release"]["tag_name"] == "v1.2.0"
    assert release.raw_payload["release"]["body_excerpt"].startswith("Bug fixes")
    # Ensure the payload is JSON-serializable end-to-end.
    json.dumps(release.raw_payload)
