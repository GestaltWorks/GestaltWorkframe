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
from collections.abc import AsyncIterator
from typing import Any

import pytest

from gestalt_connector_protocol import ConnectorCapabilities, ConnectorConfig, ConnectorHealth, Document, SourceMetadata
from gestalt_connector_protocol.cli import ConnectorValidationError, main, validate_connector


class GoodConnector:
    connector_id = "good"
    capabilities = ConnectorCapabilities()

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {"type": "object"}

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        return ConnectorHealth(status="ok")

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        yield Document(
            doc_id="doc-1",
            source=SourceMetadata(connector_id=config.connector_id, source_type="fixture", external_id="doc-1"),
            body_text="Valid body",
        )


class BadConnector(GoodConnector):
    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        yield Document(
            doc_id="doc-1",
            source=SourceMetadata(connector_id="wrong", source_type="fixture", external_id="doc-1"),
            body_text="Valid body",
        )


@pytest.mark.asyncio
async def test_validate_connector_accepts_valid_documents(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"connector_id": "good"}), encoding="utf-8")
    monkeypatch.setattr("gestalt_connector_protocol.cli._load_connector", lambda ref: GoodConnector())
    result = await validate_connector("good", config)
    assert result.documents_validated == 1


@pytest.mark.asyncio
async def test_validate_connector_rejects_wrong_connector_id(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"connector_id": "good"}), encoding="utf-8")
    monkeypatch.setattr("gestalt_connector_protocol.cli._load_connector", lambda ref: BadConnector())
    with pytest.raises(ConnectorValidationError):
        await validate_connector("good", config)


def test_connector_test_cli_returns_success(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("connector_id: good\n", encoding="utf-8")
    monkeypatch.setattr("gestalt_connector_protocol.cli._load_connector", lambda ref: GoodConnector())
    assert main(["validate", "good", str(config)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"connector_id": "good", "documents_validated": 1}