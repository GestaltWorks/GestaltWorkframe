# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import csv
import hashlib
import html
import json
import logging
import re
import tarfile
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from gestalt_connector_protocol import (
    BodyStructured,
    ConnectorCapabilities,
    ConnectorConfig,
    Diagnostics,
    ConnectorHealth,
    Document,
    HeadingSection,
    ParagraphSection,
    RedactionPipeline,
    RedactionWhitelist,
    SourceMetadata,
    TableSection,
    Timestamps,
)

DEFAULT_EXTENSIONS = (".txt", ".md", ".html", ".json", ".yaml", ".yml", ".csv", ".docx", ".xlsx", ".pptx", ".pdf")
ARCHIVE_EXTENSIONS = (".zip", ".tar.gz", ".tgz")
DEFAULT_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", "dist", "build", ".next", "out"}
MAX_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1000


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilesystemConnectorConfig:
    root_path: Path
    include_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    exclude_dirs: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_EXCLUDE_DIRS))
    include_archives: bool = False
    redaction_whitelist: RedactionWhitelist = field(default_factory=RedactionWhitelist)

    @classmethod
    def from_connector_config(cls, config: ConnectorConfig) -> "FilesystemConnectorConfig":
        settings = dict(config.settings)
        root = settings.get("root_path") or settings.get("path")
        if not root:
            raise ValueError("settings.root_path is required")
        extensions = tuple(_normalize_extension(item) for item in settings.get("include_extensions", DEFAULT_EXTENSIONS))
        exclude_dirs = frozenset(str(item) for item in settings.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS))
        include_archives = bool(settings.get("include_archives", False))
        whitelist = RedactionWhitelist.from_mapping(settings.get("redaction_whitelist", {}))
        return cls(Path(str(root)), extensions, exclude_dirs, include_archives, whitelist)


class FilesystemConnector:
    connector_id = "gestalt-connector-fs"
    capabilities = ConnectorCapabilities(
        supports_incremental=False,
        supported_resource_types=("file",),
        supported_mime_types=(
            "text/plain",
            "text/markdown",
            "text/html",
            "application/json",
            "text/csv",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    )

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["settings"],
            "properties": {
                "connector_id": {"type": "string", "default": cls.connector_id},
                "settings": {
                    "type": "object",
                    "required": ["root_path"],
                    "properties": {
                        "root_path": {"type": "string"},
                        "include_extensions": {"type": "array", "items": {"type": "string"}},
                        "exclude_dirs": {"type": "array", "items": {"type": "string"}},
                        "include_archives": {"type": "boolean", "default": False},
                        "redaction_whitelist": {"type": "object"},
                    },
                },
            },
        }

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        try:
            parsed = FilesystemConnectorConfig.from_connector_config(config)
        except ValueError as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if not parsed.root_path.exists():
            return ConnectorHealth(status="unreachable", message=f"root_path does not exist: {parsed.root_path}")
        if not parsed.root_path.is_dir():
            return ConnectorHealth(status="unreachable", message=f"root_path is not a directory: {parsed.root_path}")
        return ConnectorHealth(status="ok", message=str(parsed.root_path))

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        parsed = FilesystemConnectorConfig.from_connector_config(config)
        pipeline = RedactionPipeline.default(parsed.redaction_whitelist)
        for path in _walk_files(parsed):
            for document in _documents_for_path(config, parsed, path):
                yield pipeline.apply_to_document(document)


@dataclass(frozen=True)
class ExtractedFile:
    text: str
    structured: BodyStructured
    warnings: tuple[str, ...] = ()


def _walk_files(config: FilesystemConnectorConfig) -> list[Path]:
    files: list[Path] = []
    for path in config.root_path.rglob("*"):
        if _skip_path(path, config):
            continue
        if path.is_file() and (path.suffix.lower() in config.include_extensions or (config.include_archives and _is_archive(path))):
            files.append(path)
    return sorted(files)


def _documents_for_path(config: ConnectorConfig, parsed: FilesystemConnectorConfig, path: Path) -> list[Document]:
    relative = path.relative_to(parsed.root_path).as_posix()
    if parsed.include_archives and _is_archive(path):
        return [_document(config, path, f"{relative}!{member_name}", extracted, member_name) for member_name, extracted in _extract_archive(path, parsed)]
    extracted = _extract(path)
    if not extracted.text.strip():
        return []
    return [_document(config, path, relative, extracted, path.stem)]


def _document(config: ConnectorConfig, source_path: Path, external_id: str, extracted: "ExtractedFile", title: str) -> Document:
    return Document(
        doc_id=_doc_id(config.connector_id, external_id),
        source=SourceMetadata(
            connector_id=config.connector_id,
            connector_name=config.display_name,
            source_system="filesystem",
            source_type="file",
            source_url=source_path.as_uri() if source_path.is_absolute() else "",
            external_id=external_id,
            title=Path(title).stem,
            path=list(Path(external_id).parts),
        ),
        body_text=extracted.text,
        body_structured=extracted.structured,
        diagnostics=Diagnostics(warnings=list(extracted.warnings)),
        tags=["filesystem", Path(external_id).suffix.lower().lstrip("."), *Path(external_id).parts[:-1]],
        timestamps=Timestamps(source_updated_at=_mtime(source_path)),
    )


