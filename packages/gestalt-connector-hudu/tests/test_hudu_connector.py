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

from gestalt_connector_hudu import HuduConnector, HuduConnectorConfig, HuduResponse, translate_hudu_resource
from gestalt_connector_protocol import ConnectorConfig


class FakeRequester:
    def __init__(self, responses: dict[str, HuduResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HuduResponse:
        key = url.split("example.hudu", 1)[-1] + f"?page={params.get('page')}"
        self.calls.append(key)
        return self.responses[key]


def _config() -> ConnectorConfig:
    return ConnectorConfig(connector_id="gestalt-connector-hudu", auth={"api_key": "hudu-key"}, settings={"base_url": "https://example.hudu", "page_size": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(200, "ok"), (401, "auth_error"), (429, "rate_limited"), (500, "unreachable")])
async def test_health_check_states(status_code: int, expected: str) -> None:
    connector = HuduConnector(FakeRequester({"/api/v1/companies?page=1": HuduResponse(status_code, {"companies": []})}))
    assert (await connector.health_check(_config())).status == expected


@pytest.mark.asyncio
async def test_pagination_uses_total_pages() -> None:
    connector = HuduConnector(
        FakeRequester(
            {
                "/api/v1/companies?page=1": HuduResponse(200, {"companies": [{"id": "1", "name": "Acme"}], "pagination": {"total_pages": 2}}),
                "/api/v1/companies?page=2": HuduResponse(200, {"companies": [{"id": "2", "name": "Beta"}], "pagination": {"total_pages": 2}}),
            }
        )
    )
    records = await connector._paginate(HuduConnectorConfig.from_connector_config(_config()), "/api/v1/companies", {})
    assert [record["name"] for record in records] == ["Acme", "Beta"]


@pytest.mark.asyncio
async def test_pagination_enforces_max_pages() -> None:
    connector = HuduConnector(FakeRequester({"/api/v1/companies?page=1": HuduResponse(200, {"companies": [{"id": "1"}], "pagination": {"total_pages": 2}})}))
    config = ConnectorConfig(connector_id="gestalt-connector-hudu", auth={"api_key": "hudu-key"}, settings={"base_url": "https://example.hudu", "page_size": 1, "max_pages": 1})

    with pytest.raises(RuntimeError, match="max_pages"):
        await connector._paginate(HuduConnectorConfig.from_connector_config(config), "/api/v1/companies", {})


@pytest.mark.asyncio
async def test_pagination_falls_back_to_full_page_when_metadata_missing() -> None:
    connector = HuduConnector(
        FakeRequester(
            {
                "/api/v1/companies?page=1": HuduResponse(200, {"companies": [{"id": "1", "name": "Acme"}]}),
                "/api/v1/companies?page=2": HuduResponse(200, {"companies": []}),
            }
        )
    )

    records = await connector._paginate(HuduConnectorConfig.from_connector_config(_config()), "/api/v1/companies", {})

    assert [record["name"] for record in records] == ["Acme"]


@pytest.mark.asyncio
async def test_discovers_all_company_resource_types() -> None:
    responses = {"/api/v1/companies?page=1": HuduResponse(200, {"companies": [{"id": "1", "name": "Acme"}]}), "/api/v1/companies?page=2": HuduResponse(200, {"companies": []})}
    for resource_type in ("articles", "knowledge_base_articles", "assets", "processes", "relationships"):
        responses[f"/api/v1/companies/1/{resource_type}?page=1"] = HuduResponse(200, {resource_type: [{"id": resource_type, "name": resource_type, "body": "<p>body</p>"}]})
        responses[f"/api/v1/companies/1/{resource_type}?page=2"] = HuduResponse(200, {resource_type: []})
    docs = [doc async for doc in HuduConnector(FakeRequester(responses)).discover_documents(_config())]
    assert {doc.source.source_type for doc in docs} == {"articles", "knowledge_base_articles", "assets", "processes", "relationships"}


def test_article_translation_uses_markdown_or_html_body() -> None:
    document = translate_hudu_resource("gestalt-connector-hudu", {"id": "1", "name": "Acme"}, "articles", {"id": "a1", "title": "Runbook", "body": "<p>Patch server</p>"})
    assert "Patch server" in document.body_text
    assert document.tags[:2] == ["hudu", "articles"]


def test_secure_fields_are_stripped_and_local_only() -> None:
    document = translate_hudu_resource("gestalt-connector-hudu", {"id": "1", "name": "Acme"}, "assets", {"id": "asset1", "name": "Firewall", "secure_password": "NeverStoreThis"})
    assert "NeverStoreThis" not in document.body_text
    assert document.privacy.cloud_llm_eligible is False


def test_process_steps_render_as_ordered_list() -> None:
    document = translate_hudu_resource("gestalt-connector-hudu", {"id": "1", "name": "Acme"}, "processes", {"id": "p1", "name": "Onboard", "steps": [{"name": "Create user"}]})
    assert "Create user" in document.body_text