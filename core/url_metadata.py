"""URL metadata extractor for the newsletter custom-add flow.

The operator pastes a URL into the admin discovery or admin newsletter
panel. The backend fetches the URL with SSRF-safe constraints, parses
the head for OG / Twitter card / standard meta tags + the <title>, and
returns a preview dict the UI can render and the operator can edit
before save.

Hard limits enforced here:
- URL must be a public https URL (hostname not localhost, .local,
  private IP, etc.) via kb.target_safety.validate_public_https_url
- Hostname DNS resolution checked for global addresses (defense in
  depth against DNS rebinding tricks; matches the existing discovery
  handler stance)
- Fetch timeout: FETCH_TIMEOUT_SECONDS
- Response body cap: MAX_RESPONSE_BYTES (head-only; we only need the
  <head> for meta tags)
- Redirects cap: MAX_REDIRECTS, every hop revalidated for safety
- Content-type must be HTML-ish (text/html, application/xhtml+xml)

Parser uses stdlib html.parser; no external HTML library dependency.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from kb.target_safety import validate_public_https_url

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 8.0
MAX_RESPONSE_BYTES = 1_500_000   # 1.5 MB; head usually fits in first 50KB
MAX_REDIRECTS = 3
ALLOWED_CONTENT_TYPE_PREFIXES = ("text/html", "application/xhtml+xml")


class MetadataExtractError(ValueError):
    """Raised for any input-validation or fetch-side failure the
    operator should see surfaced as a 4xx, not a 500."""


@dataclass(frozen=True)
class ExtractedMetadata:
    """Preview shape returned to the admin UI.

    All fields are operator-editable before save; the extractor is a
    convenience, not an oracle. Empty strings mean 'we did not find
    that field' so the UI can prompt the operator to fill it in.
    """

    url: str  # final URL after redirects, validated through SSRF guard
    title: str
    description: str
    image_url: str
    source_name: str   # hostname or og:site_name when present
    raw_html_length: int  # for diagnostics; not displayed publicly


@dataclass(frozen=True)
class _FetchedHtml:
    url: str
    status_code: int
    content_type: str
    body: bytes
    location: str = ""


def _resolve_hostname_is_global(hostname: str) -> bool:
    """Resolve the hostname and check EVERY answer is a global IP.

    Defense in depth against DNS rebinding / wildcard-internal records.
    A hostname that resolves to a private/loopback IP is rejected here
    even though validate_public_https_url passed it on the string level.

    Returns False on resolution failure (the fetch path will fail anyway,
    but we surface the rejection earlier with a clearer error).
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not addr.is_global:
            return False
    return True


# ---------------------------------------------------------------------------
# HTML head parser. Stdlib HTMLParser so no extra dep.
# ---------------------------------------------------------------------------


