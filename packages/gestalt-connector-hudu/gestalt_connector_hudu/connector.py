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

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from gestalt_connector_protocol import ConnectorCapabilities, ConnectorConfig, ConnectorHealth, Document, RedactionPipeline, RedactionWhitelist

from gestalt_connector_hudu.translators import translate_hudu_resource


RESOURCE_TYPES = ("articles", "knowledge_base_articles", "assets", "processes", "relationships")


@dataclass(frozen=True)
class HuduResponse:
    status_code: int
    json_body: dict[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


class HuduRequester(Protocol):
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HuduResponse: ...


@dataclass(frozen=True)
class HuduConnectorConfig:
    connector_id: str
    base_url: str
    api_key: str
    page_size: int = 100
    max_pages: int = 1000
    company_ids: tuple[str, ...] = ()
    redaction_whitelist: RedactionWhitelist = field(default_factory=RedactionWhitelist)

    @classmethod
    def from_connector_config(cls, config: ConnectorConfig) -> "HuduConnectorConfig":
        settings = dict(config.settings)
        return cls(
            connector_id=config.connector_id,
            base_url=str(settings.get("base_url", "")).rstrip("/"),
            api_key=str(config.auth.get("api_key", "")),
            page_size=int(settings.get("page_size", 100)),
            max_pages=int(settings.get("max_pages", 1000)),
            company_ids=tuple(str(item) for item in settings.get("company_ids", ())),
            redaction_whitelist=RedactionWhitelist.from_mapping(settings.get("redaction_whitelist", {})),
        )


class HuduConnector:
    connector_id = "gestalt-connector-hudu"
    capabilities = ConnectorCapabilities(supported_resource_types=RESOURCE_TYPES)

    def __init__(self, requester: HuduRequester | None = None) -> None:
        self._requester = requester or _UrllibRequester()

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["auth", "settings"],
            "properties": {
                "connector_id": {"type": "string", "default": cls.connector_id},
                "auth": {"type": "object", "required": ["api_key"], "properties": {"api_key": {"type": "string", "writeOnly": True}}},
                "settings": {
                    "type": "object",
                    "required": ["base_url"],
                    "properties": {
                        "base_url": {"type": "string"},
                        "page_size": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
                        "max_pages": {"type": "integer", "default": 1000, "minimum": 1, "maximum": 10000},
                        "company_ids": {"type": "array", "items": {"type": "string"}},
                        "redaction_whitelist": {"type": "object"},
                    },
                },
            },
        }

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        parsed = HuduConnectorConfig.from_connector_config(config)
        if not parsed.base_url:
            return ConnectorHealth(status="unreachable", message="settings.base_url is required")
        if not parsed.api_key:
            return ConnectorHealth(status="auth_error", message="auth.api_key is required")
        try:
            response = await self._request("GET", "/api/v1/companies", parsed, {"page": 1, "page_size": 1})
        except Exception as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if response.status_code == 200:
            return ConnectorHealth(status="ok")
        if response.status_code in {401, 403}:
            return ConnectorHealth(status="auth_error")
        if response.status_code == 429:
            return ConnectorHealth(status="rate_limited")
        return ConnectorHealth(status="unreachable", message=f"unexpected Hudu status {response.status_code}")

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        parsed = HuduConnectorConfig.from_connector_config(config)
        pipeline = RedactionPipeline.default(parsed.redaction_whitelist)
        companies = await self._paginate(parsed, "/api/v1/companies", {})
        for company in companies:
            company_id = str(company.get("id", ""))
            if parsed.company_ids and company_id not in parsed.company_ids:
                continue
            for resource_type in RESOURCE_TYPES:
                for resource in await self._company_resources(parsed, company_id, resource_type):
                    yield pipeline.apply_to_document(translate_hudu_resource(parsed.connector_id, company, resource_type, resource))

    async def _company_resources(self, config: HuduConnectorConfig, company_id: str, resource_type: str) -> list[dict[str, Any]]:
        path = f"/api/v1/companies/{urllib.parse.quote(company_id)}/{resource_type}"
        return await self._paginate(config, path, {})

    async def _paginate(self, config: HuduConnectorConfig, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        page = 1
        records: list[dict[str, Any]] = []
        while True:
            if page > config.max_pages:
                raise RuntimeError(f"Hudu pagination exceeded max_pages={config.max_pages} for {path}")
            response = await self._request("GET", path, config, {"page": page, "page_size": config.page_size, **dict(params)})
            if response.status_code >= 400:
                raise RuntimeError(f"Hudu request failed {response.status_code} for {path}")
            data = _records(response.json_body)
            records.extend(data)
            if not _has_next(response.json_body, page, len(data), config.page_size):
                return records
            page += 1

    async def _request(self, method: str, path: str, config: HuduConnectorConfig, params: Mapping[str, Any]) -> HuduResponse:
        return await self._requester.request(method, _url(config.base_url, path), {"x-api-key": config.api_key, "Accept": "application/json"}, params)


class _UrllibRequester:
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HuduResponse:
        return await asyncio.to_thread(self._request_sync, method, url, headers, params)

    def _request_sync(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> HuduResponse:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url=url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return HuduResponse(response.status, _json_body(response.read().decode("utf-8")), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return HuduResponse(exc.code, _json_body(body), dict(exc.headers.items()))


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _json_body(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _records(body: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "companies", "articles", "knowledge_base_articles", "assets", "processes", "relationships"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _has_next(body: dict[str, Any], page: int, count: int, page_size: int) -> bool:
    pagination = body.get("pagination") or body.get("meta")
    if isinstance(pagination, dict):
        if isinstance(pagination.get("next_page"), int):
            return True
        total_pages = pagination.get("total_pages")
        if isinstance(total_pages, int):
            return page < total_pages
    return count >= page_size
