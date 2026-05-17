# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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

from gestalt_connector_msgraph_files.translators import translate_drive_item, translate_list_item


@dataclass(frozen=True)
class MSGraphResponse:
    status_code: int
    json_body: dict[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


class MSGraphRequester(Protocol):
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> MSGraphResponse: ...


@dataclass(frozen=True)
class MSGraphFilesConfig:
    connector_id: str
    access_token: str
    base_url: str = "https://graph.microsoft.com/v1.0"
    site_ids: tuple[str, ...] = ()
    drive_ids: tuple[str, ...] = ()
    delta_links: dict[str, str] = field(default_factory=dict)
    redaction_whitelist: RedactionWhitelist = field(default_factory=RedactionWhitelist)

    @classmethod
    def from_connector_config(cls, config: ConnectorConfig) -> "MSGraphFilesConfig":
        settings = dict(config.settings)
        return cls(
            connector_id=config.connector_id,
            access_token=str(config.auth.get("access_token", "")),
            base_url=str(settings.get("base_url", "https://graph.microsoft.com/v1.0")).rstrip("/"),
            site_ids=tuple(str(item) for item in settings.get("site_ids", ())),
            drive_ids=tuple(str(item) for item in settings.get("drive_ids", ())),
            delta_links={str(key): str(value) for key, value in dict(settings.get("delta_links", {})).items()},
            redaction_whitelist=RedactionWhitelist.from_mapping(settings.get("redaction_whitelist", {})),
        )


class MSGraphFilesConnector:
    connector_id = "gestalt-connector-msgraph-files"
    capabilities = ConnectorCapabilities(supports_incremental=True, supported_resource_types=("drive_item", "list_item"))

    def __init__(self, requester: MSGraphRequester | None = None) -> None:
        self._requester = requester or _UrllibRequester()
        self.delta_links: dict[str, str] = {}

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["auth"],
            "properties": {
                "connector_id": {"type": "string", "default": cls.connector_id},
                "auth": {"type": "object", "required": ["access_token"], "properties": {"access_token": {"type": "string", "writeOnly": True}}},
                "settings": {
                    "type": "object",
                    "properties": {
                        "base_url": {"type": "string", "default": "https://graph.microsoft.com/v1.0"},
                        "site_ids": {"type": "array", "items": {"type": "string"}},
                        "drive_ids": {"type": "array", "items": {"type": "string"}},
                        "delta_links": {"type": "object"},
                    },
                },
            },
        }

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        parsed = MSGraphFilesConfig.from_connector_config(config)
        if not parsed.access_token:
            return ConnectorHealth(status="auth_error", message="auth.access_token is required")
        try:
            response = await self._request("GET", "/me/drive", parsed, {})
        except Exception as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if response.status_code == 200:
            return ConnectorHealth(status="ok")
        if response.status_code in {401, 403}:
            return ConnectorHealth(status="auth_error")
        if response.status_code == 429:
            return ConnectorHealth(status="rate_limited")
        return ConnectorHealth(status="unreachable", message=f"unexpected Graph status {response.status_code}")

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        parsed = MSGraphFilesConfig.from_connector_config(config)
        pipeline = RedactionPipeline.default(parsed.redaction_whitelist)
        for drive in await self._drives(parsed):
            drive_id = str(drive.get("id", ""))
            if parsed.drive_ids and drive_id not in parsed.drive_ids:
                continue
            for item in await self._drive_items(parsed, drive_id):
                yield pipeline.apply_to_document(translate_drive_item(parsed.connector_id, drive, item))
        for site_id in parsed.site_ids:
            for item in await self._site_list_items(parsed, site_id):
                yield pipeline.apply_to_document(translate_list_item(parsed.connector_id, site_id, item))

    async def _drives(self, config: MSGraphFilesConfig) -> list[dict[str, Any]]:
        if config.drive_ids:
            return [{"id": drive_id, "name": drive_id} for drive_id in config.drive_ids]
        return await self._paginate(config, "/me/drives", {})

    async def _drive_items(self, config: MSGraphFilesConfig, drive_id: str) -> list[dict[str, Any]]:
        delta = config.delta_links.get(drive_id)
        path = delta or f"/drives/{urllib.parse.quote(drive_id)}/root/delta"
        return await self._paginate(config, path, {}, delta_key=drive_id)

    async def _site_list_items(self, config: MSGraphFilesConfig, site_id: str) -> list[dict[str, Any]]:
        return await self._paginate(config, f"/sites/{urllib.parse.quote(site_id)}/lists", {"expand": "items"})

    async def _paginate(self, config: MSGraphFilesConfig, path: str, params: Mapping[str, Any], delta_key: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        next_path = path
        next_params = dict(params)
        while next_path:
            response = await self._request_with_throttle("GET", next_path, config, next_params)
            if response.status_code >= 400:
                raise RuntimeError(f"Graph request failed {response.status_code} for {next_path}")
            value = response.json_body.get("value", [])
            if isinstance(value, list):
                records.extend(item for item in value if isinstance(item, dict))
            if delta_key and isinstance(response.json_body.get("@odata.deltaLink"), str):
                self.delta_links[delta_key] = str(response.json_body["@odata.deltaLink"])
            next_link = response.json_body.get("@odata.nextLink")
            next_path = _path_from_url(str(next_link), config.base_url) if next_link else ""
            next_params = {}
        return records

    async def _request_with_throttle(self, method: str, path: str, config: MSGraphFilesConfig, params: Mapping[str, Any]) -> MSGraphResponse:
        response = await self._request(method, path, config, params)
        if response.status_code == 429:
            await asyncio.sleep(float(response.headers.get("Retry-After", "1")))
            response = await self._request(method, path, config, params)
        return response

    async def _request(self, method: str, path: str, config: MSGraphFilesConfig, params: Mapping[str, Any]) -> MSGraphResponse:
        return await self._requester.request(method, _url(config.base_url, path), {"Authorization": f"Bearer {config.access_token}", "Accept": "application/json"}, params)


class _UrllibRequester:
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> MSGraphResponse:
        return await asyncio.to_thread(self._request_sync, method, url, headers, params)

    def _request_sync(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> MSGraphResponse:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url=url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return MSGraphResponse(response.status, json.loads(response.read().decode("utf-8") or "{}"), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return MSGraphResponse(exc.code, json.loads(body or "{}") if body else {}, dict(exc.headers.items()))


def _url(base_url: str, path: str) -> str:
    if path.startswith("https://"):
        return path
    return f"{base_url}/{path.lstrip('/')}"


def _path_from_url(value: str, base_url: str) -> str:
    if not value.startswith(base_url):
        return value
    parsed = urllib.parse.urlparse(value)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")
