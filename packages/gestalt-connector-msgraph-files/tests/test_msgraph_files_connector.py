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

from gestalt_connector_msgraph_files import MSGraphFilesConfig, MSGraphFilesConnector, MSGraphResponse, translate_drive_item, translate_list_item
from gestalt_connector_protocol import ConnectorConfig


class FakeRequester:
    def __init__(self, responses: dict[str, list[MSGraphResponse]]) -> None:
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls: list[str] = []

    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> MSGraphResponse:
        key = url.split("v1.0", 1)[-1]
        self.calls.append(key)
        return self.responses[key].pop(0)


def _config(settings: dict[str, Any] | None = None) -> ConnectorConfig:
    return ConnectorConfig(connector_id="gestalt-connector-msgraph-files", auth={"access_token": "token"}, settings=settings or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(200, "ok"), (401, "auth_error"), (429, "rate_limited"), (500, "unreachable")])
async def test_health_check_states(status_code: int, expected: str) -> None:
    connector = MSGraphFilesConnector(FakeRequester({"/me/drive": [MSGraphResponse(status_code, {"id": "drive"})]}))
    assert (await connector.health_check(_config())).status == expected


@pytest.mark.asyncio
async def test_delta_pagination_captures_delta_link() -> None:
    connector = MSGraphFilesConnector(
        FakeRequester(
            {
                "/drives/drive-1/root/delta": [MSGraphResponse(200, {"value": [{"id": "item-1", "name": "Doc.docx"}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/drives/drive-1/root/delta?token=abc"})]
            }
        )
    )
    records = await connector._drive_items(MSGraphFilesConfig.from_connector_config(_config({"drive_ids": ["drive-1"]})), "drive-1")
    assert records[0]["id"] == "item-1"
    assert connector.delta_links["drive-1"].endswith("token=abc")


@pytest.mark.asyncio
async def test_retry_after_on_throttled_request() -> None:
    connector = MSGraphFilesConnector(
        FakeRequester(
            {
                "/drives/drive-1/root/delta": [MSGraphResponse(429, headers={"Retry-After": "0"}), MSGraphResponse(200, {"value": []})]
            }
        )
    )
    assert await connector._drive_items(MSGraphFilesConfig.from_connector_config(_config({"drive_ids": ["drive-1"]})), "drive-1") == []


@pytest.mark.asyncio
async def test_discover_documents_walks_configured_drive() -> None:
    connector = MSGraphFilesConnector(
        FakeRequester({"/drives/drive-1/root/delta": [MSGraphResponse(200, {"value": [{"id": "item-1", "name": "Doc.docx", "webUrl": "https://sharepoint/doc"}]})]})
    )
    docs = [doc async for doc in connector.discover_documents(_config({"drive_ids": ["drive-1"]}))]
    assert docs[0].source.source_type == "drive_item"
    assert docs[0].source.source_url == "https://sharepoint/doc"


def test_translate_drive_item_preserves_folder_path() -> None:
    document = translate_drive_item("gestalt-connector-msgraph-files", {"id": "drive-1"}, {"id": "item-1", "name": "Doc.docx", "parentReference": {"path": "/drive/root:/KB/Sample"}})
    assert "KB/Sample/Doc.docx" in document.body_text
    assert "KB" in document.tags


def test_translate_list_item_renders_metadata_table() -> None:
    document = translate_list_item("gestalt-connector-msgraph-files", "site-1", {"id": "li-1", "fields": {"Title": "Asset", "Owner": "Ops"}})
    assert "Owner: Ops" in document.body_text
    assert document.source.source_type == "list_item"