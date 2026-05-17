# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from xml.etree import ElementTree

from gestalt_connector_protocol import ConnectorCapabilities, ConnectorConfig, ConnectorHealth, Document, RedactionPipeline, RedactionWhitelist

from gestalt_connector_s3.translators import translate_s3_object

DEFAULT_EXTENSIONS = (".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".html", ".xml")
MAX_KEYS = 1000
MAX_OBJECT_BYTES = 25 * 1024 * 1024

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3Response:
    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class S3Requester(Protocol):
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> S3Response: ...


@dataclass(frozen=True)
class S3ConnectorConfig:
    connector_id: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(default="", repr=False)
    region: str = "us-east-1"
    endpoint_url: str = ""
    prefixes: tuple[str, ...] = ("",)
    max_keys: int = MAX_KEYS
    include_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    redaction_whitelist: RedactionWhitelist = field(default_factory=RedactionWhitelist)

    @classmethod
    def from_connector_config(cls, config: ConnectorConfig) -> "S3ConnectorConfig":
        settings = dict(config.settings)
        auth = dict(config.auth)
        return cls(
            connector_id=config.connector_id,
            bucket=str(settings.get("bucket", "")),
            access_key_id=str(auth.get("access_key_id", "")),
            secret_access_key=str(auth.get("secret_access_key", "")),
            session_token=str(auth.get("session_token", "")),
            region=_validated_region(str(settings.get("region", "us-east-1"))),
            endpoint_url=_validated_endpoint_url(str(settings.get("endpoint_url", ""))),
            prefixes=tuple(str(item) for item in settings.get("prefixes", ("",))),
            max_keys=_bounded_max_keys(settings.get("max_keys", MAX_KEYS)),
            include_extensions=tuple(_normalize_extension(item) for item in settings.get("include_extensions", DEFAULT_EXTENSIONS)),
            redaction_whitelist=RedactionWhitelist.from_mapping(settings.get("redaction_whitelist", {})),
        )

    @property
    def base_url(self) -> str:
        if self.endpoint_url:
            return self.endpoint_url
        return f"https://s3.{self.region}.amazonaws.com"


class S3Connector:
    connector_id = "gestalt-connector-s3"
    capabilities = ConnectorCapabilities(
        supports_incremental=False,
        supported_resource_types=("s3_object",),
        supported_mime_types=("text/plain", "text/markdown", "application/json", "text/csv", "text/html", "application/xml", "text/xml"),
    )

    def __init__(self, requester: S3Requester | None = None) -> None:
        self._requester = requester or _UrllibRequester()

    @classmethod
    def config_schema(cls) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["auth", "settings"],
            "properties": {
                "connector_id": {"type": "string", "default": cls.connector_id},
                "auth": {
                    "type": "object",
                    "required": ["access_key_id", "secret_access_key"],
                    "properties": {
                        "access_key_id": {"type": "string", "writeOnly": True},
                        "secret_access_key": {"type": "string", "writeOnly": True},
                        "session_token": {"type": "string", "writeOnly": True},
                    },
                },
                "settings": {
                    "type": "object",
                    "required": ["bucket"],
                    "properties": {
                        "bucket": {"type": "string"},
                        "region": {"type": "string", "default": "us-east-1"},
                        "endpoint_url": {"type": "string"},
                        "prefixes": {"type": "array", "items": {"type": "string"}},
                        "max_keys": {"type": "integer", "default": MAX_KEYS, "minimum": 1, "maximum": MAX_KEYS},
                        "include_extensions": {"type": "array", "items": {"type": "string"}},
                        "redaction_whitelist": {"type": "object"},
                    },
                },
            },
        }

    async def health_check(self, config: ConnectorConfig) -> ConnectorHealth:
        try:
            parsed = S3ConnectorConfig.from_connector_config(config)
        except ValueError as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if not parsed.bucket:
            return ConnectorHealth(status="unreachable", message="settings.bucket is required")
        if not parsed.access_key_id or not parsed.secret_access_key:
            return ConnectorHealth(status="auth_error", message="access_key_id and secret_access_key are required")
        try:
            response = await self._request("HEAD", f"/{parsed.bucket}", parsed, {})
        except Exception as exc:
            return ConnectorHealth(status="unreachable", message=str(exc))
        if response.status_code in {200, 204}:
            return ConnectorHealth(status="ok")
        if response.status_code in {401, 403}:
            return ConnectorHealth(status="auth_error")
        if response.status_code in {429, 503}:
            return ConnectorHealth(status="rate_limited")
        return ConnectorHealth(status="unreachable", message=f"unexpected S3 status {response.status_code}")

    async def discover_documents(self, config: ConnectorConfig) -> AsyncIterator[Document]:
        parsed = S3ConnectorConfig.from_connector_config(config)
        if not parsed.access_key_id or not parsed.secret_access_key:
            raise ValueError("access_key_id and secret_access_key are required")
        pipeline = RedactionPipeline.default(parsed.redaction_whitelist)
        async for metadata in self._objects(parsed):
            key = str(metadata.get("Key") or "")
            if not key or not _included(key, parsed.include_extensions):
                continue
            response = await self._request("GET", f"/{parsed.bucket}/{_quote_key(key)}", parsed, {})
            if 400 <= response.status_code < 500:
                logger.warning("Skipping S3 object %s after status %s", key, response.status_code)
                continue
            if response.status_code >= 500:
                logger.warning("Skipping S3 object %s after status %s", key, response.status_code)
                continue
            if _too_large(response):
                logger.warning("Skipping oversized S3 object %s", key)
                continue
            text = response.body.decode("utf-8", errors="replace")
            if not text.strip():
                continue
            document = translate_s3_object(parsed.connector_id, parsed.bucket, metadata, text, response.headers)
            yield pipeline.apply_to_document(document)

    async def _objects(self, config: S3ConnectorConfig) -> AsyncGenerator[dict[str, Any], None]:
        for prefix in config.prefixes or ("",):
            token = ""
            while True:
                params: dict[str, Any] = {"list-type": "2", "max-keys": config.max_keys}
                if prefix:
                    params["prefix"] = prefix
                if token:
                    params["continuation-token"] = token
                response = await self._request("GET", f"/{config.bucket}", config, params)
                if response.status_code >= 400:
                    logger.warning("Skipping S3 prefix %s after list status %s", prefix, response.status_code)
                    break
                try:
                    page = _parse_list_objects(response.body)
                except ElementTree.ParseError:
                    logger.warning("Skipping S3 prefix %s after malformed list XML", prefix)
                    break
                for item in page.objects:
                    yield item
                if not page.next_token:
                    break
                token = page.next_token

    async def _request(self, method: str, path: str, config: S3ConnectorConfig, params: Mapping[str, Any]) -> S3Response:
        url = _url(config.base_url, path)
        headers = _signed_headers(method, url, params, config)
        return await self._requester.request(method, url, headers, params)


