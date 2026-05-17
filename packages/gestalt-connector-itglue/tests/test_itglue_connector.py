# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from gestalt_connector_itglue import ITGlueConnector, ITGlueConnectorConfig, ITGlueResponse, translate_itglue_asset
from gestalt_connector_protocol import ConnectorConfig


class FakeRequester:
    def __init__(self, responses: dict[str, list[ITGlueResponse]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[tuple[str, Mapping[str, str], Mapping[str, Any]]] = []

    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> ITGlueResponse:
        self.calls.append((url, headers, params))
        path = url.split("api.itglue.com", 1)[-1]
        queue = self.responses[path]
        return queue.pop(0)


def _config() -> ConnectorConfig:
    return ConnectorConfig(connector_id="gestalt-connector-itglue", auth={"api_key": "test-key"}, settings={"backoff_seconds": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(200, "ok"), (401, "auth_error"), (429, "rate_limited"), (500, "unreachable")],
)
async def test_health_check_states(status_code: int, expected: str) -> None:
    connector = ITGlueConnector(FakeRequester({"/me": [ITGlueResponse(status_code)]}))
    assert (await connector.health_check(_config())).status == expected


@pytest.mark.asyncio
async def test_health_check_unreachable_on_request_exception() -> None:
    class BrokenRequester:
        async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> ITGlueResponse:
            raise OSError("offline")

    assert (await ITGlueConnector(BrokenRequester()).health_check(_config())).status == "unreachable"


@pytest.mark.asyncio
async def test_health_check_rejects_dual_auth_configuration() -> None:
    config = ConnectorConfig(connector_id="gestalt-connector-itglue", auth={"api_key": "test-key", "access_token": "token"})

    health = await ITGlueConnector(FakeRequester({})).health_check(config)

    assert health.status == "auth_error"


@pytest.mark.asyncio
async def test_request_prefers_single_auth_header_for_access_token() -> None:
    requester = FakeRequester({"/me": [ITGlueResponse(200)]})
    config = ConnectorConfig(connector_id="gestalt-connector-itglue", auth={"access_token": "token"})

    await ITGlueConnector(requester).health_check(config)

    headers = requester.calls[0][1]
    assert "Authorization" in headers
    assert "x-api-key" not in headers


@pytest.mark.asyncio
async def test_pagination_follows_link_header_and_retries_429() -> None:
    requester = FakeRequester(
        {
            "/organizations": [
                ITGlueResponse(429, headers={"Retry-After": "0"}),
                ITGlueResponse(200, {"data": [{"id": "1", "attributes": {"name": "Acme"}}]}, {"Link": "<https://api.itglue.com/organizations?page=2>; rel=\"next\""}),
            ],
            "/organizations?page=2": [ITGlueResponse(200, {"data": [{"id": "2", "attributes": {"name": "Beta"}}]})],
        }
    )
    connector = ITGlueConnector(requester)
    records = await connector._organizations(ITGlueConnectorConfig.from_connector_config(_config()))
    assert [record["id"] for record in records] == ["1", "2"]
    assert len(requester.calls) == 3


@pytest.mark.asyncio
async def test_discovers_all_asset_types_for_organization() -> None:
    responses = {"/organizations": [ITGlueResponse(200, {"data": [{"id": "1", "attributes": {"name": "Acme"}}]})]}
    for asset_type in ("documents", "configurations", "domains", "locations", "contacts", "passwords", "flexible-assets"):
        responses[f"/organizations/1/relationships/{asset_type}"] = [ITGlueResponse(200, {"data": [{"id": asset_type, "attributes": {"name": asset_type, "body": "<p>body</p>"}}]})]
    docs = [doc async for doc in ITGlueConnector(FakeRequester(responses)).discover_documents(_config())]
    assert {doc.source.source_type for doc in docs} == {"documents", "configurations", "domains", "locations", "contacts", "passwords", "flexible-assets"}
    assert len(docs) == 7


def test_document_translation_preserves_html_text() -> None:
    document = translate_itglue_asset(
        "gestalt-connector-itglue",
        {"id": "org-1", "attributes": {"name": "Acme"}},
        "documents",
        {"id": "doc-1", "attributes": {"name": "Runbook", "body": "<h1>Title</h1><p>Step one</p>"}},
    )
    assert "Step one" in document.body_text
    assert document.tags[:2] == ["itglue", "documents"]


def test_password_translation_strips_password_values() -> None:
    document = translate_itglue_asset(
        "gestalt-connector-itglue",
        {"id": "org-1", "attributes": {"name": "Acme"}},
        "passwords",
        {"id": "pw-1", "attributes": {"name": "Firewall", "username": "admin", "password": "NeverStoreThis"}},
    )
    assert "NeverStoreThis" not in document.body_text
    assert document.privacy.cloud_llm_eligible is False
    assert document.privacy.redactions_applied[0].sensitive_class == "password"


def test_flexible_asset_translation_retains_template_name() -> None:
    document = translate_itglue_asset(
        "gestalt-connector-itglue",
        {"id": "org-1", "attributes": {"name": "Acme"}},
        "flexible-assets",
        {"id": "flex-1", "attributes": {"name": "ISP Circuit", "flexible-asset-type-name": "WAN", "traits": {"carrier": "ExampleNet"}}},
    )
    assert document.source.labels["template"] == "WAN"
    assert "ExampleNet" in document.body_text