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

from collections.abc import AsyncIterator
from typing import Any

from gestalt_connector_protocol import (
    Connector,
    ConnectorCapabilities,
    ConnectorConfig,
    ConnectorHealth,
    Document,
    SourceMetadata,
)


class ReferenceConnector:
    connector_id = "reference"
    capabilities = ConnectorCapabilities(supported_resource_types=("article",))

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {"type": "object", "required": ["connector_id"]}

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        return ConnectorHealth(status="ok", message=config.connector_id)

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        yield Document(
            doc_id="reference-doc",
            source=SourceMetadata(
                connector_id=config.connector_id,
                source_type="article",
                external_id="reference-doc",
            ),
            body_text="Reference document",
        )


def test_reference_connector_satisfies_protocol() -> None:
    connector: Connector = ReferenceConnector()
    assert connector.connector_id == "reference"
    assert connector.config_schema()["type"] == "object"
