"""Discovery target validation helpers.

Operator-added discovery sources are trusted less than server config. These
helpers keep handlers from turning the API process into an internal-network fetch
proxy while preserving public-source discovery.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

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


# ---------------------------------------------------------------------------
# Connect-time SSRF guard
#
# ``validate_public_https_url`` runs at insert and fetch time, but it can only
# reason about literal IPs. A public *hostname* whose DNS record points at an
# internal address (169.254.169.254, 127.0.0.1, 10.0.0.0/8, ...) passes that
# check and the process then fetches internal infrastructure. The guard below
# resolves the host and refuses any request whose destination is non-global.
# Wired in as an httpx transport, it validates every redirect hop too, so a
# public URL that 30x-redirects to an internal address is also refused before
# the socket is opened.
# ---------------------------------------------------------------------------


def _address_is_global(ip_text: str) -> bool:
    try:
        return ipaddress.ip_address(ip_text).is_global
    except ValueError:
        return False


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to its A/AAAA addresses. Replaceable in tests."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


# Module-level indirection so tests can simulate hostile DNS without network.
resolve_host_addresses = _default_resolver


def assert_destination_is_global(host: str, *, source_name: str = "request", field: str = "url host") -> None:
    """Raise ``ValueError`` if ``host`` resolves to a non-global address.

    A literal IP is checked directly; a hostname is resolved (every returned
    address must be global). Resolution failure is fail-closed: an unresolvable
    host is rejected rather than handed to the socket layer.
    """

    cleaned = (host or "").strip().rstrip(".").lower()
    if not cleaned:
        raise ValueError(f"{source_name} {field} is empty")
    if cleaned in BLOCKED_HOSTS or cleaned.endswith(BLOCKED_SUFFIXES):
        raise ValueError(f"{source_name} {field} host is not allowed: {cleaned}")

    try:
        ipaddress.ip_address(cleaned)
        addresses: list[str] = [cleaned]
    except ValueError:
        try:
            addresses = list(resolve_host_addresses(cleaned))
        except OSError as exc:
            raise ValueError(f"{source_name} {field} host could not be resolved: {cleaned}") from exc

    if not addresses:
        raise ValueError(f"{source_name} {field} host did not resolve: {cleaned}")
    for ip_text in addresses:
        if not _address_is_global(ip_text):
            raise ValueError(
                f"{source_name} {field} resolves to a non-global address ({ip_text}); refusing internal fetch"
            )


class SsrfGuardTransport(httpx.AsyncBaseTransport):
    """httpx transport that refuses requests to non-global destinations.

    Validates the host of every request it handles -- including each redirect
    hop, since httpx re-enters the transport per hop -- before delegating to the
    wrapped transport.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert_destination_is_global(request.url.host, source_name="outbound fetch")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_guarded_async_client(**kwargs) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` that refuses internal-network destinations."""
    return httpx.AsyncClient(transport=SsrfGuardTransport(), **kwargs)