def _skip_path(path: Path, config: FilesystemConnectorConfig) -> bool:
    parts = path.relative_to(config.root_path).parts
    return any(part.startswith(".") or part in config.exclude_dirs for part in parts)


def _extract(path: Path) -> ExtractedFile:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _extract_csv(path)
    if suffix in {".docx", ".xlsx", ".pptx"}:
        return _extract_ooxml(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix in {".json", ".yaml", ".yml"}:
        return _extract_structured_text(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".html":
        text = _html_to_text(text)
    heading = HeadingSection(text=path.stem, level=1)
    return ExtractedFile(text=f"{path.stem}\n{text}".strip(), structured=BodyStructured(sections=[heading, ParagraphSection(text=text.strip())]))


def _extract_archive(path: Path, config: FilesystemConnectorConfig) -> list[tuple[str, ExtractedFile]]:
    members: list[tuple[str, bytes]] = []
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    logger.warning("Archive member count limit reached for %s", path)
                    break
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    logger.warning("Skipping oversized archive member %s in %s", info.filename, path)
                    continue
                members.append((info.filename, archive.read(info)))
    elif path.name.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    logger.warning("Archive member count limit reached for %s", path)
                    break
                if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    logger.warning("Skipping oversized archive member %s in %s", member.name, path)
                    continue
                extracted = archive.extractfile(member) if member.isfile() else None
                if extracted is not None:
                    members.append((member.name, extracted.read()))
    extracted_members: list[tuple[str, ExtractedFile]] = []
    for name, data in members:
        if Path(name).suffix.lower() not in config.include_extensions:
            continue
        text = data.decode("utf-8", errors="replace")
        if Path(name).suffix.lower() == ".html":
            text = _html_to_text(text)
        extracted_members.append((name, ExtractedFile(text=f"{Path(name).stem}\n{text}".strip(), structured=BodyStructured(sections=[HeadingSection(text=Path(name).stem, level=1), ParagraphSection(text=text.strip())]))))
    return extracted_members


def _extract_csv(path: Path) -> ExtractedFile:
    rows = list(csv.reader(path.read_text(encoding="utf-8", errors="replace").splitlines()))
    headers = rows[0] if rows else []
    body_rows = rows[1:] if rows else []
    text = "\n".join(", ".join(row) for row in rows)
    return ExtractedFile(
        text=f"{path.stem}\n{text}".strip(),
        structured=BodyStructured(sections=[HeadingSection(text=path.stem, level=1), TableSection(headers=headers, rows=body_rows)]),
    )


def _extract_structured_text(path: Path) -> ExtractedFile:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
        text = json.dumps(parsed, indent=2, sort_keys=True, default=str)
    except Exception:
        text = raw
    return ExtractedFile(text=f"{path.stem}\n{text}".strip(), structured=BodyStructured(sections=[HeadingSection(text=path.stem, level=1), ParagraphSection(text=text)]))


def _extract_ooxml(path: Path) -> ExtractedFile:
    names_by_suffix = {
        ".docx": ("word/document.xml",),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"),
        ".pptx": (),
    }
    xml_names = names_by_suffix[path.suffix.lower()]
    with zipfile.ZipFile(path) as archive:
        if path.suffix.lower() == ".pptx":
            xml_names = tuple(sorted(name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")))
        text = "\n".join(_xml_text(archive.read(name)) for name in xml_names if name in archive.namelist())
    return ExtractedFile(text=f"{path.stem}\n{text}".strip(), structured=BodyStructured(sections=[HeadingSection(text=path.stem, level=1), ParagraphSection(text=text.strip())]))


def _extract_pdf(path: Path) -> ExtractedFile:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("PDF extraction skipped because pypdf is unavailable: %s", path)
        text = ""
        warnings = ("PDF extraction skipped because pypdf is unavailable",)
    else:
        try:
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            warnings = ()
        except Exception as exc:
            logger.warning("PDF extraction failed for %s: %s", path, exc)
            text = ""
            warnings = ("PDF extraction failed",)
    return ExtractedFile(text=f"{path.stem}\n{text}".strip(), structured=BodyStructured(sections=[HeadingSection(text=path.stem, level=1), ParagraphSection(text=text.strip())]), warnings=warnings)


def _html_to_text(value: str) -> str:
    no_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_scripts)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _xml_text(value: bytes) -> str:
    root = ElementTree.fromstring(value)
    parts = [item.strip() for item in root.itertext() if item.strip()]
    return " ".join(parts)


def _doc_id(connector_id: str, relative_path: str) -> str:
    return hashlib.sha256(f"{connector_id}:{relative_path}".encode("utf-8")).hexdigest()


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _normalize_extension(value: object) -> str:
    extension = str(value).lower().strip()
    return extension if extension.startswith(".") else f".{extension}"


def _is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_EXTENSIONS)
