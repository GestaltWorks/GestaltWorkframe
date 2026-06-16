import logging
import os
import threading
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from mcp.server.fastmcp import FastMCP
from gestaltworkframe.kb.retrieval_format import (
    NO_RELEVANT_INFO_MESSAGE,
    SEARCH_ERROR_MESSAGE,
    format_search_results,
)

logger = logging.getLogger(__name__)

# Setup Chroma DB connection
CHROMA_DB_DIR = Path(os.getenv("CHROMA_DB_DIR", "gestaltworkframe/kb/chroma_db"))
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

def _ranked_documents(query: str, num_results: int) -> list:
    """Shared ranked-retrieval core: similarity search, dedupe, rerank, truncate.

    Returns a list of (document, score) pairs. Used by both kb_search (text) and
    kb_search_cloud_eligible (privacy metadata) so the two never diverge.
    """
    bounded_results = _bounded_num_results(num_results)
    vs = get_vectorstore()
    candidate_count = min(max(bounded_results * 5, 20), 50)
    results = vs.similarity_search_with_score(query, k=candidate_count)
    results = _dedupe_results(results)
    return sorted(
        results,
        key=lambda item: _rerank_score(
            query,
            item[0].metadata.get('source', 'Unknown'),
            item[0].page_content,
            item[1],
        ),
    )[:bounded_results]


def doc_cloud_llm_eligible(metadata: dict) -> bool:
    """Whether a single retrieved document may be sent to a cloud LLM.

    Cloud-eligible unless its source metadata explicitly marks it restricted via
    `cloud_llm_eligible: false`. Public/library and approved-discovery sources
    leave the flag unset and are therefore eligible; a privacy-restricted source
    sets it false at ingest time and is honored here.
    """
    return bool(metadata.get("cloud_llm_eligible", True))


def kb_search_with_eligibility(
    query: str, num_results: int = DEFAULT_NUM_RESULTS
) -> tuple[str, bool]:
    """One ranked search returning (formatted text, cloud-eligibility).

    cloud-eligibility is True only if every retrieved document is cloud-eligible;
    one restricted source downgrades the whole retrieval, so context mixing a
    private local source is not sent to a cloud LLM. Empty results / errors
    default to eligible (no private content surfaced to gate on). A single query
    serves both the text and the privacy decision — the hot path stays one search.
    """
    try:
        ranked = _ranked_documents(query, num_results)
        if not ranked:
            return NO_RELEVANT_INFO_MESSAGE, True
        eligible = all(doc_cloud_llm_eligible(doc.metadata) for doc, _ in ranked)
        return format_search_results(ranked), eligible
    except Exception:
        logger.exception("Knowledge base search failed")
        return SEARCH_ERROR_MESSAGE, True


@mcp.tool(
    name="kb_search",
    description="Search the configured corpus and reference sources. Returns ranked snippets with citations and public links when available."
)
def kb_search(
    query: str,
    num_results: int = DEFAULT_NUM_RESULTS
) -> str:
    # MCP tool surface: text only. Internal callers that also need the privacy
    # decision use kb_search_with_eligibility (same single query).
    return kb_search_with_eligibility(query, num_results)[0]

if __name__ == "__main__":
    mcp.run(transport="stdio")
