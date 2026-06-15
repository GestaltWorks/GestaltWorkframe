import logging
import os
from pathlib import Path

from langchain_core.documents import Document
from langchain_community.document_loaders import BSHTMLLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from gestaltworkframe.kb.source_links import public_source_url
from gestaltworkframe.kb.source_registry import CorpusSource, source_metadata, validate_source_registry

# Paths — required via env vars; no host-specific defaults
MAIN_CORPUS_PATH = Path(
    os.getenv("MAIN_CORPUS_PATH", os.getenv("CORPUS_REPO_PATH", "data/corpus"))
)
CHEAT_SHEET_PATH = Path(
    os.getenv("CHEAT_SHEET_PATH", "data/api-cheat-sheet.html")
)
CHROMA_DB_DIR = Path(os.getenv("CHROMA_DB_DIR", "kb/chroma_db"))
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200
EXTERNAL_REPORT_URL = os.getenv(
    "EXTERNAL_REPORT_URL",
    "https://example.com/sample-industry-report.pdf",
)
ANTHROPIC_AI_FLUENCY_4D_URL = os.getenv(
    "ANTHROPIC_AI_FLUENCY_4D_URL",
    "https://www-cdn.anthropic.com/b383cf6baddbfc72fdf8b0ed533a518e2872d531.pdf",
)

logger = logging.getLogger(__name__)

