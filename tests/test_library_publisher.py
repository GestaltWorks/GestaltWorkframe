from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core.db import DiscoveryFind, DiscoverySource
import kb.library_publisher as publisher
from kb.library_publisher import _document_content, _github_app_jwt, _safe_target_path


def test_safe_target_path_rejects_traversal_and_absolute_paths():
    assert _safe_target_path("discovery/approved/item.md") == "discovery/approved/item.md"

    for path in ("../secret.md", "discovery/..hidden/item.md", "/tmp/item.md", "C:/tmp/item.md", "item.txt"):
        with pytest.raises(ValueError):
            _safe_target_path(path)


def test_document_content_neutralizes_frontmatter_delimiters():
    find = DiscoveryFind(
        discovery_source_id="source-id",
        finding_type="post",
        external_id="post:1",
        title="Example",
        url="https://example.test/post",
        summary_text="---\nUseful body\n---",
    )
    source = DiscoverySource(name="source", watch_type="rss_feed", target="https://example.test/feed.xml")

    content = _document_content(find, source, notes="---\napproved")

    assert "\n- - -\nUseful body\n- - -\n" in content
    assert "## Review notes\n\n- - -\napproved" in content


def test_github_app_jwt_uses_configured_app_id(monkeypatch):
    private_key = _set_github_app_env(monkeypatch)

    token = _github_app_jwt()
    header, payload, signature = token.split(".")

    assert json.loads(_decode_segment(header))["alg"] == "RS256"
    assert json.loads(_decode_segment(payload))["iss"] == "12345"
    assert signature
    private_key.public_key().verify(
        _decode_bytes(signature),
        f"{header}.{payload}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_github_app_configured_requires_all_env_vars(monkeypatch):
    for name in (
        "LIBRARY_PUBLISHER_GITHUB_APP_ID",
        "LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID",
        "LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64",
    ):
        monkeypatch.delenv(name, raising=False)

    assert not publisher._github_app_configured()
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_APP_ID", "12345")
    assert not publisher._github_app_configured()
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID", "67890")
    assert not publisher._github_app_configured()
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64", "key")
    assert publisher._github_app_configured()


@pytest.mark.asyncio
async def test_publish_find_to_library_mints_github_app_installation_token(monkeypatch):
    _set_github_app_env(monkeypatch)
    monkeypatch.setenv("LIBRARY_PUBLISHER_REPO", "example/library-repo")

    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.method == "POST" and request.url.path == "/app/installations/67890/access_tokens":
            return httpx.Response(201, json={"token": "installation-token"})
        if request.method == "GET" and request.url.path == "/repos/example/library-repo/contents/discovery/approved/test.md":
            return httpx.Response(404)
        if request.method == "PUT" and request.url.path == "/repos/example/library-repo/contents/discovery/approved/test.md":
            assert request.headers["authorization"] == "Bearer installation-token"
            return httpx.Response(
                200,
                json={
                    "content": {"html_url": "https://github.com/example/library-repo/blob/main/discovery/approved/test.md"},
                    "commit": {"html_url": "https://github.com/example/library-repo/commit/abc"},
                },
            )
        return httpx.Response(500)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        publisher.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    find = DiscoveryFind(
        discovery_source_id="source-id",
        finding_type="post",
        external_id="post:2",
        title="Test",
        url="https://example.test/post",
    )
    source = DiscoverySource(name="source", watch_type="rss_feed", target="https://example.test/feed.xml")

    result = await publisher.publish_find_to_library(find, source, target_path="discovery/approved/test.md")

    assert result.public_url.endswith("/discovery/approved/test.md")
    assert requests[0][0:2] == ("POST", "/app/installations/67890/access_tokens")
    assert requests[1][2] == "Bearer installation-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    (
        (httpx.Response(201, text="<html>not json</html>"), "not valid JSON"),
        (httpx.Response(201, json={"expires_at": "soon"}), "did not include a token"),
    ),
)
async def test_publisher_token_rejects_bad_installation_token_response(monkeypatch, response, message):
    _set_github_app_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/app/installations/67890/access_tokens":
            return response
        return httpx.Response(500)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        publisher.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(publisher.LibraryPublisherError, match=message):
        await publisher._publisher_token()


def _set_github_app_env(monkeypatch) -> rsa.RSAPrivateKey:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID", "67890")
    monkeypatch.setenv("LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64", base64.b64encode(pem).decode("ascii"))
    return private_key


def _decode_segment(segment: str) -> str:
    return _decode_bytes(segment).decode("utf-8")


def _decode_bytes(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(f"{segment}{pad}")
