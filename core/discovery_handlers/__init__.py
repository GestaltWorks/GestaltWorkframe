"""Discovery handler registry.

Each handler module exports an async `poll(source, http)` callable that returns
a list of `FindCandidate` records. The scheduler dispatches to handlers by
`watch_type`. New source types land as new modules registered here; the
scheduler stays agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, NamedTuple

import httpx


class FindCandidate(NamedTuple):
    """One normalized finding emitted by a handler."""

    finding_type: str  # "release" | "commit_delta" | "post" | "new_repo" | "diff" | "mention"
    external_id: str  # Stable identifier within the source for dedup.
    title: str
    url: str
    summary_text: str
    raw_payload: dict
    importance_signal: str = "normal"  # "low" | "normal" | "high"

    # Optional rollup fields. Handlers that aggregate leaf items into a
    # single category-level signal (currently github_repo_artifact_scan)
    # populate these so the scheduler can store the rolled-up shape.
    # Empty / zero / None leaves the existing per-file behavior intact.
    category: str = ""
    child_count: int = 0
    last_upstream_updated_at: datetime | None = None


@dataclass
class PollResult:
    """Outcome of one poll, including conditional-fetch state for next time."""

    finds: list[FindCandidate]
    etag: str = ""
    last_modified: str = ""
    status: str = "ok"  # "ok" | "not_modified" | "error"
    error: str = ""


PollFn = Callable[["DiscoverySourceLike", httpx.AsyncClient], Awaitable[PollResult]]


# Thin protocol-like alias to avoid leaking SQLModel into handler imports. The
# scheduler passes a dataclass with the fields handlers actually read; tests
# pass equivalent dataclasses without touching the DB.
@dataclass
class DiscoverySourceLike:
    name: str
    watch_type: str
    target: str
    etag: str = ""
    last_modified: str = ""
    auth_token: str = ""


# Registered after handler modules are imported below.
_HANDLERS: dict[str, PollFn] = {}


def register(watch_type: str, fn: PollFn) -> None:
    _HANDLERS[watch_type] = fn


def get_handler(watch_type: str) -> PollFn:
    if watch_type not in _HANDLERS:
        raise KeyError(f"No discovery handler registered for watch_type={watch_type}")
    return _HANDLERS[watch_type]


def registered_watch_types() -> frozenset[str]:
    return frozenset(_HANDLERS.keys())


# Import handlers so they self-register. Imports go at the bottom so the
# protocol types above are available when the modules load.
from core.discovery_handlers import github_repo as _github_repo  # noqa: E402,F401
from core.discovery_handlers import github_repo_artifact as _github_repo_artifact  # noqa: E402,F401
from core.discovery_handlers import github_topic as _github_topic  # noqa: E402,F401
from core.discovery_handlers import github_user_org as _github_user_org  # noqa: E402,F401
from core.discovery_handlers import rss as _rss  # noqa: E402,F401
from core.discovery_handlers import saved_search as _saved_search  # noqa: E402,F401
from core.discovery_handlers import subreddit as _subreddit  # noqa: E402,F401
from core.discovery_handlers import web_diff as _web_diff  # noqa: E402,F401
from core.discovery_handlers import youtube_channel as _youtube_channel  # noqa: E402,F401
