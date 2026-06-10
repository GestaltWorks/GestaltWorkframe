"""github_repo_watch handler.

Polls a single GitHub repository for new releases and a commit-delta signal on
the default branch since the last successful poll. Uses the GitHub REST API
with conditional-fetch headers (ETag, If-Modified-Since) to stay polite.

The token is resolved from `source.auth_token` (set by the scheduler from
the key store) and falls back to the `APP_GITHUB_TOKEN` environment variable. Per the project's non-negotiables the token never enters LLM context;
this handler only uses it for HTTP auth. Anonymous fallback works for public
repos but is sharply rate-limited (60 requests/hour).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from gestaltworkframe.core.discovery_handlers import (
    DiscoverySourceLike,
    FindCandidate,
    PollResult,
    register,
)

logger = logging.getLogger(__name__)

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_USER_AGENT = "gestalt-workframe-discovery"
DEFAULT_TIMEOUT_SECONDS = 15
RELEASE_PAGE_SIZE = 10
COMMIT_PAGE_SIZE = 30


def _auth_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": GITHUB_USER_AGENT,
    }
    resolved = token or os.getenv("APP_GITHUB_TOKEN", "").strip()
    if resolved:
        headers["Authorization"] = f"Bearer {resolved}"
    return headers


def _conditional_headers(source: DiscoverySourceLike) -> dict[str, str]:
    headers: dict[str, str] = {}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    return headers


def _release_payload(release: dict[str, Any]) -> dict[str, Any]:
    # Trim what we persist to the handful of fields the queue/UI actually uses.
    return {
        "id": release.get("id"),
        "tag_name": release.get("tag_name"),
        "name": release.get("name"),
        "draft": bool(release.get("draft")),
        "prerelease": bool(release.get("prerelease")),
        "published_at": release.get("published_at"),
        "html_url": release.get("html_url"),
        "body_excerpt": (release.get("body") or "")[:1024],
        "author_login": (release.get("author") or {}).get("login"),
    }


def _commit_payload(commit: dict[str, Any]) -> dict[str, Any]:
    commit_info = commit.get("commit") or {}
    author = commit_info.get("author") or {}
    return {
        "sha": commit.get("sha"),
        "html_url": commit.get("html_url"),
        "message_excerpt": (commit_info.get("message") or "").splitlines()[0][:240],
        "committed_at": author.get("date"),
        "author_name": author.get("name"),
        "author_login": (commit.get("author") or {}).get("login"),
    }


async def _fetch_releases(
    http: httpx.AsyncClient, owner_repo: str, token: str = ""
) -> list[dict[str, Any]]:
    url = f"{GITHUB_API_ROOT}/repos/{owner_repo}/releases"
    response = await http.get(
        url,
        params={"per_page": RELEASE_PAGE_SIZE},
        headers=_auth_headers(token),
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else []


async def _fetch_commits(
    http: httpx.AsyncClient,
    owner_repo: str,
    source: DiscoverySourceLike,
) -> tuple[list[dict[str, Any]], str, str, bool]:
    """Return commits, etag, last-modified, and not_modified flag."""

    url = f"{GITHUB_API_ROOT}/repos/{owner_repo}/commits"
    headers = _auth_headers(source.auth_token)
    headers.update(_conditional_headers(source))
    response = await http.get(
        url,
        params={"per_page": COMMIT_PAGE_SIZE},
        headers=headers,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if response.status_code == 304:
        return [], source.etag, source.last_modified, True
    if response.status_code == 404:
        return [], "", "", False
    response.raise_for_status()
    payload = response.json()
    commits = payload if isinstance(payload, list) else []
    return (
        commits,
        response.headers.get("etag", "") or "",
        response.headers.get("last-modified", "") or "",
        False,
    )


def _release_to_candidate(owner_repo: str, release: dict[str, Any]) -> FindCandidate:
    release_id = release.get("id")
    tag = release.get("tag_name") or "untagged"
    title = release.get("name") or tag
    importance = "high" if not release.get("prerelease") and not release.get("draft") else "normal"
    summary_lines = [f"Release {tag} on {owner_repo}."]
    body = (release.get("body") or "").strip()
    if body:
        summary_lines.append(body.splitlines()[0][:240])
    return FindCandidate(
        finding_type="release",
        external_id=f"release:{release_id}",
        title=f"{owner_repo}: {title}",
        url=release.get("html_url") or f"https://github.com/{owner_repo}/releases",
        summary_text="\n".join(summary_lines),
        raw_payload={"kind": "release", "repo": owner_repo, "release": _release_payload(release)},
        importance_signal=importance,
    )


def _commit_to_candidate(owner_repo: str, commit: dict[str, Any]) -> FindCandidate:
    sha = commit.get("sha") or ""
    commit_info = commit.get("commit") or {}
    message = (commit_info.get("message") or "").splitlines()[0][:160]
    return FindCandidate(
        finding_type="commit_delta",
        external_id=f"commit:{sha}",
        title=f"{owner_repo} commit {sha[:7]}: {message}" if message else f"{owner_repo} commit {sha[:7]}",
        url=commit.get("html_url") or f"https://github.com/{owner_repo}/commit/{sha}",
        summary_text=message,
        raw_payload={"kind": "commit_delta", "repo": owner_repo, "commit": _commit_payload(commit)},
        importance_signal="low",
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    owner_repo = source.target.strip().strip("/")
    if "/" not in owner_repo:
        return PollResult(finds=[], status="error", error=f"Invalid github_repo_watch target: {source.target!r}")

    finds: list[FindCandidate] = []
    new_etag = source.etag
    new_last_modified = source.last_modified
    not_modified = False

    try:
        releases = await _fetch_releases(http, owner_repo, source.auth_token)
        finds.extend(_release_to_candidate(owner_repo, release) for release in releases)

        commits, etag, last_modified, not_modified = await _fetch_commits(http, owner_repo, source)
        finds.extend(_commit_to_candidate(owner_repo, commit) for commit in commits)
        new_etag = etag
        new_last_modified = last_modified
    except httpx.HTTPError as exc:
        logger.warning("github_repo_watch poll failed for %s: %s", owner_repo, exc)
        return PollResult(finds=[], status="error", error=str(exc))

    return PollResult(
        finds=finds,
        etag=new_etag,
        last_modified=new_last_modified,
        status="not_modified" if not_modified and not finds else "ok",
    )


register("github_repo_watch", poll)
