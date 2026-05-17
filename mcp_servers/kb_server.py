import logging
import os
import threading
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from mcp.server.fastmcp import FastMCP
from kb.retrieval_format import (
    NO_RELEVANT_INFO_MESSAGE,
    SEARCH_ERROR_MESSAGE,
    format_search_results,
)

logger = logging.getLogger(__name__)

# Setup Chroma DB connection
CHROMA_DB_DIR = Path(os.getenv("CHROMA_DB_DIR", "kb/chroma_db"))
DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10

# Initialize FastMCP
mcp = FastMCP("KB_Server")

# Cache the vector store
_vectorstore = None
_vectorstore_lock = threading.Lock()


def get_vectorstore() -> Any:
    global _vectorstore
    if _vectorstore is None:
        with _vectorstore_lock:
            if _vectorstore is None:
                embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
                _vectorstore = Chroma(
                    persist_directory=str(CHROMA_DB_DIR),
                    embedding_function=embeddings
                )
    return _vectorstore


def vectorstore_document_count() -> int | None:
    vs = get_vectorstore()
    collection = getattr(vs, "_collection", None)
    count = getattr(collection, "count", None)
    if not callable(count):
        return None
    return int(count())


def _bounded_num_results(num_results: int) -> int:
    return min(max(num_results, 1), MAX_NUM_RESULTS)

def _is_overview_query(query: str) -> bool:
    q = query.lower()
    return any(term in q for term in [
        "what is this platform",
        "what is the platform",
        "platform overview",
        "what does this do",
    ])

def _is_workflow_library_query(query: str) -> bool:
    q = query.lower()
    return any(term in q for term in [
        "workflow library",
        "bundle.json",
        ".bundle.json",
        "import bundle",
        "plug in",
        "template",
        "onboarding workflow",
    ])

def _rerank_score(query: str, source: str, content: str, distance: float) -> float:
    score = distance
    source_l = source.lower().replace("\\", "/")
    content_l = content.lower()

    if _is_overview_query(query):
        if "automation platform" in content_l:
            score -= 1.0
        if "building workflows" in content_l or "designing automations" in content_l:
            score -= 0.5
        if "contributors/" in source_l:
            score += 0.5
        if any(term in source_l for term in ["/api", "api/", "schema", "openapi"]):
            score += 0.8

    if _is_workflow_library_query(query):
        if source_l.endswith("index.md"):
            score -= 0.9
        if ".bundle.json" in source_l or source_l.endswith("bundle.json"):
            score -= 0.5
        if any(term in content_l for term in ["drop the `.bundle.json`", "ready to import", "preserved import compatibility"]):
            score -= 0.9
        if any(term in content_l for term in ["original `.bundle.json`", "workflow libraries", "source urls"]):
            score -= 0.5
        if any(term in content_l for term in ["onboarding", "provisioning", "user lifecycle"]):
            score -= 0.4
        if any(term in source_l for term in ["openapi", "schema", "jamf", "/api/"]):
            score += 0.8

    return score

def _dedupe_results(results: list[tuple[Any, float]]) -> list[tuple[Any, float]]:
    seen = set()
    deduped = []
    for doc, score in results:
        key = (doc.metadata.get("source", ""), doc.page_content[:200])
        if key in seen:
            continue
        seen.add(key)
        deduped.append((doc, score))
    return deduped

@mcp.tool(
    name="kb_search",
    description="Search the configured corpus and reference sources. Returns ranked snippets with citations and public links when available."
)
def kb_search(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS
) -> str:
    try:
        bounded_results = _bounded_num_results(num_results)
        vs = get_vectorstore()
        candidate_count = min(max(bounded_results * 5, 20), 50)
        results = vs.similarity_search_with_score(query, k=candidate_count)
        results = _dedupe_results(results)
        results = sorted(
            results,
            key=lambda item: _rerank_score(
                query,
                item[0].metadata.get('source', 'Unknown'),
                item[0].page_content,
                item[1],
            ),
        )[:bounded_results]
        
        if not results:
            return NO_RELEVANT_INFO_MESSAGE

        return format_search_results(results)
    except Exception:
        logger.exception("Knowledge base search failed")
        return SEARCH_ERROR_MESSAGE

if __name__ == "__main__":
    mcp.run(transport="stdio")
