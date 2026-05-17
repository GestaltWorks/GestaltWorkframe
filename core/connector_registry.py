from __future__ import annotations

from threading import RLock
from dataclasses import dataclass
from typing import Protocol

from gestalt_connector_protocol import ConnectorConfig, WebhookRequest, WebhookResult


class WebhookConnector(Protocol):
    async def webhook_handler(self, config: ConnectorConfig, request: WebhookRequest) -> WebhookResult: ...


@dataclass(frozen=True)
class RegisteredConnector:
    connector_id: str
    connector: WebhookConnector
    config: ConnectorConfig


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, RegisteredConnector] = {}
        self._lock = RLock()

    def register(self, connector_id: str, connector: WebhookConnector, config: ConnectorConfig) -> None:
        if not connector_id.strip():
            raise ValueError("connector_id is required")
        with self._lock:
            self._connectors[connector_id] = RegisteredConnector(connector_id, connector, config)

    def get(self, connector_id: str) -> RegisteredConnector | None:
        with self._lock:
            return self._connectors.get(connector_id)


connector_registry = ConnectorRegistry()
