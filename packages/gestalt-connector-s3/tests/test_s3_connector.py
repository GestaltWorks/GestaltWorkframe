# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from gestalt_connector_protocol import ConnectorConfig
from gestalt_connector_s3 import S3Connector, S3ConnectorConfig, S3Response, translate_s3_object
from gestalt_connector_s3.connector import MAX_OBJECT_BYTES

LIST_PAGE = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>docs/runbook.md</Key><LastModified>2026-01-01T00:00:00Z</LastModified><ETag>&quot;abc&quot;</ETag><Size>12</Size><StorageClass>STANDARD</StorageClass></Contents>
  <Contents><Key>images/logo.png</Key><Size>99</Size></Contents>
</ListBucketResult>
"""

PAGED_FIRST = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>a.md</Key><Size>1</Size></Contents>
  <NextContinuationToken>next</NextContinuationToken>
</ListBucketResult>
"""

PAGED_SECOND = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>b.md</Key><Size>1</Size></Contents>
</ListBucketResult>
"""


class FakeRequester:
    def __init__(self, responses: list[S3Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> S3Response:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "params": dict(params)})
        return self.responses.pop(0)


def _config(settings: dict[str, Any] | None = None, auth: dict[str, Any] | None = None) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="gestalt-connector-s3",
        auth={"access_key_id": "key", "secret_access_key": "secret"} if auth is None else auth,
        settings={"bucket": "kb-bucket", "region": "us-west-2", **(settings or {})},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "expected"), [(200, "ok"), (403, "auth_error"), (503, "rate_limited"), (500, "unreachable")])
async def test_health_check_states(status_code: int, expected: str) -> None:
    connector = S3Connector(FakeRequester([S3Response(status_code)]))
    assert (await connector.health_check(_config())).status == expected


@pytest.mark.asyncio
async def test_health_check_requires_bucket_and_auth() -> None:
    connector = S3Connector(FakeRequester([]))
    assert (await connector.health_check(_config(settings={"bucket": ""}))).status == "unreachable"
    assert (await connector.health_check(_config(auth={}))).status == "auth_error"


@pytest.mark.asyncio
async def test_discover_documents_lists_and_fetches_text_objects() -> None:
    requester = FakeRequester([S3Response(200, LIST_PAGE), S3Response(200, b"runbook body", {"Content-Type": "text/markdown"})])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config())]

    assert len(docs) == 1
    assert docs[0].source.source_type == "s3_object"
    assert docs[0].source.external_id == "kb-bucket/docs/runbook.md"
    assert "runbook body" in docs[0].body_text
    assert requester.calls[0]["params"]["list-type"] == "2"
    assert requester.calls[1]["url"].endswith("/kb-bucket/docs/runbook.md")


@pytest.mark.asyncio
async def test_list_objects_follows_continuation_token() -> None:
    requester = FakeRequester([S3Response(200, PAGED_FIRST), S3Response(200, PAGED_SECOND)])
    connector = S3Connector(requester)

    objects = [item async for item in connector._objects(S3ConnectorConfig.from_connector_config(_config()))]

    assert [item["Key"] for item in objects] == ["a.md", "b.md"]
    assert requester.calls[1]["params"]["continuation-token"] == "next"


@pytest.mark.asyncio
async def test_s3_requests_are_signed_without_exposing_secret() -> None:
    requester = FakeRequester([S3Response(200)])
    connector = S3Connector(requester)

    await connector.health_check(_config())

    auth = requester.calls[0]["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=key/")
    assert "secret" not in auth


def test_translate_s3_object_preserves_metadata() -> None:
    document = translate_s3_object("gestalt-connector-s3", "kb-bucket", {"Key": "docs/runbook.md", "ETag": '"abc"', "Size": "12"}, "body", {"Content-Type": "text/markdown"})

    assert document.source.source_url == "s3://kb-bucket/docs/runbook.md"
    assert document.source.labels["etag"] == "abc"
    assert document.source.labels["content_type"] == "text/markdown"
    assert "docs" in document.tags


@pytest.mark.asyncio
async def test_custom_endpoint_must_be_https_and_public() -> None:
    requester = FakeRequester([S3Response(200)])
    connector = S3Connector(requester)

    ok = await connector.health_check(_config(settings={"endpoint_url": "https://s3.example.com"}))

    assert ok.status == "ok"
    assert requester.calls[0]["url"].startswith("https://s3.example.com/")
    assert len(requester.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_url", ["http://s3.example.com", "https://169.254.169.254", "https://[::1]", "https://[fd00::1]", "https://localhost", "https://internal.local"])
async def test_custom_endpoint_rejects_unsafe_targets_without_network(endpoint_url: str) -> None:
    requester = FakeRequester([])
    connector = S3Connector(requester)

    health = await connector.health_check(_config(settings={"endpoint_url": endpoint_url}))

    assert health.status == "unreachable"
    assert requester.calls == []


@pytest.mark.asyncio
async def test_discover_documents_skips_object_4xx_and_continues() -> None:
    list_page = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>docs/missing.md</Key><Size>1</Size></Contents>
  <Contents><Key>docs/present.md</Key><Size>1</Size></Contents>
</ListBucketResult>
"""
    requester = FakeRequester([S3Response(200, list_page), S3Response(404), S3Response(200, b"present")])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config())]

    assert len(docs) == 1
    assert docs[0].source.external_id == "kb-bucket/docs/present.md"