class _HeadMetaParser(HTMLParser):
    """Collects <meta> + <title> from the head. Stops when </head> is
    seen so we never walk into a 1MB body for nothing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_chunks: list[str] = []
        self.metas: list[dict[str, str]] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if tag.lower() == "title":
            self.in_title = True
            return
        if tag.lower() == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            self.metas.append(attr_map)
            return
        if tag.lower() == "body":
            # Some sites omit </head>; stop at <body> too.
            self.done = True

    def handle_endtag(self, tag: str) -> None:
        if self.done:
            return
        lowered = tag.lower()
        if lowered == "title":
            self.in_title = False
            return
        if lowered == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self.in_title and not self.done:
            self.title_chunks.append(data)


def _first_meta(metas: list[dict[str, str]], *keys_and_values: tuple[str, str]) -> str:
    """Return the content of the first <meta> matching any (attr, value)
    pair. Used to walk a priority list: og:title -> twitter:title -> name=title."""
    for attr, value in keys_and_values:
        target = value.lower()
        for meta in metas:
            if meta.get(attr, "").lower() == target:
                content = meta.get("content", "").strip()
                if content:
                    return content
    return ""


def _parse_head(html_bytes: bytes, base_url: str) -> ExtractedMetadata:
    """Parse the head of a fetched HTML response and return the preview.

    Decodes UTF-8 strictly (most modern sites); falls back to latin-1 on
    decode failure so we still get something usable for legacy pages."""
    try:
        html_text = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html_text = html_bytes.decode("latin-1", errors="replace")

    parser = _HeadMetaParser()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001
        # Malformed HTML — keep whatever we already collected.
        logger.warning("HTML head parser failed mid-stream for %s", base_url)

    title = _first_meta(
        parser.metas,
        ("property", "og:title"),
        ("name", "twitter:title"),
    ) or "".join(parser.title_chunks).strip()
    title = re.sub(r"\s+", " ", title)[:300]

    description = _first_meta(
        parser.metas,
        ("property", "og:description"),
        ("name", "twitter:description"),
        ("name", "description"),
    )
    description = re.sub(r"\s+", " ", description)[:600]

    image_url = _first_meta(
        parser.metas,
        ("property", "og:image"),
        ("name", "twitter:image"),
        ("name", "twitter:image:src"),
    )
    # If image_url is relative or non-http(s), drop it so the UI never
    # renders a broken/dangerous reference.
    if image_url:
        img_parsed = urlparse(image_url)
        if img_parsed.scheme not in {"http", "https"} or not img_parsed.netloc:
            image_url = ""

    site_name = _first_meta(
        parser.metas,
        ("property", "og:site_name"),
        ("name", "application-name"),
    )
    if not site_name:
        # Fall back to the URL hostname (sans www.).
        host = urlparse(base_url).hostname or ""
        site_name = host[4:] if host.startswith("www.") else host

    return ExtractedMetadata(
        url=base_url,
        title=title,
        description=description,
        image_url=image_url,
        source_name=site_name[:200],
        raw_html_length=len(html_bytes),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _validate_public_destination(raw_url: str, *, source_name: str) -> str:
    """Validate URL shape and DNS before any fetch attempt."""
    try:
        url = validate_public_https_url(raw_url, source_name=source_name)
    except ValueError as exc:
        raise MetadataExtractError(str(exc)) from exc

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    loop = asyncio.get_running_loop()
    is_global = await loop.run_in_executor(None, _resolve_hostname_is_global, hostname)
    if not is_global:
        raise MetadataExtractError(
            f"Host {hostname!r} resolves to a non-public address; not fetched."
        )
    return url


async def _fetch_html_once(client: httpx.AsyncClient, url: str) -> _FetchedHtml:
    async with client.stream("GET", url) as response:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        location = response.headers.get("location", "")
        if response.status_code in {301, 302, 303, 307, 308}:
            return _FetchedHtml(str(response.url), response.status_code, content_type, b"", location)
        if response.status_code >= 400:
            return _FetchedHtml(str(response.url), response.status_code, content_type, b"")

        if content_type and not any(content_type.startswith(p) for p in ALLOWED_CONTENT_TYPE_PREFIXES):
            raise MetadataExtractError(
                f"Unsupported content-type {content_type!r}; expected text/html."
            )

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = 0
            if declared_length > MAX_RESPONSE_BYTES:
                raise MetadataExtractError("Response body exceeded metadata preview size limit")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise MetadataExtractError("Response body exceeded metadata preview size limit")
            chunks.append(chunk)
        return _FetchedHtml(str(response.url), response.status_code, content_type, b"".join(chunks))


async def extract_url_metadata(raw_url: str) -> ExtractedMetadata:
    """Fetch `raw_url` with SSRF guards and return a preview dict.

    Raises MetadataExtractError for any expected failure mode the
    operator should see: bad URL, blocked host, non-HTML response, fetch
    timeout, oversized body. Unexpected failures bubble up as 500.
    """

    url = await _validate_public_destination(raw_url, source_name="custom URL")

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            max_redirects=MAX_REDIRECTS,
            headers={
                "User-Agent": "gestalt-workframe metadata preview",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            redirects = 0
            while True:
                fetched = await _fetch_html_once(client, url)
                if fetched.status_code in {301, 302, 303, 307, 308}:
                    if redirects >= MAX_REDIRECTS:
                        raise MetadataExtractError(f"Too many redirects fetching {raw_url}")
                    if not fetched.location:
                        raise MetadataExtractError("Redirect response missing Location header")
                    next_url = urljoin(url, fetched.location)
                    url = await _validate_public_destination(
                        next_url,
                        source_name="custom URL redirect",
                    )
                    redirects += 1
                    continue
                break
    except httpx.TimeoutException as exc:
        raise MetadataExtractError(f"Timed out fetching {raw_url}") from exc
    except httpx.HTTPError as exc:
        raise MetadataExtractError(f"Could not fetch {raw_url}: {exc}") from exc

    if fetched.status_code >= 400:
        raise MetadataExtractError(f"Fetch returned HTTP {fetched.status_code} for {url}")

    final_url = await _validate_public_destination(fetched.url, source_name="custom URL (final)")
    return _parse_head(fetched.body, final_url)
