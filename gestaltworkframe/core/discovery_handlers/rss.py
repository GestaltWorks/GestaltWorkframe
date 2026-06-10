"""rss_feed handler.

Polls an RSS 2.0 or Atom feed. Uses stdlib XML parsing rather than feedparser
to keep the dependency surface small for M1. Recognized feed shapes:

  * RSS 2.0: `<rss><channel><item>...</item></channel></rss>`
  * Atom 1.0: `<feed xmlns="http://www.w3.org/2005/Atom"><entry>...</entry></feed>`

Conditional fetch uses ETag / If-Modified-Since.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from gestaltworkframe.core.discovery_handlers import (
    DiscoverySourceLike,
    FindCandidate,
    PollResult,
    register,
)
from kb.target_safety import validate_public_https_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15
USER_AGENT = "gestalt-workframe-discovery"
MAX_SUMMARY_CHARS = 480
MAX_ITEMS_PER_POLL = 25

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _conditional_headers(source: DiscoverySourceLike) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "User-Agent": USER_AGENT,
    }
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified
    return headers


def _clean_summary(raw: str) -> str:
    if not raw:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", raw)
    normalized = _WHITESPACE_RE.sub(" ", stripped).strip()
    return normalized[:MAX_SUMMARY_CHARS]


def _gather_text(element: ET.Element | None) -> str:
    """Concatenate all text nodes inside `element`.

    Robust against feeds that drop un-escaped HTML directly into `<description>`,
    which the XML parser turns into nested children. `itertext()` walks the tree
    so we recover the whole human-readable string regardless of nesting.
    """

    if element is None:
        return ""
    return "".join(element.itertext())


def _parse_rss(root: ET.Element) -> list[FindCandidate]:
    candidates: list[FindCandidate] = []
    channel = root.find("channel")
    if channel is None:
        return candidates
    for item in channel.findall("item")[:MAX_ITEMS_PER_POLL]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip() or link or title
        description = _clean_summary(_gather_text(item.find("description")))
        pub_date = (item.findtext("pubDate") or "").strip()
        if not (title or link):
            continue
        candidates.append(
            FindCandidate(
                finding_type="post",
                external_id=f"rss:{guid}",
                title=title or link,
                url=link,
                summary_text=description,
                raw_payload={
                    "kind": "post",
                    "feed_format": "rss",
                    "title": title,
                    "link": link,
                    "guid": guid,
                    "pubDate": pub_date,
                    "description_excerpt": description,
                },
                importance_signal="normal",
            )
        )
    return candidates


def _atom_link(entry: ET.Element) -> str:
    link_el = entry.find(f"{_ATOM_NS}link")
    if link_el is not None:
        href = link_el.get("href")
        if href:
            return href.strip()
    return ""


def _parse_atom(root: ET.Element) -> list[FindCandidate]:
    candidates: list[FindCandidate] = []
    for entry in root.findall(f"{_ATOM_NS}entry")[:MAX_ITEMS_PER_POLL]:
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        url = _atom_link(entry)
        entry_id = (entry.findtext(f"{_ATOM_NS}id") or "").strip() or url or title
        summary_raw = _gather_text(entry.find(f"{_ATOM_NS}summary")) or _gather_text(
            entry.find(f"{_ATOM_NS}content")
        )
        summary = _clean_summary(summary_raw)
        published = (entry.findtext(f"{_ATOM_NS}published") or entry.findtext(f"{_ATOM_NS}updated") or "").strip()
        if not (title or url):
            continue
        candidates.append(
            FindCandidate(
                finding_type="post",
                external_id=f"atom:{entry_id}",
                title=title or url,
                url=url,
                summary_text=summary,
                raw_payload={
                    "kind": "post",
                    "feed_format": "atom",
                    "title": title,
                    "url": url,
                    "id": entry_id,
                    "published": published,
                    "summary_excerpt": summary,
                },
                importance_signal="normal",
            )
        )
    return candidates


def _detect_and_parse(xml_text: str) -> list[FindCandidate]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Could not parse RSS/Atom body: %s", exc)
        return []
    tag = root.tag.lower()
    if tag == "rss" or tag.endswith("}rss"):
        return _parse_rss(root)
    if tag.endswith("feed"):
        return _parse_atom(root)
    logger.warning("Unrecognized feed root element: %r", root.tag)
    return []


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    try:
        url = validate_public_https_url(source.target, source_name=source.name)
    except ValueError as exc:
        return PollResult(finds=[], status="error", error=f"Invalid rss_feed target: {exc}")

    try:
        response = await http.get(
            url,
            headers=_conditional_headers(source),
            timeout=DEFAULT_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.warning("rss_feed poll failed for %s: %s", source.name, exc)
        return PollResult(finds=[], status="error", error=str(exc))

    if response.status_code == 304:
        return PollResult(finds=[], status="not_modified", etag=source.etag, last_modified=source.last_modified)
    try:
        validate_public_https_url(str(response.url), source_name=source.name, field="redirect target")
    except ValueError as exc:
        return PollResult(finds=[], status="error", error=f"Unsafe rss_feed redirect: {exc}")
    if response.status_code >= 400:
        return PollResult(
            finds=[],
            status="error",
            error=f"HTTP {response.status_code} from {url}",
        )

    candidates = _detect_and_parse(response.text)
    return PollResult(
        finds=candidates,
        etag=response.headers.get("etag", "") or "",
        last_modified=response.headers.get("last-modified", "") or "",
        status="ok",
    )


register("rss_feed", poll)
