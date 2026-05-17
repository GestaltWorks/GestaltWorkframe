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
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from gestalt_connector_protocol import ConnectorCapabilities, ConnectorConfig, ConnectorHealth, Document, RedactionPipeline, RedactionWhitelist

from gestalt_connector_itglue.translators import translate_itglue_asset


ASSET_TYPES = ("documents", "configurations", "domains", "locations", "contacts", "passwords", "flexible-assets")


@dataclass(frozen=True)
class ITGlueResponse:
    status_code: int
    json_body: dict[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


class ITGlueRequester(Protocol):
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> ITGlueResponse: ...


@dataclass(frozen=True)
class ITGlueConnectorConfig:
    connector_id: str
    base_url: str = "https://api.itglue.com"
    api_key: str = ""
    access_token: str = ""
    page_size: int = 100
    max_retries: int = 3
    backoff_seconds: float = 0.25
    organization_ids: tuple[str, ...] = ()
    redaction_whitelist: RedactionWhitelist = field(default_factory=RedactionWhitelist)

    @classmethod
    def from_connector_config(cls, config: ConnectorConfig) -> "ITGlueConnectorConfig":
        settings = dict(config.settings)
        auth = dict(config.auth)
        return cls(
            connector_id=config.connector_id,
            base_url=str(settings.get("base_url", "https://api.itglue.com")).rstrip("/"),
            api_key=str(auth.get("api_key", "")),
            access_token=str(auth.get("access_token", auth.get("oauth_access_token", ""))),
            page_size=int(settings.get("page_size", 100)),
            max_retries=int(settings.get("max_retries", 3)),
            backoff_seconds=float(settings.get("backoff_seconds", 0.25)),
            organization_ids=tuple(str(item) for item in settings.get("organization_ids", ())),
            redaction_whitelist=RedactionWhitelist.from_mapping(settings.get("redaction_whitelist", {})),
        )

    @property
    def auth_conflict(self) -> bool:
        return bool(self.api_key and self.access_token)


class ITGlueConnector:
    connector_id = "gestalt-connector-itglue"
    capabilities = ConnectorCapabilities(
        supports_incremental=False,
        supported_resource_types=ASSET_TYPES,
        emits_acl=False,
    )

    def __init__(self, requester: ITGlueRequester | None = None) -> None:
        self._requester = requester or _UrllibRequester()

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["auth"],
            "properties": {
                "connector_id": {"type": "string", "default": cls.connector_id},
                "auth": {
                    "type": "object",
                    "properties": {"api_key": {"type": "string", "writeOnly": True}, "access_token": {"type": "string", "writeOnly": True}},
                    "oneOf": [{"required": ["api_key"]}, {"required": ["access_token"]}],
                },
                "settings": {
                    "type": "object",
                    "properties": {
                        "base_url": {"type": "string", "default": "https://api.itglue.com"},
                        "page_size": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
                        "organization_ids": {"type": "array", "items": {"type": "string"}},
                        "redaction_whitelist": {"type": "object"},
                    },
                },
            },
        }

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        parsed = ITGlueConnectorConfig.from_connector_config(config)
        if parsed.auth_conflict:
            return ConnectorHealth(status="auth_error", message="Use either api_key or access_token, not both")
        if not parsed.api_key and not parsed.access_token:
            return ConnectorHealth(status="auth_error", message="api_key or access_token is required")
        try:
            response = await self._request("GET", "/me", parsed, {})
        except Exception as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if response.status_code == 200:
            return ConnectorHealth(status="ok")
        if response.status_code in {401, 403}:
            return ConnectorHealth(status="auth_error", message="ITGlue authentication failed")
        if response.status_code == 429:
            return ConnectorHealth(status="rate_limited", message="ITGlue rate limit reached")
        return ConnectorHealth(status="unreachable", message=f"unexpected ITGlue status {response.status_code}")

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        parsed = ITGlueConnectorConfig.from_connector_config(config)
        if parsed.auth_conflict:
            raise ValueError("Use either api_key or access_token, not both")
        pipeline = RedactionPipeline.default(parsed.redaction_whitelist)
        organizations = await self._organizations(parsed)
        for organization in organizations:
            org_id = str(organization.get("id", ""))
            if not org_id or (parsed.organization_ids and org_id not in parsed.organization_ids):
                continue
            for asset_type in ASSET_TYPES:
                for asset in await self._organization_assets(parsed, org_id, asset_type):
                    document = translate_itglue_asset(parsed.connector_id, organization, asset_type, asset)
                    yield pipeline.apply_to_document(document)

    async def _organizations(self, config: ITGlueConnectorConfig) -> list[dict[str, Any]]:
        return await self._paginate(config, "/organizations", {})

    async def _organization_assets(self, config: ITGlueConnectorConfig, organization_id: str, asset_type: str) -> list[dict[str, Any]]:
        path = f"/organizations/{urllib.parse.quote(organization_id)}/relationships/{asset_type}"
        return await self._paginate(config, path, {})

    async def _paginate(self, config: ITGlueConnectorConfig, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        page_params: dict[str, Any] = {"page[size]": config.page_size, **dict(params)}
        url_path = path
        records: list[dict[str, Any]] = []
        while url_path:
            response = await self._request_with_backoff("GET", url_path, config, page_params)
            if response.status_code >= 400:
                raise RuntimeError(f"ITGlue request failed {response.status_code} for {url_path}")
            data = response.json_body.get("data", [])
            if isinstance(data, list):
                records.extend(item for item in data if isinstance(item, dict))
            next_link = _next_link(response)
            url_path = _path_from_url(next_link, config.base_url) if next_link else ""
            page_params = {}
        return records

    async def _request_with_backoff(self, method: str, path: str, config: ITGlueConnectorConfig, params: Mapping[str, Any]) -> ITGlueResponse:
        attempts = 0
        while True:
            response = await self._request(method, path, config, params)
            if response.status_code != 429 or attempts >= config.max_retries:
                return response
            retry_after = float(response.headers.get("Retry-After", config.backoff_seconds * (2**attempts)))
            await asyncio.sleep(retry_after)
            attempts += 1

    async def _request(self, method: str, path: str, config: ITGlueConnectorConfig, params: Mapping[str, Any]) -> ITGlueResponse:
        headers = {"Accept": "application/vnd.api+json"}
        if config.access_token:
            headers["Authorization"] = f"Bearer {config.access_token}"
        elif config.api_key:
            headers["x-api-key"] = config.api_key
        return await self._requester.request(method, _url(config.base_url, path), headers, params)


class _UrllibRequester:
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> ITGlueResponse:
        return await asyncio.to_thread(self._request_sync, method, url, headers, params)

    def _request_sync(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> ITGlueResponse:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url=url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return ITGlueResponse(response.status, _json_body(body), dict(response.headers.items()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            parsed = _json_body(body)
            return ITGlueResponse(exc.code, parsed, dict(exc.headers.items()))


def _url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base_url}/{path.lstrip('/')}"


def _json_body(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _next_link(response: ITGlueResponse) -> str:
    links = response.json_body.get("links")
    if isinstance(links, dict) and isinstance(links.get("next"), str):
        return str(links["next"])
    link_header = response.headers.get("Link") or response.headers.get("link")
    if not link_header:
        return ""
    for part in link_header.split(","):
        if 'rel="next"' in part or "rel=next" in part:
            return part.split(";", 1)[0].strip().strip("<>")
    return ""


def _path_from_url(value: str, base_url: str) -> str:
    if not value.startswith(base_url):
        return value
    parsed = urllib.parse.urlparse(value)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")