@dataclass(frozen=True)
class _ListPage:
    objects: list[dict[str, Any]]
    next_token: str = ""


class _UrllibRequester:
    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> S3Response:
        return await asyncio.to_thread(self._request_sync, method, url, headers, params)

    def _request_sync(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> S3Response:
        if params:
            url = f"{url}?{_canonical_query(params)}"
        request = urllib.request.Request(url=url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                headers = dict(response.headers.items())
                content_length = headers.get("Content-Length") or headers.get("content-length")
                if method == "GET" and content_length:
                    try:
                        if int(content_length) > MAX_OBJECT_BYTES:
                            return S3Response(response.status, b"", headers)
                    except ValueError:
                        pass
                if method == "GET":
                    # Read one byte past the cap so callers can reject unknown-length oversized bodies without unbounded buffering.
                    return S3Response(response.status, response.read(MAX_OBJECT_BYTES + 1), headers)
                return S3Response(response.status, response.read(), headers)
        except urllib.error.HTTPError as exc:
            return S3Response(exc.code, exc.read(), dict(exc.headers.items()))


def _bounded_max_keys(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return MAX_KEYS
    return max(1, min(parsed, MAX_KEYS))


def _validated_region(value: str) -> str:
    region = value.strip().lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if not region or any(char not in allowed for char in region) or region.startswith("-") or region.endswith("-"):
        raise ValueError("settings.region must contain only lowercase letters, numbers, and hyphens")
    return region


def _validated_endpoint_url(value: str) -> str:
    endpoint = value.rstrip("/")
    if not endpoint:
        return ""
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("settings.endpoint_url must be an https URL with a hostname")
    if parsed.username or parsed.password:
        raise ValueError("settings.endpoint_url must not contain credentials")
    hostname = parsed.hostname.lower().strip("[]")
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise ValueError("settings.endpoint_url must not target local hostnames")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return endpoint
    if any((address.is_private, address.is_loopback, address.is_link_local, address.is_unspecified, address.is_multicast, address.is_reserved)):
        raise ValueError("settings.endpoint_url must not target private or link-local addresses")
    return endpoint


def _too_large(response: S3Response) -> bool:
    content_length = response.headers.get("Content-Length") or response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_OBJECT_BYTES:
                return True
        except ValueError:
            pass
    return len(response.body) > MAX_OBJECT_BYTES


def _parse_list_objects(value: bytes) -> _ListPage:
    if not value:
        return _ListPage([])
    # S3 list XML comes from the configured S3/S3-compatible endpoint; custom endpoints are SSRF-guarded before request.
    root = ElementTree.fromstring(value)
    objects: list[dict[str, Any]] = []
    for contents in root.findall(".//{*}Contents"):
        item = {_strip_ns(child.tag): child.text or "" for child in contents}
        if item.get("Key"):
            objects.append(item)
    token_el = root.find(".//{*}NextContinuationToken")
    token = (token_el.text or "") if token_el is not None else ""
    return _ListPage(objects, token)


def _signed_headers(method: str, url: str, params: Mapping[str, Any], config: S3ConnectorConfig) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    parsed = urllib.parse.urlparse(url)
    payload_hash = hashlib.sha256(b"").hexdigest()
    header_items = {"host": parsed.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    if config.session_token:
        header_items["x-amz-security-token"] = config.session_token
    signed_headers = ";".join(sorted(header_items))
    canonical_headers = "".join(f"{key}:{header_items[key]}\n" for key in sorted(header_items))
    canonical_query = _canonical_query(params)
    scope = f"{date_stamp}/{config.region}/s3/aws4_request"
    canonical_request = "\n".join([method, parsed.path or "/", canonical_query, canonical_headers, signed_headers, payload_hash])
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(_signing_key(config.secret_access_key, date_stamp, config.region), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"Authorization": f"AWS4-HMAC-SHA256 Credential={config.access_key_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"}
    headers.update({key: value for key, value in header_items.items() if key != "host"})
    return headers


def _signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    key_date = hmac.new(("AWS4" + secret).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    key_region = hmac.new(key_date, region.encode("utf-8"), hashlib.sha256).digest()
    key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
    return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()


def _canonical_query(params: Mapping[str, Any]) -> str:
    return "&".join(f"{urllib.parse.quote(str(key), safe='')}={urllib.parse.quote(str(value), safe='')}" for key, value in sorted(params.items()))


def _url(base_url: str, path: str) -> str:
    return f"{base_url}/{path.lstrip('/')}"


def _quote_key(key: str) -> str:
    return urllib.parse.quote(key, safe="/")


def _included(key: str, extensions: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(lowered.endswith(extension) for extension in extensions)


def _normalize_extension(value: object) -> str:
    extension = str(value).lower().strip()
    return extension if extension.startswith(".") else f".{extension}"


def _strip_ns(value: str) -> str:
    return value.rsplit("}", 1)[-1]
