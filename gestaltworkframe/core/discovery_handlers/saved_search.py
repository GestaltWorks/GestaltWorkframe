"""saved_search handler using Brave Search when configured."""

from __future__ import annotations

import os
from typing import Any

import httpx

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 10


def _candidate(query: str, item: dict[str, Any]) -> FindCandidate:
    url = item.get("url") or ""
    title = item.get("title") or url or "Saved search result"
    return FindCandidate(
        finding_type="mention",
        external_id=f"brave:{query}:{url or title}",
        title=title,
        url=url,
        summary_text=(item.get("description") or "")[:480],
        raw_payload={"kind": "saved_search_result", "query": query, "url": url, "title": title},
        importance_signal="normal",
    )


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    token = source.auth_token or os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not token:
        return PollResult(finds=[], status="error", error="BRAVE_SEARCH_API_KEY is not configured")
    query = source.target.strip()
    if not query:
        return PollResult(finds=[], status="error", error="saved_search requires a query")
    response = await http.get(
        BRAVE_SEARCH_URL,
        params={"q": query, "count": MAX_RESULTS, "search_lang": "en"},
        headers={"Accept": "application/json", "X-Subscription-Token": token},
        timeout=15,
    )
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"Brave search HTTP {response.status_code}")
    payload = response.json()
    results = ((payload.get("web") or {}).get("results") or []) if isinstance(payload, dict) else []
    return PollResult(finds=[_candidate(query, item) for item in results[:MAX_RESULTS]])


register("saved_search", poll)