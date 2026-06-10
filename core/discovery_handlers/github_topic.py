"""github_topic_watch handler."""

from __future__ import annotations

from typing import Any

import httpx

from core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register
from core.discovery_handlers.github_repo import GITHUB_API_ROOT, _auth_headers

MAX_REPOS = 10


def _repo_candidate(topic: str, repo: dict[str, Any]) -> FindCandidate:
    full_name = repo.get("full_name") or repo.get("name") or "unknown"
    updated_at = repo.get("updated_at") or ""
    return FindCandidate(
        finding_type="new_repo",
        external_id=f"github-topic:{topic}:{repo.get('id') or full_name}",
        title=f"{full_name} matches topic:{topic}",
        url=repo.get("html_url") or f"https://github.com/{full_name}",
        summary_text=(repo.get("description") or "")[:480],
        raw_payload={
            "kind": "github_topic_repo",
            "topic": topic,
            "full_name": full_name,
            "updated_at": updated_at,
            "stars": repo.get("stargazers_count"),
        },
        importance_signal="normal" if (repo.get("stargazers_count") or 0) < 100 else "high",
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    topic = source.target.strip().removeprefix("topic:").strip()
    if not topic:
        return PollResult(finds=[], status="error", error="github_topic_watch requires a topic target")
    response = await http.get(
        f"{GITHUB_API_ROOT}/search/repositories",
        params={"q": f"topic:{topic}", "sort": "updated", "order": "desc", "per_page": MAX_REPOS},
        headers=_auth_headers(source.auth_token),
        timeout=15,
    )
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"GitHub search HTTP {response.status_code}")
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else []
    return PollResult(finds=[_repo_candidate(topic, repo) for repo in items[:MAX_REPOS]])


register("github_topic_watch", poll)