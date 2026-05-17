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

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from gestalt_connector_protocol.models import Document


@dataclass(frozen=True)
class ConnectorConfig:
    connector_id: str
    display_name: str = ""
    auth: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorHealth:
    status: Literal["ok", "auth_error", "rate_limited", "unreachable", "degraded"]
    message: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorCapabilities:
    supports_incremental: bool = False
    supports_webhooks: bool = False
    emits_acl: bool = False
    emits_attachments: bool = False
    supported_resource_types: tuple[str, ...] = ()
    supported_mime_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectorValidationResult:
    connector_id: str
    documents_validated: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebhookRequest:
    connector_id: str
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WebhookResult:
    accepted: bool
    documents: tuple[Document, ...] = ()
    message: str = ""


class Connector(Protocol):
    connector_id: str
    capabilities: ConnectorCapabilities

    @classmethod
    def config_schema(cls) -> dict[str, Any]: ...

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth: ...

    def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]: ...

    async def webhook_handler(self, config: ConnectorConfig, request: WebhookRequest) -> WebhookResult: ...

