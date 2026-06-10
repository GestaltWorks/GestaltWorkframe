"""Email digest scaffold for discovery findings.

Off by default. The scheduler and future cron/timer path can call this module
after a run to summarize queue activity for an operator without exposing secrets
or publishing content.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gestaltworkframe.core.discovery_queue import list_recent_finds
from gestaltworkframe.core.discovery_summary import summarize_discovery_finds
from gestaltworkframe.core.email_service import NotificationStatus, send_internal_email


@dataclass(frozen=True)
class DiscoveryDigestConfig:
    enabled: bool
    recipient: str
    max_items: int = 100

    @classmethod
    def from_env(cls) -> "DiscoveryDigestConfig":
        return cls(
            enabled=os.getenv("DISCOVERY_DIGEST_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"},
            recipient=(os.getenv("DISCOVERY_DIGEST_RECIPIENT") or os.getenv("DISCOVERY_DIGEST_TO") or "").strip(),
            max_items=_int_env("DISCOVERY_DIGEST_MAX_ITEMS", 100),
        )


def render_discovery_digest_html(finds: list[dict[str, Any]]) -> str:
    return render_discovery_digest_html_from_summary(summarize_discovery_finds(finds))


def render_discovery_digest_html_from_summary(summary: dict[str, Any]) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    suggested = _render_items(summary["suggested_posts"]) or "<li>No strong Updates candidates in this batch.</li>"
    ingestion = _render_items(summary.get("ingestion_candidates", [])) or "<li>No strong library indexing candidates in this batch.</li>"
    new_sources = _render_items(summary["new_source_candidates"]) or "<li>No new source candidates in this batch.</li>"
    routine = _render_items(summary.get("routine_updates", [])) or "<li>No routine tracked-source updates in this batch.</li>"
    topics = "".join(
        f"<li><strong>{html.escape(str(group['topic']))}</strong>: {group['count']} findings, "
        f"{group['newsletter_candidates']} newsletter candidates</li>"
        for group in summary["topic_groups"][:8]
    ) or "<li>No topics detected.</li>"
    sources = "".join(
        f"<li><strong>{html.escape(str(source['source_name']))}</strong>: {source['count']} findings, "
        f"{source['notable_count']} notable</li>"
        for source in summary["prominent_sources"]
    ) or "<li>No active sources in this batch.</li>"
    return (
        "<h1>Discovery Newsletter</h1>"
        f"<p>Generated {html.escape(generated)}. Review findings in the admin discovery queue.</p>"
        f"<p><strong>{summary['total']}</strong> recent findings, <strong>{summary['high_importance']}</strong> high-importance, "
        f"<strong>{len(summary['suggested_posts'])}</strong> suggested Updates candidates.</p>"
        "<h2>Suggested Updates and Additions picks</h2>"
        f"<ol>{suggested}</ol>"
        "<h2>library indexing candidates</h2>"
        f"<ol>{ingestion}</ol>"
        "<h2>New sources to consider</h2>"
        f"<ol>{new_sources}</ol>"
        "<h2>Routine tracked-source updates</h2>"
        f"<ol>{routine}</ol>"
        "<h2>Topic map</h2>"
        f"<ul>{topics}</ul>"
        "<h2>Prominent sources</h2>"
        f"<ul>{sources}</ul>"
    )


async def send_discovery_digest(
    session: AsyncSession,
    *,
    config: DiscoveryDigestConfig | None = None,
) -> NotificationStatus:
    cfg = config or DiscoveryDigestConfig.from_env()
    if not cfg.enabled:
        return "skipped"
    finds = await list_recent_finds(session, limit=cfg.max_items, status="pending")
    summary = summarize_discovery_finds(finds)
    html_body = render_discovery_digest_html_from_summary(summary)
    subject = f"[discovery newsletter] {len(finds)} findings / {len(summary['suggested_posts'])} suggested picks"
    return await send_internal_email(subject, html_body, recipient=cfg.recipient or None)


def _render_items(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        title = html.escape(str(item.get("title") or "Untitled"))
        url = html.escape(_safe_href(str(item.get("url") or "")), quote=True)
        source = html.escape(str(item.get("source_name") or "unknown"))
        topic = html.escape(str(item.get("review_topic") or "review"))
        score = html.escape(str(item.get("newsletter_score") or 0))
        action = html.escape(str(item.get("suggested_action") or "Review."))
        tags = html.escape(", ".join(str(tag) for tag in item.get("review_tags") or []))
        link = f'<a href="{url}">{title}</a>' if url else title
        rows.append(f"<li><strong>{link}</strong><br><small>{source} / {topic} / score {score} / {tags}</small><p>{action}</p></li>")
    return "\n".join(rows)


def _safe_href(url: str) -> str:
    cleaned = url.strip()
    if cleaned.startswith(("https://", "http://")):
        return cleaned
    return ""


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default