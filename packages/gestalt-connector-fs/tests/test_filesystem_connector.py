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

import json
import tarfile
import zipfile

import pytest

import gestalt_connector_fs.connector as fs_connector
from gestalt_connector_fs import FilesystemConnector
from gestalt_connector_protocol import ConnectorConfig
from gestalt_connector_protocol.cli import validate_connector


def _config(root_path) -> ConnectorConfig:
    return ConnectorConfig(connector_id="gestalt-connector-fs", display_name="Fixture FS", settings={"root_path": str(root_path)})


def _zip_xml(path, name: str, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, f"<root><t>{text}</t></root>")


@pytest.mark.asyncio
async def test_health_check_reports_ok_for_existing_directory(tmp_path) -> None:
    health = await FilesystemConnector().health_check(_config(tmp_path))
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_discovers_text_json_yaml_html_and_csv(tmp_path) -> None:
    (tmp_path / "article.md").write_text("# Hello\nWorld", encoding="utf-8")
    (tmp_path / "data.json").write_text(json.dumps({"name": "router"}), encoding="utf-8")
    (tmp_path / "config.yaml").write_text("enabled: true\n", encoding="utf-8")
    (tmp_path / "page.html").write_text("<h1>Title</h1><p>Body</p>", encoding="utf-8")
    (tmp_path / "table.csv").write_text("name,value\nrouter,1\n", encoding="utf-8")
    docs = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))]
    assert len(docs) == 5
    assert {doc.source.external_id for doc in docs} == {"article.md", "data.json", "config.yaml", "page.html", "table.csv"}
    assert all(doc.source.connector_id == "gestalt-connector-fs" for doc in docs)


@pytest.mark.asyncio
async def test_extracts_docx_xlsx_and_pptx_text(tmp_path) -> None:
    _zip_xml(tmp_path / "doc.docx", "word/document.xml", "Doc body")
    _zip_xml(tmp_path / "sheet.xlsx", "xl/sharedStrings.xml", "Sheet body")
    _zip_xml(tmp_path / "deck.pptx", "ppt/slides/slide1.xml", "Deck body")
    docs = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))]
    text = "\n".join(doc.body_text for doc in docs)
    assert "Doc body" in text
    assert "Sheet body" in text
    assert "Deck body" in text


@pytest.mark.asyncio
async def test_archive_extraction_is_off_by_default(tmp_path) -> None:
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        archive.writestr("inside.md", "from archive")
    docs = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))]
    assert docs == []


@pytest.mark.asyncio
async def test_zip_archive_extraction_when_enabled(tmp_path) -> None:
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        archive.writestr("inside.md", "from archive")
    config = ConnectorConfig(connector_id="gestalt-connector-fs", settings={"root_path": str(tmp_path), "include_archives": True})
    docs = [doc async for doc in FilesystemConnector().discover_documents(config)]
    assert docs[0].source.external_id == "archive.zip!inside.md"
    assert "from archive" in docs[0].body_text


@pytest.mark.asyncio
async def test_zip_archive_skips_oversized_members(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fs_connector, "MAX_ARCHIVE_MEMBER_BYTES", 4)
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        archive.writestr("inside.md", "too large")
    config = ConnectorConfig(connector_id="gestalt-connector-fs", settings={"root_path": str(tmp_path), "include_archives": True})

    docs = [doc async for doc in FilesystemConnector().discover_documents(config)]

    assert docs == []


@pytest.mark.asyncio
async def test_zip_archive_limits_member_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fs_connector, "MAX_ARCHIVE_MEMBERS", 1)
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as archive:
        archive.writestr("first.md", "one")
        archive.writestr("second.md", "two")
    config = ConnectorConfig(connector_id="gestalt-connector-fs", settings={"root_path": str(tmp_path), "include_archives": True})

    docs = [doc async for doc in FilesystemConnector().discover_documents(config)]

    assert [doc.source.external_id for doc in docs] == ["archive.zip!first.md"]


@pytest.mark.asyncio
async def test_targz_archive_extraction_when_enabled(tmp_path) -> None:
    inner = tmp_path / "inside.md"
    inner.write_text("from targz", encoding="utf-8")
    with tarfile.open(tmp_path / "archive.tar.gz", "w:gz") as archive:
        archive.add(inner, arcname="inside.md")
    inner.unlink()
    config = ConnectorConfig(connector_id="gestalt-connector-fs", settings={"root_path": str(tmp_path), "include_archives": True})
    docs = [doc async for doc in FilesystemConnector().discover_documents(config)]
    assert docs[0].source.external_id == "archive.tar.gz!inside.md"
    assert "from targz" in docs[0].body_text


@pytest.mark.asyncio
async def test_skips_hidden_and_build_output_dirs(tmp_path) -> None:
    (tmp_path / ".secret.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.md").write_text("dep", encoding="utf-8")
    (tmp_path / "keep.md").write_text("keep", encoding="utf-8")
    docs = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))]
    assert [doc.source.external_id for doc in docs] == ["keep.md"]


@pytest.mark.asyncio
async def test_doc_id_is_stable_for_same_relative_path(tmp_path) -> None:
    (tmp_path / "stable.txt").write_text("one", encoding="utf-8")
    first = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))][0]
    (tmp_path / "stable.txt").write_text("two", encoding="utf-8")
    second = [doc async for doc in FilesystemConnector().discover_documents(_config(tmp_path))][0]
    assert first.doc_id == second.doc_id


@pytest.mark.asyncio
async def test_redacts_sensitive_text_before_emit(tmp_path) -> None:
    (tmp_path / "secret.env").write_text("password=" + "SecretValue9", encoding="utf-8")
    config = ConnectorConfig(
        connector_id="gestalt-connector-fs",
        settings={"root_path": str(tmp_path), "include_extensions": [".env"]},
    )
    document = [doc async for doc in FilesystemConnector().discover_documents(config)][0]
    assert "SecretValue9" not in document.body_text
    assert document.privacy.redactions_applied[0].sensitive_class == "password"
    assert document.privacy.cloud_llm_eligible is False


@pytest.mark.asyncio
async def test_connector_test_validate_passes_end_to_end(tmp_path, monkeypatch) -> None:
    (tmp_path / "sample.md").write_text("hello", encoding="utf-8")
    config_file = tmp_path / "connector.yaml"
    config_file.write_text(
        f"connector_id: gestalt-connector-fs\nsettings:\n  root_path: {tmp_path.as_posix()}\n  include_extensions:\n    - .md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gestalt_connector_protocol.cli._load_connector", lambda ref: FilesystemConnector())
    result = await validate_connector("gestalt-connector-fs", config_file)
    assert result.documents_validated == 1


@pytest.mark.asyncio
async def test_connector_test_validate_accepts_100_file_fixture_share(tmp_path, monkeypatch) -> None:
    for index in range(100):
        (tmp_path / f"doc-{index}.md").write_text(f"document {index}", encoding="utf-8")
    config_file = tmp_path / "connector.yaml"
    config_file.write_text(
        f"connector_id: gestalt-connector-fs\nsettings:\n  root_path: {tmp_path.as_posix()}\n  include_extensions:\n    - .md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gestalt_connector_protocol.cli._load_connector", lambda ref: FilesystemConnector())
    result = await validate_connector("gestalt-connector-fs", config_file)
    assert result.documents_validated == 100