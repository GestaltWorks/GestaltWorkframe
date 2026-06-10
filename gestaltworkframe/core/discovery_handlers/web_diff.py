"""web_diff handler."""

from __future__ import annotations

import hashlib
import re

import httpx

from gestaltworkframe.core.discovery_handlers import DiscoverySourceLike, FindCandidate, PollResult, register
from gestaltworkframe.kb.target_safety import validate_public_https_url

_WS = re.compile(r"\s+")


def _fingerprint(text: str) -> str:
    normalized = _WS.sub(" ", text).strip()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8", "ignore")).hexdigest()


async def poll(source: DiscoverySourceLike, http: httpx.AsyncClient) -> PollResult:
    try:
        url = validate_public_https_url(source.target, source_name=source.name)
    except ValueError as exc:
        return PollResult(finds=[], status="error", error=f"Invalid web_diff target: {exc}")
    response = await http.get(url, headers={"User-Agent": "gestalt-workframe-discovery/1.0"}, timeout=20, follow_redirects=True)
    try:
        validate_public_https_url(str(response.url), source_name=source.name, field="redirect target")
    except ValueError as exc:
        return PollResult(finds=[], status="error", error=f"Unsafe web_diff redirect: {exc}")
    if response.status_code >= 400:
        return PollResult(finds=[], status="error", error=f"web_diff HTTP {response.status_code}")
    digest = _fingerprint(response.text)
    if source.etag == digest:
        return PollResult(finds=[], status="not_modified", etag=digest)
    title = f"Web page changed: {source.name}"
    return PollResult(
        finds=[
            FindCandidate(
                finding_type="diff",
                external_id=f"web-diff:{digest}",
                title=title,
                url=url,
                summary_text="Tracked page content changed since the previous successful poll.",
                raw_payload={"kind": "web_diff", "url": url, "fingerprint": digest},
                importance_signal="normal",
            )
        ],
        etag=digest,
    )


register("web_diff", poll)