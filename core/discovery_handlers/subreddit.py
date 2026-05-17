"""subreddit_watch handler."""

from __future__ import annotations

from typing import Any

import httpx

from core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register

MAX_POSTS = 15
USER_AGENT = "gestalt-workframe-discovery/1.0"


def _candidate(subreddit: str, post: dict[str, Any]) -> FindCandidate:
    data = post.get("data") or {}
    permalink = data.get("permalink") or ""
    url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else data.get("url") or ""
    score = int(data.get("score") or 0)
    return FindCandidate(
        finding_type="post",
        external_id=f"reddit:{subreddit}:{data.get('id') or data.get('name')}",
        title=data.get("title") or f"r/{subreddit} post",
        url=url,
        summary_text=(data.get("selftext") or data.get("url") or "")[:480],
        raw_payload={
            "kind": "subreddit_post",
            "subreddit": subreddit,
            "id": data.get("id"),
            "score": score,
            "created_utc": data.get("created_utc"),
            "author": data.get("author"),
        },
        importance_signal="high" if score >= 50 else "normal",
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    subreddit = source.target.strip().removeprefix("r/").strip("/")
    if not subreddit:
        return PollResult(finds=[], status="error", error="subreddit_watch requires r/name target")
    response = await http.get(
        f"https://www.reddit.com/r/{subreddit}/new.json",
        params={"limit": MAX_POSTS, "raw_json": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
        follow_redirects=True,
    )
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"Reddit HTTP {response.status_code}")
    payload = response.json()
    children = ((payload.get("data") or {}).get("children") or []) if isinstance(payload, dict) else []
    return PollResult(finds=[_candidate(subreddit, post) for post in children[:MAX_POSTS]])


register("subreddit_watch", poll)