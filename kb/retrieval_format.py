from typing import Any

from kb.source_links import library_entry_url, public_source_url

NO_RELEVANT_INFO_MESSAGE = "No relevant information found in the knowledge base."
SEARCH_ERROR_MESSAGE = "Error searching knowledge base."


def format_search_result(index: int, doc: Any, score: float) -> str:
    """Format one retrieval hit so the LLM can cite it without hunting.

    Citation discipline (see CITATION_DISCIPLINE in core/personas.py)
    expects the model to name title + author/org + year + URL when library
    has a specific source. That works better when those fields are
    explicit, labeled, and at the top of the chunk - not buried in a
    metadata dict or interleaved with content. The format below gives
    each field its own line so a small model can pattern-match and
    extract them reliably.

    Fields:
    - Title: human-readable headline (falls back to source path)
    - Source URL: the public URL to link from the answer
    - Library: stable deployment-side URL for the library entry
    - Source type / topic / source name / author when present in metadata
    - Body: the content snippet
    """
    metadata = doc.metadata or {}
    source = metadata.get("source", "Unknown")
    doc_type = metadata.get("type", "Unknown")
    link = public_source_url(metadata)
    library = library_entry_url(metadata)
    title = (
        metadata.get("title")
        or metadata.get("display_title")
        or metadata.get("source_name")
        or source
    )
    author = metadata.get("author") or metadata.get("attribution") or ""
    year = metadata.get("year") or metadata.get("published_year") or ""
    topic = metadata.get("topic") or metadata.get("review_topic") or ""
    source_name = metadata.get("source_name") or ""

    lines = [f"[Result {index}] (relevance: {1 - min(score, 1.0):.2f})"]
    lines.append(f"Title: {title}")
    if author:
        lines.append(f"Author: {author}")
    if year:
        lines.append(f"Year: {year}")
    if source_name and source_name != title:
        lines.append(f"From: {source_name}")
    if topic:
        lines.append(f"Topic: {topic}")
    lines.append(f"Source type: {doc_type}")
    if link:
        lines.append(f"Source URL: {link}")
    if library and library != link:
        lines.append(f"Library entry: {library}")
    lines.append(f"Internal source path: {source}")
    lines.append("Body:")
    lines.append(str(doc.page_content).strip())
    lines.append("-" * 40)
    return "\n".join(lines)


def format_search_results(results: list[tuple[Any, float]]) -> str:
    if not results:
        return NO_RELEVANT_INFO_MESSAGE
    return "\n\n".join(
        format_search_result(index, doc, score)
        for index, (doc, score) in enumerate(results, start=1)
    )
