"""Discovery target validation helpers.

Operator-added discovery sources are trusted less than server config. These
helpers keep handlers from turning the API process into an internal-network fetch
proxy while preserving public-source discovery.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,80}$")
GITHUB_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
SUBREDDIT_RE = re.compile(r"^(?:r/)?[A-Za-z0-9_]{2,21}$")
YOUTUBE_TARGET_RE = re.compile(r"^(?:channel:|u/|@)?[A-Za-z0-9_.-]{2,120}$")

BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain"})
BLOCKED_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
)


def validate_public_https_url(raw_url: str, *, source_name: str, field: str = "target") -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"{source_name} {field} must use https://")
    if not parsed.hostname:
        raise ValueError(f"{source_name} {field} requires a host")
    if parsed.username or parsed.password:
        raise ValueError(f"{source_name} {field} must not include credentials")

    host = parsed.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError(f"{source_name} {field} host is not allowed: {host}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not address.is_global:
        raise ValueError(f"{source_name} {field} must not target private or local addresses")
    return url


def validate_discovery_target(watch_type: str, target: str, *, source_name: str) -> None:
    """Validate that `target` is shaped correctly for `watch_type`.

    Closed-by-default: an unknown `watch_type` raises. Previously this function
    fell through silently, so a future watch_type added to `ALLOWED_WATCH_TYPES`
    without a matching validator branch would let unconstrained input through
    the insert-time SSRF check.
    """

    value = target.strip()
    if watch_type in {"github_repo_watch", "github_repo_artifact_scan"}:
        if not GITHUB_REPO_RE.fullmatch(value):
            raise ValueError(f"{source_name} target must be a GitHub owner/repo name")
        return
    if watch_type == "github_topic_watch":
        topic = value.removeprefix("topic:")
        if not GITHUB_TOPIC_RE.fullmatch(topic):
            raise ValueError(f"{source_name} target must be a GitHub topic slug")
        return
    if watch_type == "github_user_org_watch":
        if not GITHUB_ACCOUNT_RE.fullmatch(value):
            raise ValueError(f"{source_name} target must be a GitHub user or org name")
        return
    if watch_type in {"rss_feed", "web_diff"}:
        validate_public_https_url(value, source_name=source_name)
        return
    if watch_type == "subreddit_watch":
        if not SUBREDDIT_RE.fullmatch(value):
            raise ValueError(f"{source_name} target must be a subreddit name like r/msp")
        return
    if watch_type == "youtube_channel_watch":
        if value.startswith(("http://", "https://")) or not YOUTUBE_TARGET_RE.fullmatch(value):
            raise ValueError(f"{source_name} target must be a YouTube channel/user identifier, not a URL")
        return
    if watch_type == "saved_search":
        if value.startswith(("http://", "https://")) or len(value) > 300:
            raise ValueError(f"{source_name} target must be a saved search query")
        return
    raise ValueError(f"{source_name} has no target validator for watch_type={watch_type!r}")