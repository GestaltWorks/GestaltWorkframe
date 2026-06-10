"""github_repo_artifact_scan handler tests.

The handler emits ONE FindCandidate per top-level directory (the
"category"), not one per file. Leaf metadata travels in
raw_payload.children. The scheduler stores the rolled-up shape so the
admin UI surfaces "TimeZest (8 files)" instead of eight separate rows.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike
from gestaltworkframe.core.discovery_handlers.github_repo_artifact import _latest_commit_at, poll


_TREE_PAYLOAD = {
    "tree": [
        # TimeZest category: a bundle + a docs file + a schema. Three
        # children; the category's finding_type wins as workflow_bundle.
        {"path": "TimeZest/Option Generators/ListAgentsWorkflow.bundle.json",
         "type": "blob", "sha": "tz-bundle-sha", "size": 2048},
        {"path": "TimeZest/Subworkflows/Send Link/ReadMe.md",
         "type": "blob", "sha": "tz-doc-sha", "size": 1024},
        {"path": "TimeZest/schemas/appointment.schema.json",
         "type": "blob", "sha": "tz-schema-sha", "size": 512},
        # Account Management: two bundles.
        {"path": "Account Management/DeleteOrg.bundle.json",
         "type": "blob", "sha": "rm-org-sha", "size": 2048},
        {"path": "Account Management/DeleteUser.bundle.json",
         "type": "blob", "sha": "rm-user-sha", "size": 2048},
        # Apple Shortcuts + Siri: one bundle. Spaces and plus sign in
        # the category name must survive intact.
        {"path": "Apple Shortcuts + Siri/workflow_template.bundle.json",
         "type": "blob", "sha": "apple-sha", "size": 1024},
        # Top-level file (no category) — must NOT create a candidate.
        {"path": "README.md", "type": "blob", "sha": "readme-sha", "size": 100},
        # Noise paths that should be filtered out by score / path-term
        # rules even though they live under a category folder.
        {"path": ".github/workflows/ci.yml",
         "type": "blob", "sha": "ci-sha", "size": 200},
        {"path": "package.json",
         "type": "blob", "sha": "boring-sha", "size": 200},
    ]
}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _source(target: str = "example/repo", etag: str = "") -> DiscoverySourceLike:
    return DiscoverySourceLike(
        name="example_repo_artifacts",
        watch_type="github_repo_artifact_scan",
        target=target,
        etag=etag,
    )


def _route_handler(extra=None):
    """Build a handler that serves the standard tree payload and stubs
    commit-history lookups so the timestamp branch doesn't 404."""
    extra = extra or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example/repo":
            return httpx.Response(
                200,
                json={"default_branch": "main", "html_url": "https://github.com/example/repo"},
            )
        if request.url.path == "/repos/example/repo/git/trees/main":
            return httpx.Response(200, json=_TREE_PAYLOAD, headers={"ETag": '"tree-etag"'})
        if request.url.path == "/repos/example/repo/commits":
            path_param = request.url.params.get("path", "")
            iso = extra.get(path_param, "2026-05-05T12:00:00Z")
            return httpx.Response(
                200,
                json=[{"commit": {"committer": {"date": iso}}}],
            )
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_poll_emits_one_candidate_per_category():
    async with _client(_route_handler()) as client:
        result = await poll(_source(), client)

    assert result.status == "ok"
    assert result.etag == '"tree-etag"'
    titles = {find.title for find in result.finds}
    assert titles == {
        "example/repo/TimeZest",
        "example/repo/Account Management",
        "example/repo/Apple Shortcuts + Siri",
    }
    # external_id is stable across polls; the scheduler dedups on it
    # so subsequent polls update the same row rather than appending.
    for find in result.finds:
        assert find.external_id == f"category:{find.category}"
        assert find.url.endswith(f"/tree/HEAD/{find.category}")


@pytest.mark.asyncio
async def test_poll_packs_child_metadata_into_raw_payload():
    async with _client(_route_handler()) as client:
        result = await poll(_source(), client)

    timezest = next(f for f in result.finds if f.category == "TimeZest")
    assert timezest.child_count == 3
    payload = timezest.raw_payload
    assert payload["kind"] == "category_rollup"
    assert payload["category"] == "TimeZest"
    children_paths = {c["path"] for c in payload["children"]}
    assert children_paths == {
        "TimeZest/Option Generators/ListAgentsWorkflow.bundle.json",
        "TimeZest/Subworkflows/Send Link/ReadMe.md",
        "TimeZest/schemas/appointment.schema.json",
    }
    # Every child carries its kind + score so the admin UI can
    # render the file list without re-fetching.
    for child in payload["children"]:
        assert "kind" in child and "score" in child and "url" in child
    json.dumps(payload)  # JSON-serializable for raw_payload storage


@pytest.mark.asyncio
async def test_poll_category_finding_type_and_importance_match_dominant_child():
    """A category with bundles is high importance and finding_type
    workflow_bundle. A docs-only category would be normal importance."""
    async with _client(_route_handler()) as client:
        result = await poll(_source(), client)

    timezest = next(f for f in result.finds if f.category == "TimeZest")
    automation_mgmt = next(f for f in result.finds if f.category == "Account Management")
    assert timezest.finding_type == "workflow_bundle"
    assert timezest.importance_signal == "high"
    assert automation_mgmt.finding_type == "workflow_bundle"
    assert automation_mgmt.importance_signal == "high"


@pytest.mark.asyncio
async def test_poll_last_upstream_updated_at_pulled_from_commits_api():
    extras = {
        "TimeZest": "2026-05-12T10:00:00Z",
        "Account Management": "2026-05-08T08:30:00Z",
        "Apple Shortcuts + Siri": "2026-04-30T15:15:00Z",
    }
    async with _client(_route_handler(extra=extras)) as client:
        result = await poll(_source(), client)

    timezest = next(f for f in result.finds if f.category == "TimeZest")
    assert timezest.last_upstream_updated_at is not None
    assert timezest.last_upstream_updated_at.isoformat().startswith("2026-05-12")


@pytest.mark.asyncio
async def test_poll_skips_top_level_files_and_low_score_paths():
    """README.md at the repo root has no category; CI YAML is excluded
    by the negative-score prefix. Neither should produce a candidate."""
    async with _client(_route_handler()) as client:
        result = await poll(_source(), client)

    titles = {find.title for find in result.finds}
    assert "example/repo/README.md" not in titles
    assert "example/repo/.github" not in titles


@pytest.mark.asyncio
async def test_poll_uses_tree_conditional_headers_and_handles_304():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/example/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/example/repo/git/trees/main":
            assert request.headers["If-None-Match"] == '"previous"'
            return httpx.Response(304)
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await poll(_source(etag='"previous"'), client)

    assert result.status == "not_modified"
    assert result.finds == []
    assert result.etag == '"previous"'


@pytest.mark.asyncio
async def test_latest_commit_at_ignores_malformed_commit_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"commit": {"committer": "not-a-dict"}}])

    async with _client(handler) as client:
        assert await _latest_commit_at(client, "example/repo", "TimeZest") is None


@pytest.mark.asyncio
async def test_poll_rejects_malformed_target():
    async with httpx.AsyncClient() as client:
        result = await poll(_source(target="not/a/valid/repo"), client)

    assert result.finds == []
    assert result.status == "error"
    assert "Invalid github_repo_artifact_scan target" in result.error
