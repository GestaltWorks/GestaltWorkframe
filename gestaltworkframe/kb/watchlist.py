"""Watched source definitions for the discovery subsystem.

`CorpusSource` in `kb/source_registry.py` describes corpora already ingested into
the local KB. `WatchedSource` here describes operational watchers that poll
external endpoints (GitHub repos, RSS feeds, etc.) for change signals. Findings
flow into the `discovery_finds` review queue. Approved findings can later be
promoted into `CorpusSource` entries with full provenance.

M2/M3 support read-only public source polling. Watch rows never hold secrets.
"""

from dataclasses import dataclass
from typing import Iterable

from gestaltworkframe.kb.target_safety import validate_discovery_target

ALLOWED_WATCH_TYPES = frozenset(
    {
        "github_repo_watch",
        "github_repo_artifact_scan",
        "github_topic_watch",
        "github_user_org_watch",
        "rss_feed",
        "subreddit_watch",
        "youtube_channel_watch",
        "web_diff",
        "saved_search",
    }
)

# Cadence buckets are mapped to seconds at the scheduler. Per-source overrides
# remain possible via the `refresh_interval_seconds` field on the persisted
# discovery_sources row; the dataclass keeps the human-readable bucket so seed
# data stays scannable.
ALLOWED_REFRESH_CADENCES = frozenset(
    {
        "hourly",
        "every_6h",
        "daily",
        "weekly",
    }
)

CADENCE_SECONDS = {
    "hourly": 3600,
    "every_6h": 21600,
    "daily": 86400,
    "weekly": 604800,
}


@dataclass(frozen=True)
class WatchedSource:
    """Static definition of one operational watcher.

    A WatchedSource is a plan to poll a single endpoint. Persistent state
    (last_polled_at, etag, last_status) lives on the `discovery_sources` table.
    Seed data is the canonical declaration of every watcher this deployment runs.

    Policy fields mirror `CorpusSource` so that approved findings can be
    promoted into corpus entries without re-deriving licensing and attribution.
    """

    name: str
    watch_type: str
    target: str  # GitHub `owner/repo`, full RSS URL, etc.
    description: str
    refresh_cadence: str
    canonical_url: str
    provenance: str
    license_notes: str
    attribution: str
    trust_tier: str
    display_policy: str
    retrieval_policy: str
    curriculum_policy: str
    agent_access_policy: str
    secret_handling: str
    importance_floor: str  # "low" | "normal" | "high" — used by future digest ordering
    active: bool = True


def _required(value: str, field: str, source_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{source_name} requires {field}")
    return normalized


def validate_watchlist(sources: Iterable[WatchedSource]) -> None:
    """Raise ValueError if any seed entry is malformed or non-unique."""

    names: set[str] = set()
    errors: list[str] = []
    for source in sources:
        try:
            name = _required(source.name, "name", "watched source")
            if name in names:
                errors.append(f"duplicate watched source name: {name}")
            names.add(name)

            if source.watch_type not in ALLOWED_WATCH_TYPES:
                errors.append(
                    f"{name} has unsupported watch_type: {source.watch_type}"
                )
            if source.refresh_cadence not in ALLOWED_REFRESH_CADENCES:
                errors.append(
                    f"{name} has unsupported refresh_cadence: {source.refresh_cadence}"
                )
            if source.importance_floor not in {"low", "normal", "high"}:
                errors.append(
                    f"{name} has unsupported importance_floor: {source.importance_floor}"
                )

            for field_name, value in {
                "target": source.target,
                "description": source.description,
                "canonical_url": source.canonical_url,
                "provenance": source.provenance,
                "license_notes": source.license_notes,
                "attribution": source.attribution,
                "trust_tier": source.trust_tier,
                "display_policy": source.display_policy,
                "retrieval_policy": source.retrieval_policy,
                "agent_access_policy": source.agent_access_policy,
                "secret_handling": source.secret_handling,
            }.items():
                _required(value, field_name, name)
            validate_discovery_target(source.watch_type, source.target, source_name=name)
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise ValueError("Invalid watchlist: " + "; ".join(errors))


def refresh_seconds(source: WatchedSource) -> int:
    """Convert a WatchedSource cadence bucket to seconds."""

    return CADENCE_SECONDS[source.refresh_cadence]
