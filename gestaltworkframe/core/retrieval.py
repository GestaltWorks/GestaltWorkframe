import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from gestaltworkframe.mcp_servers.kb_server import kb_search_with_eligibility
from gestaltworkframe.core.discovery_retrieval import approved_discovery_context_result
from gestaltworkframe.core.tool_policy import WORKFLOW_PATTERN_SEARCH

logger = logging.getLogger(__name__)


# A small per-process retrieval cache. Users frequently re-ask similar
# questions in the same session, the kb_search call is the slowest part
# of a turn, and the kb corpus only changes during ingestion (not per
# request). The cache is keyed on the (query, tool_name, limit) tuple
# the public retrieve() method already accepts. Entries expire after a
# bounded TTL so freshly-ingested docs surface without a process restart.
RETRIEVAL_CACHE_MAX_ENTRIES = int(os.getenv("RETRIEVAL_CACHE_MAX_ENTRIES", "128"))
RETRIEVAL_CACHE_TTL_SECONDS = float(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "300"))


@dataclass(frozen=True)
class RetrievalResult:
    tool_name: str
    query: str
    content: str
    source: str = "local"
    cloud_llm_eligible: bool = True

    @property
    def has_context(self) -> bool:
        lowered = self.content.lower()
        return bool(
            self.content.strip()
            and "no relevant information found" not in lowered
            and "error searching knowledge base" not in lowered
        )


class KnowledgeRetriever:
    def __init__(self, fallback_url: str | None = None) -> None:
        self.fallback_url = fallback_url if fallback_url is not None else os.getenv("KB_FALLBACK_SEARCH_URL", "").strip()
        # OrderedDict gives us LRU semantics with move_to_end + popitem(last=False).
        # Cache is keyed on the public retrieve() args; only successful results
        # with usable context are cached.
        self._cache: OrderedDict[tuple[str, str, int], tuple[float, RetrievalResult]] = OrderedDict()

    def _cache_get(self, key: tuple[str, str, int]) -> RetrievalResult | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        cached_at, result = entry
        if RETRIEVAL_CACHE_TTL_SECONDS > 0 and (time.monotonic() - cached_at) > RETRIEVAL_CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return result

    def _cache_put(self, key: tuple[str, str, int], result: RetrievalResult) -> None:
        if RETRIEVAL_CACHE_MAX_ENTRIES <= 0:
            return
        self._cache[key] = (time.monotonic(), result)
        self._cache.move_to_end(key)
        while len(self._cache) > RETRIEVAL_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        """Drop every cached retrieval. Used by tests and by post-ingest hooks
        that want freshly-indexed docs to surface immediately."""
        self._cache.clear()

    async def retrieve(self, query: str, tool_name: str, limit: int = 5) -> RetrievalResult:
        clean_query = query.strip()
        cache_key = (clean_query, tool_name, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        search_query = self._search_query(clean_query, tool_name)
        # One search returns both the text and per-source cloud-eligibility: a
        # restricted local source downgrades the whole retrieval, same as
        # discovery can. Library/public sources stay cloud-eligible by default.
        content, local_cloud_eligible = await asyncio.to_thread(
            kb_search_with_eligibility, search_query, limit
        )
        discovery_context = await approved_discovery_context_result(clean_query, limit=3)
        if discovery_context.content:
            if content.strip() and "no relevant information found" not in content.lower() and "error searching knowledge base" not in content.lower():
                content = f"{content}\n\n{discovery_context.content}"
                source = "local+discovery"
            else:
                content = discovery_context.content
                source = "discovery"
        else:
            source = "local"
        result = RetrievalResult(
            tool_name=tool_name,
            query=clean_query,
            content=content,
            source=source,
            cloud_llm_eligible=local_cloud_eligible and discovery_context.cloud_llm_eligible,
        )
        if result.has_context or not self.fallback_url:
            if result.has_context:
                self._cache_put(cache_key, result)
            return result
        fallback = await self._fallback(clean_query, tool_name, limit)
        final = fallback or result
        if final.has_context:
            self._cache_put(cache_key, final)
        return final

    def _search_query(self, query: str, tool_name: str) -> str:
        if tool_name != WORKFLOW_PATTERN_SEARCH:
            return query
        lowered = query.lower()
        if not any(term in lowered for term in ("bundle", "import", "plug in", "template")):
            return query
        return (
            f"{query} workflow examples import bundle ready to import "
            "workflow libraries INDEX.md README preserved import compatibility source URLs"
        )

    async def _fallback(self, query: str, tool_name: str, limit: int) -> RetrievalResult | None:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    self.fallback_url,
                    params={"q": query, "query": query, "tool": tool_name, "limit": limit},
                )
            if not response.is_success:
                return None
            content = self._fallback_content(response)
            if not content.strip():
                return None
            return RetrievalResult(tool_name=tool_name, query=query, content=content, source="fallback")
        except Exception:
            logger.exception("Knowledge base fallback search failed")
            return None

    def _fallback_content(self, response: httpx.Response) -> str:
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response.text
        data = response.json()
        if isinstance(data, dict):
            if isinstance(data.get("content"), str):
                return data["content"]
            if isinstance(data.get("results"), list):
                return "\n\n".join(str(item) for item in data["results"])
        return response.text