@pytest.mark.asyncio
async def test_discover_documents_skips_oversized_object_before_decode() -> None:
    requester = FakeRequester([S3Response(200, LIST_PAGE), S3Response(200, b"large", {"Content-Length": str(MAX_OBJECT_BYTES + 1)})])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config())]

    assert docs == []


@pytest.mark.asyncio
async def test_empty_continuation_token_does_not_request_another_page() -> None:
    list_page = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>docs/runbook.md</Key><Size>1</Size></Contents>
  <NextContinuationToken></NextContinuationToken>
</ListBucketResult>
"""
    requester = FakeRequester([S3Response(200, list_page), S3Response(200, b"body")])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config())]

    assert len(docs) == 1
    assert [call["method"] for call in requester.calls] == ["GET", "GET"]


def test_invalid_max_keys_falls_back_to_default() -> None:
    config = S3ConnectorConfig.from_connector_config(_config(settings={"max_keys": "bad"}))

    assert config.max_keys == 1000


def test_aws_auth_aliases_are_not_accepted_by_parser() -> None:
    config = S3ConnectorConfig.from_connector_config(_config(auth={"aws_access_key_id": "key", "aws_secret_access_key": "secret"}))

    assert config.access_key_id == ""
    assert config.secret_access_key == ""


def test_translate_s3_object_preserves_mapping_headers() -> None:
    document = translate_s3_object("gestalt-connector-s3", "kb-bucket", {"Key": "docs/runbook.md"}, "body", MappingProxyType({"Content-Type": "text/markdown"}))

    assert document.source.labels["content_type"] == "text/markdown"


@pytest.mark.asyncio
async def test_discover_documents_skips_object_5xx_and_continues() -> None:
    list_page = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>docs/error.md</Key><Size>1</Size></Contents>
  <Contents><Key>docs/present.md</Key><Size>1</Size></Contents>
</ListBucketResult>
"""
    requester = FakeRequester([S3Response(200, list_page), S3Response(503), S3Response(200, b"present")])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config())]

    assert len(docs) == 1
    assert docs[0].source.external_id == "kb-bucket/docs/present.md"


@pytest.mark.asyncio
async def test_discover_documents_requires_auth_before_requesting() -> None:
    requester = FakeRequester([])
    connector = S3Connector(requester)

    with pytest.raises(ValueError, match="access_key_id"):
        [doc async for doc in connector.discover_documents(_config(auth={}))]

    assert requester.calls == []


@pytest.mark.asyncio
async def test_list_failure_skips_failed_prefix() -> None:
    requester = FakeRequester([S3Response(503), S3Response(200, LIST_PAGE), S3Response(200, b"body")])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config(settings={"prefixes": ["bad/", "docs/"]}))]

    assert len(docs) == 1
    assert requester.calls[0]["params"]["prefix"] == "bad/"
    assert requester.calls[1]["params"]["prefix"] == "docs/"


@pytest.mark.asyncio
async def test_session_token_is_signed_and_sent() -> None:
    requester = FakeRequester([S3Response(200)])
    connector = S3Connector(requester)

    await connector.health_check(_config(auth={"access_key_id": "key", "secret_access_key": "secret", "session_token": "session"}))

    headers = requester.calls[0]["headers"]
    assert headers["x-amz-security-token"] == "session"
    assert "x-amz-security-token" in headers["Authorization"]
    assert "session" not in headers["Authorization"]


def test_s3_config_repr_redacts_auth_fields() -> None:
    config = S3ConnectorConfig.from_connector_config(_config(auth={"access_key_id": "AKIA_TEST", "secret_access_key": "SECRET_TEST", "session_token": "SESSION_TEST"}))

    rendered = repr(config)

    assert "AKIA_TEST" not in rendered
    assert "SECRET_TEST" not in rendered
    assert "SESSION_TEST" not in rendered


@pytest.mark.asyncio
async def test_invalid_region_rejected_without_network() -> None:
    requester = FakeRequester([])
    connector = S3Connector(requester)

    health = await connector.health_check(_config(settings={"region": "../../metadata"}))

    assert health.status == "unreachable"
    assert requester.calls == []


@pytest.mark.asyncio
async def test_malformed_list_xml_skips_failed_prefix() -> None:
    requester = FakeRequester([S3Response(200, b"<ListBucketResult>"), S3Response(200, LIST_PAGE), S3Response(200, b"body")])
    connector = S3Connector(requester)

    docs = [doc async for doc in connector.discover_documents(_config(settings={"prefixes": ["bad/", "docs/"]}))]

    assert len(docs) == 1
    assert requester.calls[0]["params"]["prefix"] == "bad/"
    assert requester.calls[1]["params"]["prefix"] == "docs/"
