"""Initial watchlist seed for the discovery subsystem.

This file ships a tiny sample seed that demonstrates the shape of a
`WatchedSource` and exercises the scheduler/handler pipeline end to end.
Replace these entries with the public sources relevant to your
deployment domain. The seed supports GitHub repos, GitHub topics, GitHub
user/org watches, subreddits, YouTube channels, web-diff URLs, RSS
feeds, and saved-search targets.

Each WatchedSource carries full provenance/license/policy metadata. The
scheduler persists operational state (last_polled_at, etag, last_status)
on the `discovery_sources` table; this seed is the canonical declaration,
regenerable on demand.
"""

from kb.watchlist import WatchedSource, validate_watchlist


# Minimal sample. Replace with sources relevant to your deployment.
_GITHUB_REPOS: tuple[tuple[str, str], ...] = (
    ("sample_github_repo", "octocat/Hello-World"),
)


def _github_watch(name: str, repo: str) -> WatchedSource:
    return WatchedSource(
        name=name,
        watch_type="github_repo_watch",
        target=repo,
        description=f"Watch GitHub repository {repo} for new commits and releases.",
        refresh_cadence="daily",
        canonical_url=f"https://github.com/{repo}",
        provenance=f"Public GitHub repository {repo}; tracked as part of the seed public catalog.",
        license_notes="Per-repo license preserved upstream; do not republish content without verifying the upstream LICENSE.",
        attribution=f"GitHub repository owner of {repo}.",
        trust_tier="public_community_repo",
        display_policy="public_after_source_review",
        retrieval_policy="approved_for_grounded_retrieval_after_review",
        curriculum_policy="not_approved_by_default",
        agent_access_policy="read_only_github_public_api; no write tokens; no org access",
        secret_handling="github_token_is_brokered_server_side; never enters LLM context or logs",
        importance_floor="normal",
    )


def _github_artifact_watch(name: str, repo: str, cadence: str = "weekly") -> WatchedSource:
    return WatchedSource(
        name=f"{name}_artifacts",
        watch_type="github_repo_artifact_scan",
        target=repo,
        description=f"Rescan GitHub repository {repo} for new or updated artifacts worth ingesting.",
        refresh_cadence=cadence,
        canonical_url=f"https://github.com/{repo}",
        provenance=f"Public GitHub repository {repo}; artifact-level rescan for corpus freshness.",
        license_notes="Link and summarize only unless upstream licensing explicitly permits reuse or the deployment owns the target repo.",
        attribution=f"GitHub repository owner of {repo}.",
        trust_tier="public_community_repo",
        display_policy="public_after_source_review",
        retrieval_policy="approved_for_grounded_retrieval_after_review",
        curriculum_policy="not_approved_by_default",
        agent_access_policy="read_only_github_public_api; no write tokens; no org access",
        secret_handling="github_token_is_brokered_server_side; never enters LLM context or logs",
        importance_floor="normal",
    )


def _generic_watch(name: str, watch_type: str, target: str, description: str, cadence: str = "daily") -> WatchedSource:
    return WatchedSource(
        name=name,
        watch_type=watch_type,
        target=target,
        description=description,
        refresh_cadence=cadence,
        canonical_url=_canonical_url(watch_type, target),
        provenance=f"Public {watch_type} target approved for discovery: {target}.",
        license_notes="Link and summarize only unless upstream licensing explicitly permits reuse.",
        attribution="Upstream public source owner.",
        trust_tier="public_discovery_signal",
        display_policy="public_after_source_review",
        retrieval_policy="approved_for_grounded_retrieval_after_review",
        curriculum_policy="not_approved_by_default",
        agent_access_policy="read_only_public_source_only",
        secret_handling="no_credentials_required_or_server_brokered_read_only_key",
        importance_floor="normal",
    )


def _canonical_url(watch_type: str, target: str) -> str:
    if watch_type == "github_repo_artifact_scan":
        return f"https://github.com/{target}"
    if watch_type == "github_topic_watch":
        topic = target.removeprefix("topic:")
        return f"https://github.com/topics/{topic}"
    if watch_type == "github_user_org_watch":
        return f"https://github.com/{target}"
    if watch_type == "subreddit_watch":
        return f"https://www.reddit.com/r/{target.removeprefix('r/')}/"
    if watch_type == "youtube_channel_watch":
        return f"https://www.youtube.com/{target}" if target.startswith("@") else f"https://www.youtube.com/{target}"
    if watch_type == "web_diff":
        return target
    if watch_type == "saved_search":
        return "https://search.brave.com/search?q=" + target.replace(" ", "+")
    return target


def _build_seed() -> tuple[WatchedSource, ...]:
    entries: list[WatchedSource] = [_github_watch(name, repo) for name, repo in _GITHUB_REPOS]
    entries.extend(
        _github_artifact_watch(name, repo, cadence="weekly")
        for name, repo in _GITHUB_REPOS
    )

    entries.append(
        _generic_watch(
            "sample_github_topic",
            "github_topic_watch",
            "automation",
            "Watch public GitHub topic:automation for new automation repositories.",
        )
    )

    entries.append(
        WatchedSource(
            name="sample_rss_feed",
            watch_type="rss_feed",
            target="https://example.com/blog/rss.xml",
            description="Sample RSS feed entry. Replace with a real source per deployment.",
            refresh_cadence="daily",
            canonical_url="https://example.com/blog/",
            provenance="Sample placeholder feed for the discovery pipeline.",
            license_notes="Cite and link rather than mirror unless the source's license explicitly permits reuse.",
            attribution="Upstream public source owner.",
            trust_tier="practitioner_public_blog",
            display_policy="link_and_summary_only_until_author_review",
            retrieval_policy="approved_for_grounded_retrieval_as_reference_metadata",
            curriculum_policy="not_approved_by_default",
            agent_access_policy="read_only_public_feed_only",
            secret_handling="no_credentials_required",
            importance_floor="normal",
        )
    )

    return tuple(entries)


WATCHLIST_SEED: tuple[WatchedSource, ...] = _build_seed()
validate_watchlist(WATCHLIST_SEED)
