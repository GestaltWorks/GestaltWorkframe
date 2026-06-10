"""github_repo_artifact_scan handler.

Walks a public GitHub repository tree and emits ONE FindCandidate per
top-level directory (the "category"), not one per leaf file. The
category is the first path segment under the repo root. Leaf metadata
travels in `raw_payload.children` so library retrieval can keep citing
specific bundles, schemas, or docs.

This rollup shape is the operator-curatable unit: TimeZest is one
signal, regardless of whether it contains 8 files or 80. The "Feature
in ticker / Send to newsletter / Dismiss" actions act on the category;
the leaf files are visible by expanding a category row in the admin UI.

Per-category metadata stored on the candidate:
- category: first path segment (e.g. "TimeZest")
- child_count: number of artifact-worthy leaf files in the category
- last_upstream_updated_at: latest commit timestamp touching the
  category folder, fetched from the repo's commits API. Falls back to
  None on quota issues; the admin UI then renders only `last_seen_at`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register
from gestaltworkframe.core.discovery_handlers.github_repo import GITHUB_API_ROOT, _auth_headers, _conditional_headers
from kb.target_safety import GITHUB_REPO_RE

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20
MAX_CATEGORIES_PER_SCAN = 25
MAX_CHILDREN_PER_CATEGORY = 200
# Cap the per-category commit lookup so a 50-category repo doesn't burn
# the GitHub API quota on every poll. Categories beyond this are still
# emitted; they just don't carry an upstream timestamp on this pass.
MAX_COMMIT_LOOKUPS_PER_SCAN = 10

ARTIFACT_EXTENSIONS = (
    ".bundle.json", ".workflow.json", ".schema.json", ".json", ".yaml", ".yml",
    ".md", ".html", ".jinja", ".j2", ".py", ".ps1",
)
HIGH_VALUE_PATH_TERMS = (
    "workflow", "workflows", "bundle", "schema", "schemas", "jinja",
    "app-builder", "app_builder", "filters", "examples", "docs",
)


async def _repo_metadata(http: httpx.AsyncClient, owner_repo: str, token: str = "") -> tuple[str, str]:
    response = await http.get(
        f"{GITHUB_API_ROOT}/repos/{owner_repo}",
        headers=_auth_headers(token),
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    return str(data.get("default_branch") or "main"), str(
        data.get("html_url") or f"https://github.com/{owner_repo}"
    )


async def _repo_tree(
    http: httpx.AsyncClient,
    owner_repo: str,
    ref: str,
    source: DiscoverySourceLike,
) -> tuple[list[dict[str, Any]], str, bool]:
    headers = _auth_headers(source.auth_token)
    headers.update(_conditional_headers(source))
    response = await http.get(
        f"{GITHUB_API_ROOT}/repos/{owner_repo}/git/trees/{ref}",
        params={"recursive": "1"},
        headers=headers,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code == 304:
        return [], source.etag, True
    response.raise_for_status()
    data = response.json()
    tree = data.get("tree") if isinstance(data, dict) else []
    return tree if isinstance(tree, list) else [], response.headers.get("etag", "") or "", False


def _artifact_kind(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".bundle.json") or "bundle" in lower:
        return "workflow_bundle"
    if "workflow" in lower:
        return "workflow_artifact"
    if "schema" in lower:
        return "schema_artifact"
    if lower.endswith((".jinja", ".j2")) or "jinja" in lower:
        return "jinja_artifact"
    if lower.endswith(".html") or "app-builder" in lower or "app_builder" in lower:
        return "app_builder_artifact"
    if lower.endswith(".md") or "/docs/" in lower or lower.startswith("docs/"):
        return "documentation_artifact"
    return "repo_artifact"


def _artifact_score(path: str) -> int:
    lower = path.lower()
    score = 0
    if lower.endswith(".bundle.json"):
        score += 8
    if lower.endswith((".workflow.json", ".schema.json")):
        score += 6
    for term in HIGH_VALUE_PATH_TERMS:
        if term in lower:
            score += 2
    if lower.startswith((".github/", "node_modules/", "dist/", "build/", ".venv/")):
        score -= 20
    return score


def _is_candidate(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith(ARTIFACT_EXTENSIONS):
        return False
    return _artifact_score(lower) > 0


def _category_for_path(path: str) -> str:
    """First path segment under the repo root, treated as the category.

    Returns "" for top-level files (which we skip from the rollup; a
    bare README.md isn't a meaningful curatable category)."""
    if "/" not in path:
        return ""
    return path.split("/", 1)[0]


def _category_kind(children: list[dict[str, Any]]) -> str:
    """Pick the dominant finding_type for the category row. Bundles win
    over workflows win over schemas etc., matching the importance
    score order. Falls back to repo_artifact when nothing matches."""
    counts: dict[str, int] = {}
    for child in children:
        counts[child["kind"]] = counts.get(child["kind"], 0) + 1
    for preferred in (
        "workflow_bundle", "workflow_artifact", "schema_artifact",
        "jinja_artifact", "app_builder_artifact", "documentation_artifact",
    ):
        if counts.get(preferred):
            return preferred
    return "repo_artifact"


def _category_importance(children: list[dict[str, Any]]) -> str:
    """High when the category contains any bundle/workflow/schema item.
    Normal otherwise. Matches the previous per-file rule."""
    for child in children:
        if child["kind"] in {"workflow_bundle", "workflow_artifact", "schema_artifact"}:
            return "high"
    return "normal"


def _category_summary(category: str, children: list[dict[str, Any]]) -> str:
    """Operator-readable one-liner. Lists the top three child filenames
    so the row is scannable in the admin panel without expanding."""
    sorted_children = sorted(children, key=lambda c: -c.get("score", 0))[:3]
    file_list = ", ".join(c["path"].rsplit("/", 1)[-1] for c in sorted_children)
    if len(children) > 3:
        file_list += f", and {len(children) - 3} more"
    kind_count = len(children)
    return (
        f"{category} contains {kind_count} artifact"
        + ("s" if kind_count != 1 else "")
        + (f" including {file_list}." if file_list else ".")
    )


async def _latest_commit_at(
    http: httpx.AsyncClient,
    owner_repo: str,
    path: str,
    token: str = "",
) -> datetime | None:
    """Latest commit timestamp touching `path`. Returns None on any
    failure so the scheduler keeps making progress; the admin UI just
    falls back to last_seen_at for the "last updated" column."""
    try:
        response = await http.get(
            f"{GITHUB_API_ROOT}/repos/{owner_repo}/commits",
            params={"path": path, "per_page": "1"},
            headers=_auth_headers(token),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            return None
        body = response.json()
        if not isinstance(body, list) or not body:
            return None
        commit = body[0].get("commit", {}) if isinstance(body[0], dict) else {}
        if not isinstance(commit, dict):
            return None
        committer = commit.get("committer") or commit.get("author") or {}
        if not isinstance(committer, dict):
            return None
        iso = committer.get("date")
        if not iso:
            return None
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _build_category_candidate(
    owner_repo: str,
    repo_url: str,
    category: str,
    children: list[dict[str, Any]],
    last_upstream: datetime | None,
) -> FindCandidate:
    kind = _category_kind(children)
    importance = _category_importance(children)
    url = f"{repo_url}/tree/HEAD/{category}"
    summary = _category_summary(category, children)
    payload = {
        "kind": "category_rollup",
        "repo": owner_repo,
        "category": category,
        "child_count": len(children),
        "children": children,
        "url": url,
    }
    return FindCandidate(
        finding_type=kind,
        # external_id is stable across polls so the scheduler dedups on
        # the category, not on the leaf files. Subsequent polls update
        # the row in place with the fresh child list.
        external_id=f"category:{category}",
        title=f"{owner_repo}/{category}",
        url=url,
        summary_text=summary,
        raw_payload=payload,
        importance_signal=importance,
        category=category,
        child_count=len(children),
        last_upstream_updated_at=last_upstream,
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    owner_repo = source.target.strip().strip("/")
    if not GITHUB_REPO_RE.fullmatch(owner_repo):
        return PollResult(finds=[], status="error", error=f"Invalid github_repo_artifact_scan target: {source.target!r}")

    try:
        default_branch, repo_url = await _repo_metadata(http, owner_repo, source.auth_token)
        tree, etag, not_modified = await _repo_tree(http, owner_repo, default_branch, source)
    except httpx.HTTPError as exc:
        logger.warning("github_repo_artifact_scan failed for %s: %s", owner_repo, exc)
        return PollResult(finds=[], status="error", error=str(exc))

    # Group artifact-worthy blobs by their first path segment.
    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not _is_candidate(path):
            continue
        category = _category_for_path(path)
        if not category:
            continue
        bucket = by_category.setdefault(category, [])
        if len(bucket) >= MAX_CHILDREN_PER_CATEGORY:
            continue
        bucket.append({
            "path": path,
            "sha": str(item.get("sha") or ""),
            "size": item.get("size"),
            "kind": _artifact_kind(path),
            "score": _artifact_score(path),
            "url": f"{repo_url}/blob/HEAD/{path}",
        })

    # Rank categories by the sum of their child scores (so a category
    # full of bundles outranks a category of plain .md files), cap at
    # MAX_CATEGORIES_PER_SCAN.
    ranked = sorted(
        by_category.items(),
        key=lambda kv: (sum(child.get("score", 0) for child in kv[1]), kv[0]),
        reverse=True,
    )[:MAX_CATEGORIES_PER_SCAN]

    # Fetch upstream commit timestamps for the top categories. We cap
    # the lookup so a poll on a wide repo doesn't burn the API quota.
    candidates: list[FindCandidate] = []
    lookup_budget = MAX_COMMIT_LOOKUPS_PER_SCAN
    for category, children in ranked:
        last_upstream: datetime | None = None
        if lookup_budget > 0:
            last_upstream = await _latest_commit_at(http, owner_repo, category, source.auth_token)
            lookup_budget -= 1
        candidates.append(_build_category_candidate(
            owner_repo, repo_url, category, children, last_upstream,
        ))

    return PollResult(
        finds=candidates,
        etag=etag or source.etag,
        status="not_modified" if not_modified else "ok",
    )


register("github_repo_artifact_scan", poll)