TEXT_EXTENSIONS = {
    ".css", ".cs", ".go", ".graphql", ".htm", ".html", ".js", ".json",
    ".jsx", ".md", ".mjs", ".ps1", ".py", ".sh", ".sql", ".toml",
    ".ts", ".tsx", ".txt", ".vb", ".xml", ".yaml", ".yml",
}
TEXT_FILENAMES = {"dockerfile", "license", "makefile", "notice", "readme"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


MAIN_CORPUS_SOURCE = CorpusSource(
    name="main_corpus",
    path=MAIN_CORPUS_PATH,
    source_type="directory",
    description="Sample corpus: docs, workflows, filters, schemas, examples, and community exports.",
    canonical_url="https://example.com/sample-corpus",
    provenance="Configured local corpus directory plus approved public source material",
    license_notes="Preserve upstream licenses and NOTICE entries; verify public display before publishing derived pages.",
    attribution="Recorded upstream source attribution.",
    trust_tier="curated_public_corpus",
    refresh_policy="manual_or_reviewed_discovery",
    display_policy="public_after_source_review",
    retrieval_policy="approved_for_grounded_retrieval",
    curriculum_policy="approved_for_curriculum_generation_with_attribution",
    agent_access_policy="read_only_filesystem; no secret-bearing paths; no write/delete tools",
    secret_handling="agents_do_not_see_secrets; scoped_short_lived_tokens_only_for_future_discovery",
    public_display=True,
    retrieval=True,
    curriculum=True,
)
API_CHEAT_SHEET_SOURCE = CorpusSource(
    name="api_cheat_sheet",
    path=CHEAT_SHEET_PATH,
    source_type="html_file",
    description="Public API cheat-sheet HTML reference offered as a free community resource.",
    canonical_url="local:file://api-cheat-sheet.html",
    provenance="local cheat-sheet file at $CHEAT_SHEET_PATH",
    license_notes="Approved for public display as a free community reference; preserve attribution and remove if source rights change.",
    attribution="API cheat-sheet community reference.",
    trust_tier="public_reference",
    refresh_policy="manual_local_file_update",
    display_policy="public",
    retrieval_policy="approved_for_grounded_retrieval",
    curriculum_policy="not_approved_by_default",
    agent_access_policy="read_only_file; no adjacent directory traversal; no secret-bearing paths",
    secret_handling="agents_do_not_see_secrets; do not expose local file paths beyond citation metadata",
    public_display=True,
    retrieval=True,
    curriculum=False,
)
EXTERNAL_REPORT_SOURCE = CorpusSource(
    name="sample_external_report",
    path=Path("external/sample-external-report.pdf"),
    source_type="external_url_reference",
    description="Sample external report reference; replace with a deployment-specific public report.",
    canonical_url=EXTERNAL_REPORT_URL,
    provenance="Public external report link provided as a sample reference.",
    license_notes="External report reference only; do not republish report contents without confirming rights and attribution requirements.",
    attribution="Upstream public report author / publisher.",
    trust_tier="third_party_public_market_research",
    refresh_policy="manual_review_before_quoting_statistics",
    display_policy="link_and_summary_only_until_rights_review",
    retrieval_policy="approved_for_grounded_retrieval_as_reference_metadata",
    curriculum_policy="not_approved_by_default",
    agent_access_policy="read_only_url_reference; cite canonical_url; do_not_invent_report_statistics",
    secret_handling="public_url_only; do not log campaign-sensitive user identifiers",
    public_display=True,
    retrieval=True,
    curriculum=False,
)
ANTHROPIC_AI_FLUENCY_4D_SOURCE = CorpusSource(
    name="anthropic_academy_ai_fluency_4d_framework",
    path=Path("external/anthropic-ai-fluency-4d-framework.pdf"),
    source_type="external_url_reference",
    description=(
        "Anthropic Academy AI Fluency Framework reference. AI Fluency means interacting with AI systems in ways "
        "that are effective, efficient, ethical, and safe. The 4D competencies are Delegation, Description, "
        "Discernment, and Diligence. Use this source for AI fluency education, Claude readiness, and service "
        "positioning around responsible AI-assisted work."
    ),
    canonical_url=ANTHROPIC_AI_FLUENCY_4D_URL,
    provenance="Anthropic Academy AI Fluency: Framework & Foundations public course and Anthropic-hosted PDF reference.",
    license_notes="External Anthropic Academy reference; cite and link rather than republishing source material.",
    attribution="Anthropic Academy, AI Fluency Framework & Foundations; Prof. Joseph Feller and Prof. Rick Dakan.",
    trust_tier="vendor_public_education_reference",
    refresh_policy="manual_review_when_anthropic_updates_course_material",
    display_policy="link_and_summary_with_attribution",
    retrieval_policy="approved_for_grounded_retrieval_as_reference_metadata",
    curriculum_policy="approved_for_curriculum_reference_with_attribution",
    agent_access_policy="read_only_url_reference; cite canonical_url; summarize 4D competencies; do_not_republish_pdf_text",
    secret_handling="public_url_only; no credentials required",
    public_display=True,
    retrieval=True,
    curriculum=True,
)
CORPUS_SOURCES = (
    MAIN_CORPUS_SOURCE,
    API_CHEAT_SHEET_SOURCE,
    EXTERNAL_REPORT_SOURCE,
    ANTHROPIC_AI_FLUENCY_4D_SOURCE,
)
validate_source_registry(CORPUS_SOURCES)


def _is_indexable(file_path: Path) -> bool:
    if any(part in SKIP_DIRS for part in file_path.parts):
        return False
    if any(part.startswith(".") for part in file_path.parts[:-1]):
        return False
    if file_path.name.startswith(".") and file_path.name not in {".gitignore", ".gitattributes"}:
        return False
    return file_path.suffix.lower() in TEXT_EXTENSIONS or file_path.name.lower() in TEXT_FILENAMES


def _source_metadata(source: CorpusSource, source_path: str, extension: str) -> dict[str, str | bool]:
    metadata = source_metadata(source, source_path, extension)
    metadata["public_url"] = public_source_url(metadata)
    return metadata


def _read_text_file(file_path: Path) -> str:
    data = file_path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.debug("Ignoring undecodable bytes in %s: %s", file_path, exc)
        return data.decode("utf-8", errors="ignore")


def _load_text_document(source: CorpusSource, file_path: Path) -> Document:
    return Document(
        page_content=_read_text_file(file_path),
        metadata=_source_metadata(
            source,
            str(file_path.relative_to(source.path)),
            file_path.suffix.lower() or "none",
        ),
    )


def load_directory_source(source: CorpusSource) -> list[Document]:
    docs = []
    if not source.path.exists():
        logger.warning("%s not found at %s", source.name, source.path)
        return docs

    for file_path in source.path.rglob("*"):
        if not file_path.is_file() or not _is_indexable(file_path):
            continue
        try:
            docs.append(_load_text_document(source, file_path))
        except Exception as e:
            logger.warning("Failed to load %s: %s", file_path, e)
    return docs


def load_html_file_source(source: CorpusSource) -> list[Document]:
    docs = []
    if source.path.exists():
        try:
            loader = BSHTMLLoader(str(source.path), open_encoding="utf-8")
            loaded = loader.load()
            for doc in loaded:
                doc.metadata.update(_source_metadata(source, source.path.name, ".html"))
            docs.extend(loaded)
        except Exception as e:
            logger.warning("Failed to load %s: %s", source.name, e)
    else:
        logger.warning("%s not found at %s", source.name, source.path)
    return docs


def load_external_url_reference_source(source: CorpusSource) -> list[Document]:
    if not source.canonical_url:
        raise ValueError(f"{source.name} external_url_reference requires canonical_url")
    return [
        Document(
            page_content=(
                f"{source.description}\n"
                f"Canonical URL: {source.canonical_url}\n"
                f"Attribution: {source.attribution}\n"
                f"Retrieval policy: {source.retrieval_policy}\n"
                f"Agent access policy: {source.agent_access_policy}"
            ),
            metadata=_source_metadata(source, source.canonical_url, ".url"),
        )
    ]


def load_corpus_sources(sources: tuple[CorpusSource, ...] = CORPUS_SOURCES) -> list[Document]:
    docs = []
    for source in sources:
        before = len(docs)
        logger.info("Loading %s from %s...", source.name, source.path)
        if source.source_type == "directory":
            docs.extend(load_directory_source(source))
        elif source.source_type == "html_file":
            docs.extend(load_html_file_source(source))
        elif source.source_type == "external_url_reference":
            docs.extend(load_external_url_reference_source(source))
        else:
            logger.warning("Unknown corpus source type %s for %s", source.source_type, source.name)
        logger.info("Loaded %s documents from %s.", len(docs) - before, source.name)
    return docs


def load_main_corpus_docs() -> list[Document]:
    return load_directory_source(MAIN_CORPUS_SOURCE)


def load_cheat_sheet() -> list[Document]:
    return load_html_file_source(API_CHEAT_SHEET_SOURCE)


def main():
    all_docs = load_corpus_sources()
    if not all_docs:
        logger.warning("No documents loaded. Exiting.")
        return

    logger.info("Splitting documents...")
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=DEFAULT_CHUNK_SIZE,
        chunk_overlap=DEFAULT_CHUNK_OVERLAP,
    )
    splits = text_splitter.split_documents(all_docs)
    logger.info("Created %s chunks.", len(splits))

    logger.info("Initializing embedding model...")
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

    logger.info("Creating Chroma DB at %s...", CHROMA_DB_DIR)
    Chroma.from_documents(
        documents=splits,
        embedding=embeddings,
        persist_directory=str(CHROMA_DB_DIR)
    )
    logger.info("Ingestion complete.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    main()
