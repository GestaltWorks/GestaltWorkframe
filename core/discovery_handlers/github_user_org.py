"""github_user_org_watch handler."""

from __future__ import annotations

from typing import Any

import httpx

from core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register
from core.discovery_handlers.github_repo import GITHUB_API_ROOT, _auth_headers

MAX_REPOS = 20


def _candidate(account: str, repo: dict[str, Any]) -> FindCandidate:
    full_name = repo.get("full_name") or f"{account}/{repo.get('name') or 'unknown'}"
    return FindCandidate(
        finding_type="new_repo",
        external_id=f"github-user-org:{account}:{repo.get('id') or full_name}",
        title=f"{account} repository: {repo.get('name') or full_name}",
        url=repo.get("html_url") or f"https://github.com/{full_name}",
        summary_text=(repo.get("description") or "")[:480],
        raw_payload={
            "kind": "github_user_org_repo",
            "account": account,
            "full_name": full_name,
            "updated_at": repo.get("updated_at"),
            "pushed_at": repo.get("pushed_at"),
            "stars": repo.get("stargazers_count"),
        },
        importance_signal="normal",
    )


async def _fetch_repos(http: httpx.AsyncClient, account: str, org: bool, token: str = "") -> httpx.Response:
    kind = "orgs" if org else "users"
    return await http.get(
        f"{GITHUB_API_ROOT}/{kind}/{account}/repos",
        params={"sort": "updated", "per_page": MAX_REPOS},
        headers=_auth_headers(token),
        timeout=15,
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    account = source.target.strip().strip("/")
    if not account:
        return PollResult(finds=[], status="error", error="github_user_org_watch requires an account")
    response = await _fetch_repos(http, account, org=True, token=source.auth_token)
    if response.status_code == 404:
        response = await _fetch_repos(http, account, org=False, token=source.auth_token)
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"GitHub repos HTTP {response.status_code}")
    payload = response.json()
    repos = payload if isinstance(payload, list) else []
    return PollResult(finds=[_candidate(account, repo) for repo in repos[:MAX_REPOS]])


register("github_user_org_watch", poll)