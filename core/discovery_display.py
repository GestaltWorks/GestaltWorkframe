"""Human-readable display layer for discovery sources and findings.

Discovery handler titles and seed source names are designed for machine
deduplication and audit logs, not for human eyes. Raw patterns like
"Owner/Repo matches topic:something" or "SOURCE_SLUG_UPPERCASE" leaked
into the public ticker and admin UI before this module existed.

`display_source_name` and `display_finding_title` produce human-readable
versions while leaving the raw fields untouched (the raw values still
back search, dedup, and audit). Public payloads include both shapes
so the frontend can render `display_*` and fall back to raw values for
sources/finds that don't match any known pattern.

When the pattern set grows, add a new (regex, formatter) entry to the
appropriate tuple and a test case in tests/test_discovery_display.py.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Source name humanization
# ---------------------------------------------------------------------------

_SOURCE_SPECIAL_CASES = {
    "discovery_scout": "Discovery scout",
}


def display_source_name(raw_name: str) -> str:
    """Turn a source slug into a human-readable name.

    Special-cases known seed names. For operator-added sources that follow
    `owner_repo` or `org_repo_variant` patterns (snake_case), split on
    underscores into "Owner / Repo Variant" form. For everything else,
    replace underscores with spaces and title-case the result.
    """
    if not raw_name:
        return ""
    raw = raw_name.strip()
    if raw in _SOURCE_SPECIAL_CASES:
        return _SOURCE_SPECIAL_CASES[raw]

    # Owner/repo style (already has a slash): preserve it.
    if "/" in raw:
        return raw

    # GitHub-style watchlist names like "Owner_Repo_Workflows_artifacts". We
    # can't perfectly recover the original capitalization, but we can produce
    # something a human can read.
    parts = raw.split("_")
    # Strip trailing "artifacts" / "topic_matches" suffixes - those are watch
    # type hints, not part of the source identity.
    suffix_strip = {"artifacts", "topics", "matches", "watch", "watches"}
    while parts and parts[-1].lower() in suffix_strip:
        parts.pop()
    if not parts:
        parts = raw.split("_")

    # Title-case each segment unless it's already mixed-case (preserve known
    # casing from operator-added sources).
    out: list[str] = []
    for part in parts:
        if any(ch.isupper() for ch in part) and any(ch.islower() for ch in part):
            out.append(part)  # already mixed-case, e.g. "community-org"
        else:
            out.append(part.capitalize())
    return " ".join(out)


# ---------------------------------------------------------------------------
# Finding title humanization
# ---------------------------------------------------------------------------

_GITHUB_TOPIC_PATTERN = re.compile(r"^(?P<repo>[^ /]+/[^ ]+) matches topic:(?P<topic>\S+)\s*$")
_GITHUB_ARTIFACT_PATTERN = re.compile(r"^(?P<repo>[^ /]+/[^ ]+) artifact:\s*(?P<path>.+)$")
_GITHUB_COMMIT_PATTERN = re.compile(r"^(?P<repo>[^ /]+/[^ ]+) commit (?P<sha>[0-9a-f]{7}):\s*(?P<message>.+)$")
_GITHUB_COMMIT_NO_MSG_PATTERN = re.compile(r"^(?P<repo>[^ /]+/[^ ]+) commit (?P<sha>[0-9a-f]{7})\s*$")
_GITHUB_USER_REPO_PATTERN = re.compile(r"^(?P<account>[^ ]+) repository:\s*(?P<repo>.+)$")
_SUBREDDIT_FALLBACK_PATTERN = re.compile(r"^r/(?P<sub>\S+) post$")


def display_finding_title(raw_title: str, finding_type: str = "", watch_type: str = "") -> str:
    """Produce a human-readable headline for a discovery find.

    Recognized handler patterns get rewritten:
    - github_topic: "X/Y matches topic:foo" -> "New foo repo: X/Y"
    - github_repo_artifact: "X/Y artifact: path/file" -> "X/Y - path/file"
    - github_repo commit: "X/Y commit abc1234: message" -> "X/Y commit: message (abc1234)"
    - github_user_org: "Account repository: name" -> "New repo from Account: name"
    - subreddit fallback "r/X post" stays as-is.

    For everything else (RSS items, blog posts, real release notes,
    etc.) the raw title is already a sentence-shaped string from the
    upstream source, so return it unchanged.
    """
    if not raw_title:
        return ""
    title = raw_title.strip()

    if (m := _GITHUB_TOPIC_PATTERN.match(title)):
        return f"New {m.group('topic')} repo: {m.group('repo')}"
    if (m := _GITHUB_USER_REPO_PATTERN.match(title)):
        return f"New repo from {m.group('account')}: {m.group('repo')}"
    if (m := _GITHUB_ARTIFACT_PATTERN.match(title)):
        return f"{m.group('repo')} - {m.group('path')}"
    if (m := _GITHUB_COMMIT_PATTERN.match(title)):
        return f"{m.group('repo')} commit: {m.group('message')} ({m.group('sha')})"
    if (m := _GITHUB_COMMIT_NO_MSG_PATTERN.match(title)):
        return f"{m.group('repo')} commit {m.group('sha')}"

    return title


# ---------------------------------------------------------------------------
# Find subtitle (small caption shown beneath the headline)
# ---------------------------------------------------------------------------

_FINDING_TYPE_LABELS = {
    "release": "Release",
    "rss_item": "Article",
    "subreddit_post": "Forum post",
    "youtube_item": "Video",
    "github_topic_match": "New repo",
    "github_user_repo": "New repo",
    "artifact": "Repository file",
    "new_source_candidate": "Candidate source",
    "diff": "Update",
    "saved_search": "Search hit",
}


def display_finding_caption(finding_type: str, source_display_name: str) -> str:
    """Build the small subtitle line for a finding card.

    Format: "<type label> from <source display name>". The frontend can
    render this directly under the title without composing the string.
    """
    label = _FINDING_TYPE_LABELS.get(finding_type or "", finding_type.replace("_", " ").capitalize() if finding_type else "Finding")
    if source_display_name:
        return f"{label} from {source_display_name}"
    return label


# ---------------------------------------------------------------------------
# Convenience: enrich a serialized find dict in place.
# ---------------------------------------------------------------------------

def enrich_find_display(payload: dict[str, Any]) -> dict[str, Any]:
    """Add display_* fields to a serialized DiscoveryFind dict.

    Idempotent: re-running on an already-enriched payload is a no-op.
    """
    source_display = display_source_name(str(payload.get("source_name", "")))
    payload["display_source_name"] = source_display
    payload["display_title"] = display_finding_title(
        str(payload.get("title", "")),
        finding_type=str(payload.get("finding_type", "")),
        watch_type=str(payload.get("watch_type", "")),
    )
    payload["display_caption"] = display_finding_caption(
        finding_type=str(payload.get("finding_type", "")),
        source_display_name=source_display,
    )
    return payload


def enrich_source_display(payload: dict[str, Any]) -> dict[str, Any]:
    """Add display_name to a serialized DiscoverySource (or rollup) dict."""
    payload["display_name"] = display_source_name(str(payload.get("name", "")))
    # Recursively enrich recent_finds when present (sources-with-activity payload).
    recent = payload.get("recent_finds")
    if isinstance(recent, list):
        for item in recent:
            if isinstance(item, dict):
                item.setdefault("display_title", display_finding_title(
                    str(item.get("title", "")),
                    finding_type=str(item.get("finding_type", "")),
                ))
    return payload
