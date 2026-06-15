"""Newsletter composition, rendering, and distribution.

The composer pulls newsletter_pending DiscoveryFind rows, snapshots them
into a NewsletterIssue, and prepares the issue for the operator to
review at /admin/newsletter. The operator can edit the editorial intro
and the subject line, then approve.

Approval triggers distribution:

- email: one M365 Graph send per active Subscriber, with their
  personalized unsubscribe token in the footer.
- web: the issue becomes publicly readable at /library/latest/<slug>
  (Phase 4 wires the render path).
- linkedin: Phase 7. For v1 the admin page exposes a Copy for LinkedIn
  button so the operator can post manually.

All three send paths write NewsletterDelivery audit rows. After a
successful email batch, every find in the issue has its newsletter_pending
flag flipped back to False so the next cycle sees only new material.

Brand voice rules (CLAUDE.md): plain register, no em dashes, no
manufactured enthusiasm. The HTML/plain/LinkedIn renderers apply those
constraints by construction; the editorial markdown is operator-authored
and trusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete as sql_delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from gestaltworkframe.core.db.models import (
    DiscoveryFind,
    DiscoverySource,
    NewsletterDelivery,
    NewsletterIssue,
)
from gestaltworkframe.core.discovery_queue import _serialize_public_find
from gestaltworkframe.core.email_service import send_internal_email
from gestaltworkframe.core.linkedin import post_to_linkedin as _post_to_linkedin
from gestaltworkframe.core.subscribers import active_subscribers

logger = logging.getLogger(__name__)

# Cycle length the cadence scheduler runs on. Composer queries cover the
# trailing CYCLE_DAYS by default when no explicit window is supplied.
CYCLE_DAYS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _site_url() -> str:
    from gestaltworkframe.core.deployment_config import get_deployment_config
    default = get_deployment_config().site.base_url
    return os.getenv("SITE_PUBLIC_URL", default).rstrip("/")


def _newsletter_label() -> str:
    from gestaltworkframe.core.deployment_config import get_deployment_config
    cfg = get_deployment_config()
    return cfg.newsletter.name or f"{cfg.identity.short_name} signals"


def _organization_label() -> str:
    from gestaltworkframe.core.deployment_config import get_deployment_config
    return get_deployment_config().identity.organization_name


def _slugify_date(when: datetime) -> str:
    return when.strftime("%Y-%m-%d")


def _unique_slug(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _default_subject(period_start: datetime, period_end: datetime, find_count: int) -> str:
    label = period_end.strftime("%b %d")
    return f"{_newsletter_label()} - {label} ({find_count} {'item' if find_count == 1 else 'items'})"


# ---------------------------------------------------------------------------
# Per-issue assignment helpers (ship-gated numbering model)
# ---------------------------------------------------------------------------


def _label_letter(idx: int) -> str:
    """Convert a 0-based index into a base-26 lowercase letter suffix.

    0 -> 'a', 25 -> 'z', 26 -> 'aa', 51 -> 'az', 52 -> 'ba'. This is a
    spreadsheet-column-style sequence so we don't cap the operator at
    26 unsent drafts per epoch.

    Mirrors the migration backfill's _label_letter helper; the two
    implementations must produce identical output.
    """
    if idx < 0:
        raise ValueError(f"label index must be non-negative, got {idx}")
    n = idx
    chars: list[str] = []
    while True:
        chars.append(chr(ord("a") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(chars))


async def _next_ship_number(session: AsyncSession) -> int:
    """Next monotonic ship_number. First shipped issue is 1."""
    row = (
        await session.execute(
            select(func.max(NewsletterIssue.ship_number))
        )
    ).scalar_one()
    return int(row or 0) + 1


async def _assign_ship_number_with_retry(
    session: AsyncSession,
    issue_id: str,
    *,
    attempts: int = 3,
) -> NewsletterIssue:
    """Assign the public issue number before any external send side effects.

    SQLite serializes writes, but two dispatchers can still compute the same
    MAX(ship_number)+1 for different issues. UNIQUE constraints catch that;
    this helper rolls back and retries before emails or public delivery rows
    are written so a collision cannot strand an issue in `sending`.
    """
    for attempt in range(attempts):
        issue = (
            await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
        ).scalar_one()
        ship_number = await _next_ship_number(session)
        issue.ship_number = ship_number
        issue.display_label = str(ship_number)
        issue.updated_at = _now()
        session.add(issue)
        try:
            await session.commit()
            await session.refresh(issue)
            return issue
        except IntegrityError:
            await session.rollback()
            logger.warning(
                "Newsletter issue %s ship_number collision on attempt %s",
                issue_id,
                attempt + 1,
            )
    try:
        issue = (
            await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
        ).scalar_one()
        issue.status = "awaiting_approval"
        issue.scheduled_send_at = None
        issue.updated_at = _now()
        issue.notes = (issue.notes + "\nShip-number assignment failed; operator review required.").strip()
        session.add(issue)
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("Could not revert newsletter issue %s after ship-number collisions", issue_id)
    raise RuntimeError(f"Could not assign newsletter ship number for issue {issue_id}")


def _preview_issue_with_finds_json(issue: NewsletterIssue, finds_json: str) -> NewsletterIssue:
    return NewsletterIssue(
        id=issue.id,
        ship_number=issue.ship_number,
        display_label=issue.display_label,
        slug=issue.slug,
        period_start=issue.period_start,
        period_end=issue.period_end,
        status=issue.status,
        editorial_markdown=issue.editorial_markdown,
        finds_json=finds_json,
        subject=issue.subject,
        approved_by=issue.approved_by,
        approved_at=issue.approved_at,
        target_send_at=issue.target_send_at,
        approval_email_sent_at=issue.approval_email_sent_at,
        scheduled_send_at=issue.scheduled_send_at,
        sent_at=issue.sent_at,
        unpublished_at=issue.unpublished_at,
        notes=issue.notes,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
    )


def _display_label_epoch(label: str) -> int | None:
    digits = ""
    for char in label:
        if not char.isdigit():
            break
        digits += char
    suffix = label[len(digits):]
    if not digits or not suffix or not suffix.islower() or not suffix.isalpha():
        return None
    return int(digits)

async def next_display_label(session: AsyncSession) -> str:
    """Compute the next sticky display label for a freshly-created issue.

    Algorithm: anchor on max(ship_number) (call it `last_ship`,
    defaults to 0 if nothing has shipped yet), then count the unsent
    rows whose display_label is already in that epoch and append a
    base-26 letter suffix. So:

    - Empty DB:              "0a"
    - One unsent draft "0a": "0b"
    - After Issue 1 ships:   next draft is "1a"
    - "0b" lingers as "0b":  sticky, never relabeled

    Pre-existing draft "1c" that ships -> becomes "2" (assigned by
    _dispatch_issue). The next draft created after that ship is "2a".
    """
    last_ship_row = (
        await session.execute(
            select(func.max(NewsletterIssue.ship_number))
        )
    ).scalar_one()
    last_ship = int(last_ship_row or 0)
    labels = (
        await session.execute(
            select(NewsletterIssue.display_label)
            .where(NewsletterIssue.ship_number.is_(None))
        )
    ).scalars().all()
    epoch_count = sum(1 for label in labels if _display_label_epoch(label or "") == last_ship)
    return f"{last_ship}{_label_letter(epoch_count)}"



async def next_default_target_send_at(session: AsyncSession) -> datetime:
    """Suggested target_send_at for a new issue.

    Anchored to the last sent issue's target_send_at + 10 days so the
    operator's chosen cadence sticks. Falls back to today + 10 days
    when there is no prior sent issue.
    """
    now = _now()
    last_sent = (
        await session.execute(
            select(NewsletterIssue)
            .where(NewsletterIssue.status == "sent")
            .order_by(NewsletterIssue.sent_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_sent is None:
        return now + timedelta(days=10)
    anchor = (
        last_sent.target_send_at
        or last_sent.scheduled_send_at
        or last_sent.sent_at
        or last_sent.created_at
    )
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    target = anchor + timedelta(days=10)
    # Push past-dated suggestions forward so the operator never sees a
    # date that's already gone.
    if target <= now:
        target = now + timedelta(days=10)
    return target


async def create_empty_issue(
    session: AsyncSession,
    *,
    target_send_at: datetime | None = None,
    subject: str = "",
    notes: str = "",
) -> NewsletterIssue:
    """Create a new draft issue with a sticky display_label.

    No ship_number is assigned yet (that happens at successful send).
    Display-label collisions are possible under concurrent draft creation;
    retry a few times so the UNIQUE(display_label) guard becomes recovery,
    not a user-visible 500.
    """
    target = target_send_at or await next_default_target_send_at(session)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    for attempt in range(3):
        now = _now()
        label = await next_display_label(session)
        base_slug = f"issue-{label}-{_slugify_date(target)}"
        existing_slugs = {
            row
            for row in (
                await session.execute(select(NewsletterIssue.slug))
            ).scalars().all()
        }
        slug = _unique_slug(base_slug, existing_slugs)
        issue = NewsletterIssue(
            ship_number=None,
            display_label=label,
            slug=slug,
            period_start=target - timedelta(days=10),
            period_end=target,
            status="draft",
            editorial_markdown="",
            finds_json="[]",
            subject=subject.strip() or f"Issue {label}",
            target_send_at=target,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        session.add(issue)
        try:
            await session.commit()
            await session.refresh(issue)
            return issue
        except IntegrityError:
            await session.rollback()
            logger.warning("Newsletter draft label collision on create attempt %s", attempt + 1)
    raise RuntimeError("Could not assign newsletter draft display label")


# Issue statuses where the operator can still edit + tag finds. Closed
# issues (sent, skipped) reject re-assignment.
_OPEN_ISSUE_STATUSES = frozenset({"draft", "awaiting_approval", "approved"})


async def assign_find_to_issue(
    session: AsyncSession,
    find_id: str,
    issue_id: str | None,
) -> DiscoveryFind:
    """Tag a find for a specific issue, or clear the assignment.

    issue_id=None removes the find from whatever issue it was tagged
    to. issue_id pointing at a sent / skipped issue is rejected with
    ValueError so historical issues stay immutable.

    Also mirrors the legacy newsletter_pending boolean so older code
    paths and serializers stay consistent during the model transition.
    """
    find = (
        await session.execute(select(DiscoveryFind).where(DiscoveryFind.id == find_id))
    ).scalar_one_or_none()
    if find is None:
        raise LookupError(f"Discovery find not found: {find_id}")

    if issue_id is None:
        find.newsletter_issue_id = None
        find.newsletter_pending = False
    else:
        issue = (
            await session.execute(
                select(NewsletterIssue).where(NewsletterIssue.id == issue_id)
            )
        ).scalar_one_or_none()
        if issue is None:
            raise LookupError(f"Newsletter issue not found: {issue_id}")
        if issue.status not in _OPEN_ISSUE_STATUSES:
            raise ValueError(
                f"Cannot tag a find onto a {issue.status} issue; pick a draft or "
                "awaiting-approval issue, or create a new one."
            )
        find.newsletter_issue_id = issue.id
        find.newsletter_pending = True
        if find.dismissed:
            logger.info("Discovery find %s un-dismissed by newsletter assignment", find.id)
            find.dismissed = False

    await session.commit()
    await session.refresh(find)
    return find


async def list_assignable_issues(session: AsyncSession) -> list[NewsletterIssue]:
    """Issues the operator can still tag finds onto: drafts and any
    open awaiting_approval / approved issues whose scheduled send is
    still in the future. Sent / skipped issues are excluded.
    """
    now = _now()
    statement = (
        select(NewsletterIssue)
        .where(NewsletterIssue.status.in_(["draft", "awaiting_approval"]))
        .order_by(NewsletterIssue.target_send_at.asc().nulls_last())
    )
    base = (await session.execute(statement)).scalars().all()
    # Include approved issues whose send hasn't fired yet so the
    # operator can still swap items in last-minute (until cancel
    # window closes via the dispatcher).
    approved = (
        await session.execute(
            select(NewsletterIssue)
            .where(NewsletterIssue.status == "approved")
            .where(NewsletterIssue.scheduled_send_at.is_not(None))
            .where(NewsletterIssue.scheduled_send_at > now)
            .order_by(NewsletterIssue.scheduled_send_at.asc().nulls_last())
        )
    ).scalars().all()
    return list(base) + list(approved)


async def live_finds_for_issue(
    session: AsyncSession,
    issue_id: str,
) -> list[dict[str, Any]]:
    """Find rows currently tagged to this issue, freshly serialized.

    Used by the admin Compose view so the operator always sees the
    current tagged state, not the finds_json snapshot. The snapshot
    is only authoritative after the issue has been sent.
    """
    rows = (
        await session.execute(
            select(DiscoveryFind, DiscoverySource)
            .join(
                DiscoverySource,
                DiscoverySource.id == DiscoveryFind.discovery_source_id,
            )
            .where(DiscoveryFind.newsletter_issue_id == issue_id)
            .order_by(DiscoveryFind.last_seen_at.desc())
        )
    ).all()
    return [_serialize_public_find(find, source) for find, source in rows]


async def auto_populate_draft(
    session: AsyncSession,
    issue_id: str,
    *,
    window_days: int = 10,
) -> int:
    """Tag every unassigned, eligible auto_indexed find from the last
    `window_days` into this draft. Used by the daily cron when it
    auto-creates the next-cycle draft so the operator opens the
    Compose view with items already populated.

    "Eligible" means: status=auto_indexed AND newsletter_issue_id IS
    NULL AND dismissed=False AND published_in_newsletter_at IS NULL
    (not already sent in a prior issue). Returns the count tagged.
    """
    cutoff = _now() - timedelta(days=max(1, window_days))
    eligible = (
        await session.execute(
            select(DiscoveryFind.id)
            .where(DiscoveryFind.status == "auto_indexed")
            .where(DiscoveryFind.newsletter_issue_id.is_(None))
            .where(DiscoveryFind.dismissed.is_(False))
            .where(DiscoveryFind.published_in_newsletter_at.is_(None))
            .where(DiscoveryFind.first_seen_at >= cutoff)
        )
    ).scalars().all()
    if not eligible:
        return 0
    for start in range(0, len(eligible), 500):
        batch = eligible[start:start + 500]
        await session.execute(
            update(DiscoveryFind)
            .where(DiscoveryFind.id.in_(batch))
            .values(newsletter_issue_id=issue_id, newsletter_pending=True)
        )
    await session.commit()
    return len(eligible)


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposeResult:
    issue: NewsletterIssue
    created: bool  # False when no pending finds and the cycle should be skipped


async def compose_pending_issue(
    session: AsyncSession,
    *,
    period_days: int = CYCLE_DAYS,
    force: bool = False,
) -> ComposeResult:
    """Build a draft issue from currently-pending finds.

    When no finds are pending and `force=False`, returns a placeholder
    issue with status="skipped" and created=False so the scheduler can
    record the cycle without bothering the operator.
    """
    now = _now()
    period_start = now - timedelta(days=max(1, period_days))

    statement = (
        select(DiscoveryFind, DiscoverySource)
        .join(DiscoverySource, DiscoverySource.id == DiscoveryFind.discovery_source_id)
        .where(DiscoveryFind.newsletter_pending == True)  # noqa: E712
        .where(DiscoveryFind.dismissed == False)  # noqa: E712
        .order_by(DiscoveryFind.first_seen_at.desc())
    )
    result = await session.execute(statement)
    rows = result.all()
    find_dicts = [_serialize_public_find(find, source) for find, source in rows]

    existing_slugs = {row.slug for row in (await session.execute(select(NewsletterIssue))).scalars()}
    label = await next_display_label(session)
    base_slug = f"issue-{label}-{_slugify_date(now)}"
    slug = _unique_slug(base_slug, existing_slugs)

    if not find_dicts and not force:
        # Record the skipped cycle so the cadence run is auditable, but
        # don't bother the operator with an approval email. The skipped
        # row still consumes a display_label so the audit trail reads
        # naturally; that label sticks (it never becomes a ship number).
        skipped = NewsletterIssue(
            ship_number=None,
            display_label=label,
            slug=slug,
            period_start=period_start,
            period_end=now,
            status="skipped",
            subject="",
            finds_json="[]",
            notes="No pending finds at compose time; cycle skipped.",
        )
        session.add(skipped)
        await session.commit()
        await session.refresh(skipped)
        return ComposeResult(issue=skipped, created=False)

    issue = NewsletterIssue(
        ship_number=None,
        display_label=label,
        slug=slug,
        period_start=period_start,
        period_end=now,
        status="awaiting_approval",
        subject=_default_subject(period_start, now, len(find_dicts)),
        finds_json=json.dumps(find_dicts),
        editorial_markdown="",
    )
    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    return ComposeResult(issue=issue, created=True)


async def update_editorial(
    session: AsyncSession,
    issue_id: str,
    *,
    editorial_markdown: str,
    subject: str | None = None,
) -> NewsletterIssue:
    """Persist edits to the issue's editorial intro and optional subject."""
    issue = (await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))).scalar_one_or_none()
    if issue is None:
        raise LookupError(f"Newsletter issue not found: {issue_id}")
    if issue.status not in {"draft", "awaiting_approval"}:
        raise ValueError(f"Cannot edit a {issue.status} issue")
    issue.editorial_markdown = editorial_markdown[:20000]
    if subject is not None:
        issue.subject = subject.strip()[:200]
    issue.updated_at = _now()
    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    return issue


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


# Allowed URL schemes for any interpolated href in the renderer. Anything
# else (javascript:, data:, file:, vbscript:, ...) is dropped and replaced
# with "#" so a poisoned discovery feed or an operator typo can't inject
# script-execution context into the email or the iframe preview.
_SAFE_URL_SCHEMES = frozenset({"http", "https"})


def _safe_url(url: str) -> str:
    """Return `url` if it parses to an http/https URL, else '#'.

    Public newsletter renderer + admin iframe preview consume this. The
    bar is intentionally strict: even relative URLs return '#' because
    every card link in this surface should be an absolute external URL.
    """
    if not url or not isinstance(url, str):
        return "#"
    try:
        parsed = urlparse(url.strip())
    except (ValueError, AttributeError):
        return "#"
    if parsed.scheme.lower() not in _SAFE_URL_SCHEMES:
        return "#"
    if not parsed.netloc:
        return "#"
    return url.strip()


def _e(value: Any) -> str:
    """HTML-escape an interpolated value for both text-content AND
    attribute-content positions. The quote=True kwarg matters: without
    it a `"` in the input breaks out of an `href="..."` attribute and
    re-opens XSS. Always pass this to {...}-formatted HTML."""
    return html.escape(str(value or ""), quote=True)


def _markdown_to_html_paragraphs(text: str) -> str:
    """Tiny markdown subset: split on blank lines into <p>, support
    **bold**, *italic*, and [text](url) inline. Intentionally minimal so
    we don't need a full markdown parser and so untrusted markdown
    can't escape the rendering envelope.

    Hardening: we now (1) escape the whole input including quotes, then
    re-emit recognized markdown patterns AFTER capture so the captured
    text/URL is already attribute-safe; (2) validate the captured URL
    against the http/https allowlist via _safe_url before emitting the
    href; (3) emit the link with quote-safe escaping. A `"` in the URL
    or a `javascript:` scheme can no longer escape the envelope.
    """
    if not text:
        return ""
    # Step 1: escape EVERYTHING including quotes, so a `"` in a markdown
    # URL captured later cannot break out of href="..."
    safe = html.escape(text, quote=True)
    paragraphs = [p.strip() for p in safe.split("\n\n") if p.strip()]
    rendered = []

    def _link_sub(match: re.Match) -> str:
        label = match.group(1)  # already HTML-escaped at step 1
        url = match.group(2)    # already HTML-escaped at step 1
        # html.escape mangles & -> &amp; but leaves http:// intact. We
        # need to UN-escape for the scheme check then re-escape with
        # quote=True for safe attribute placement.
        raw_url = html.unescape(url)
        safe_target = _safe_url(raw_url)
        return f'<a href="{html.escape(safe_target, quote=True)}">{label}</a>'

    for para in paragraphs:
        # The URL regex runs against ALREADY-ESCAPED text: `&` is `&amp;`,
        # `"` is `&quot;` so they cannot terminate the href attribute.
        para = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _link_sub, para)
        para = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", para)
        para = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", para)
        para = para.replace("\n", "<br/>")
        rendered.append(f"<p>{para}</p>")
    return "\n".join(rendered)


def render_issue_html(issue: NewsletterIssue, *, unsubscribe_url: str) -> str:
    """Brand-styled HTML for the email send and the public web view.

    Hardening: every interpolated find field is HTML-escaped via _e and
    every emitted href runs through _safe_url so a poisoned discovery
    feed can't inject script content or break out of attribute context.
    The editorial markdown is run through _markdown_to_html_paragraphs,
    which is also attribute-safe (see its docstring).
    """
    finds = json.loads(issue.finds_json) if issue.finds_json else []
    editorial_html = _markdown_to_html_paragraphs(issue.editorial_markdown)
    safe_unsub = _e(_safe_url(unsubscribe_url))
    safe_subject = _e(issue.subject)
    cards = []
    for find in finds:
        title = _e(find.get("display_title") or find.get("title") or "Update")
        url = _e(_safe_url(find.get("url") or ""))
        source = _e(find.get("display_source_name") or find.get("source_name") or "")
        summary = _e(find.get("summary_text") or "")
        topic = _e(find.get("review_topic") or "")
        meta_tail = f" &middot; {topic}" if topic else ""
        cards.append(
            "<tr><td style=\"padding:0 0 20px\">"
            "<div style=\"border:1px solid #d4bf9133;border-radius:14px;padding:16px;background:#fafaf7\">"
            f"<p style=\"margin:0 0 6px;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#7a6440\">{source}{meta_tail}</p>"
            f"<h3 style=\"margin:0 0 8px;font-size:18px;font-weight:600;color:#242128\"><a href=\"{url}\" style=\"color:#242128;text-decoration:none\">{title}</a></h3>"
            f"<p style=\"margin:0 0 10px;font-size:14px;line-height:1.55;color:#3f3a47\">{summary}</p>"
            f"<a href=\"{url}\" style=\"font-size:13px;color:#8a7126;text-decoration:underline\">Open source</a>"
            "</div></td></tr>"
        )
    cards_html = "".join(cards) or '<tr><td style="padding:16px;color:#888">No included items.</td></tr>'

    safe_site = _e(_safe_url(_site_url()))
    site_host = _e(_safe_url(_site_url()).replace("https://", "").replace("http://", "").rstrip("/"))
    safe_label = _e(_newsletter_label())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>{safe_subject}</title></head>
<body style="margin:0;padding:0;background:#f0ece2;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f0ece2;padding:24px 0">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#fffdf7;border-radius:16px;border:1px solid #d4bf9144;padding:32px">
<tr><td>
<p style="margin:0 0 6px;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:#7a6440">{safe_label}</p>
<h1 style="margin:0 0 8px;font-size:26px;color:#242128">{safe_subject}</h1>
<p style="margin:0 0 18px;font-size:13px;color:#7a6440">{issue.period_start.strftime('%b %d')} - {issue.period_end.strftime('%b %d, %Y')}</p>
{editorial_html}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:12px">
{cards_html}
</table>
<hr style="border:none;border-top:1px solid #d4bf9133;margin:24px 0 12px"/>
<p style="margin:0;font-size:12px;color:#7a6440">
Sent from <a href="{safe_site}" style="color:#7a6440">{site_host}</a> &middot;
<a href="{safe_unsub}" style="color:#7a6440">Unsubscribe</a>
</p>
</td></tr></table>
</td></tr></table>
</body></html>"""


def render_issue_plain(issue: NewsletterIssue, *, unsubscribe_url: str) -> str:
    """Plain-text version for the email's text alternative. URLs are
    scheme-filtered the same way as the HTML render so a `javascript:`
    URL never lands in the text body either."""
    finds = json.loads(issue.finds_json) if issue.finds_json else []
    lines: list[str] = [issue.subject, ""]
    if issue.editorial_markdown.strip():
        lines.append(issue.editorial_markdown.strip())
        lines.append("")
    for find in finds:
        title = find.get("display_title") or find.get("title") or "Update"
        raw_url = find.get("url") or ""
        url = _safe_url(raw_url) if raw_url else ""
        source = find.get("display_source_name") or find.get("source_name") or ""
        summary = find.get("summary_text") or ""
        lines.append(f"- {title}")
        if source:
            lines.append(f"  Source: {source}")
        if summary:
            lines.append(f"  {summary}")
        if url and url != "#":
            lines.append(f"  {url}")
        lines.append("")
    safe_unsub = _safe_url(unsubscribe_url)
    lines.append("--")
    lines.append(f"{_organization_label()} / {_safe_url(_site_url())}")
    lines.append(f"Unsubscribe: {safe_unsub}")
    return "\n".join(lines)


def render_issue_linkedin(issue: NewsletterIssue) -> str:
    """LinkedIn-friendly plain text. No HTML, no markdown bold; LinkedIn
    renders newlines naturally and ignores most formatting characters.

    Each find becomes a short bullet with the source name and the URL.
    LinkedIn's preview-card system will hit the first URL in the post,
    so the editorial intro goes first.
    """
    finds = json.loads(issue.finds_json) if issue.finds_json else []
    lines: list[str] = []
    if issue.editorial_markdown.strip():
        lines.append(issue.editorial_markdown.strip())
        lines.append("")
    lines.append(f"{_newsletter_label()} - {issue.period_end.strftime('%b %d, %Y')}")
    lines.append("")
    for find in finds:
        title = find.get("display_title") or find.get("title") or "Update"
        raw_url = find.get("url") or ""
        url = _safe_url(raw_url) if raw_url else ""
        source = find.get("display_source_name") or find.get("source_name") or ""
        prefix = f"{source}: " if source else ""
        lines.append(f"- {prefix}{title}")
        if url and url != "#":
            lines.append(f"  {url}")
    lines.append("")
    lines.append(f"Full archive: {_site_url()}/library/latest")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------


def _unsubscribe_url(token: str) -> str:
    return f"{_site_url()}/newsletter/unsubscribe?token={token}"


# Default delay between approval and actual send. Gives the operator a
# cancel window for second thoughts or a last typo catch.
DEFAULT_SCHEDULE_DELAY = timedelta(minutes=30)


async def approve_and_schedule(
    session: AsyncSession,
    issue_id: str,
    *,
    approved_by: str,
    scheduled_send_at: datetime | None = None,
) -> NewsletterIssue:
    """Mark an issue approved and schedule it for send.

    Replaces the previous immediate-send approve_and_distribute on the
    operator's request path. The state flip is atomic; subsequent
    distribution runs in dispatch_scheduled_issues() below, which the
    cron and the in-process scheduler both call.

    scheduled_send_at:
    - None or in the past -> default to now + DEFAULT_SCHEDULE_DELAY.
      This gives the operator a 30-minute cancel window for any
      approval click.
    - Future datetime -> use as-is. The dispatcher fires when the
      timestamp passes.

    Returns the issue in its new approved state. Raises ValueError if
    the issue is already past awaiting_approval (handled by an atomic
    UPDATE that affects zero rows on a race or a duplicate click).
    """
    from sqlalchemy import update

    now = _now()
    if scheduled_send_at is None or scheduled_send_at <= now:
        send_at = now + DEFAULT_SCHEDULE_DELAY
    else:
        send_at = scheduled_send_at

    result = await session.execute(
        update(NewsletterIssue)
        .where(NewsletterIssue.id == issue_id)
        .where(NewsletterIssue.status.in_(["draft", "awaiting_approval"]))
        .values(
            status="approved",
            approved_by=approved_by or "admin",
            approved_at=now,
            scheduled_send_at=send_at,
            updated_at=now,
        )
    )
    affected = result.rowcount if result.rowcount is not None else 0
    await session.commit()

    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        raise LookupError(f"Newsletter issue not found: {issue_id}")
    if affected == 0:
        raise ValueError(f"Cannot approve a {issue.status} issue")
    return issue


async def cancel_scheduled_send(
    session: AsyncSession,
    issue_id: str,
    *,
    cancelled_by: str,
) -> NewsletterIssue:
    """Pull a scheduled issue back to awaiting_approval.

    Only valid while status=approved and scheduled_send_at is still in
    the future. After the dispatcher fires (status -> sent), this is a
    no-op error: you cannot unsend an issue.
    """
    now = _now()
    result = await session.execute(
        update(NewsletterIssue)
        .where(NewsletterIssue.id == issue_id)
        .where(NewsletterIssue.status == "approved")
        .where(NewsletterIssue.scheduled_send_at.is_not(None))
        .where(NewsletterIssue.scheduled_send_at > now)
        .values(
            status="awaiting_approval",
            scheduled_send_at=None,
            updated_at=now,
            notes=NewsletterIssue.notes,  # untouched
        )
    )
    affected = result.rowcount if result.rowcount is not None else 0
    await session.commit()
    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        raise LookupError(f"Newsletter issue not found: {issue_id}")
    if affected == 0:
        # Either status isn't approved, scheduled_send_at is null, or
        # the send time already passed. All cases: can't cancel.
        raise ValueError(
            f"Cannot cancel: status={issue.status}, scheduled_send_at={issue.scheduled_send_at}"
        )
    logger.info("Newsletter issue %s send cancelled by %s", issue_id, cancelled_by)
    return issue


async def delete_issue(
    session: AsyncSession,
    issue_id: str,
    *,
    deleted_by: str,
) -> dict[str, Any]:
    """Hard-delete a newsletter issue and detach its tagged finds.

    Works on any status. Side effects:
    - Every DiscoveryFind with newsletter_issue_id == issue_id gets
      detached from the issue and newsletter_pending=False. Sent/sending
      issue finds keep published_in_newsletter_at for audit; open issue
      finds are reset so the operator can re-queue them.
    - NewsletterDelivery rows for this issue are deleted (no FK
      cascade declared at the SQLite level, so we do it explicitly).
    - The issue row itself is deleted.

    Returns a summary dict so the admin endpoint can echo what
    actually happened (find count reverted, delivery count purged).
    """
    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        raise LookupError(f"Newsletter issue not found: {issue_id}")

    tagged = (
        await session.execute(
            select(DiscoveryFind).where(DiscoveryFind.newsletter_issue_id == issue_id)
        )
    ).scalars().all()
    preserve_published_at = issue.status in {"sent", "sending"}
    for find in tagged:
        find.newsletter_issue_id = None
        find.newsletter_pending = False
        if not preserve_published_at:
            find.published_in_newsletter_at = None
    finds_reverted = len(tagged)

    delivery_result = await session.execute(
        sql_delete(NewsletterDelivery).where(NewsletterDelivery.issue_id == issue_id)
    )
    deliveries_purged = delivery_result.rowcount if delivery_result.rowcount is not None else 0

    summary = {
        "id": issue.id,
        "display_label": issue.display_label,
        "status": issue.status,
        "finds_reverted": finds_reverted,
        "deliveries_purged": deliveries_purged,
        "deleted_by": deleted_by,
    }
    await session.delete(issue)
    await session.commit()
    logger.info(
        "Newsletter issue %s (%s) deleted by %s: finds_reverted=%s deliveries_purged=%s",
        issue_id, summary["display_label"], deleted_by, finds_reverted, deliveries_purged,
    )
    return summary


async def unpublish_issue(
    session: AsyncSession,
    issue_id: str,
    *,
    unpublished_by: str,
) -> NewsletterIssue:
    """Soft-hide a sent or scheduled issue from public surfaces.

    Behavior by status:
    - sent / sending: stamp unpublished_at = now. Public archive
      filters this out; deliveries / finds / published_in_newsletter_at
      are preserved for audit.
    - approved (scheduled in future): cancel-and-hide. Stamps
      unpublished_at, clears scheduled_send_at, reverts status to
      awaiting_approval. The operator should usually just cancel
      first then unpublish if needed, but doing both in one click
      keeps the API symmetric.
    - draft / awaiting_approval / skipped: not meaningful; raises
      ValueError so the panel can disable the button at the UI level.

    Re-publish is not currently supported; clear unpublished_at via
    direct DB edit if you need to restore one.
    """
    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        raise LookupError(f"Newsletter issue not found: {issue_id}")

    if issue.unpublished_at is not None:
        return issue

    now = _now()
    if issue.status in {"sent", "sending"}:
        issue.unpublished_at = now
        issue.updated_at = now
    elif issue.status == "approved":
        issue.unpublished_at = now
        issue.scheduled_send_at = None
        issue.status = "awaiting_approval"
        issue.updated_at = now
    else:
        raise ValueError(
            f"Cannot unpublish a {issue.status} issue; only sent / sending / "
            "approved issues can be unpublished. Delete the draft instead."
        )

    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    logger.info(
        "Newsletter issue %s (%s) unpublished by %s from status=%s",
        issue_id, issue.display_label, unpublished_by, issue.status,
    )
    return issue


async def dispatch_scheduled_issues(session: AsyncSession) -> dict[str, int]:
    """Find approved issues whose scheduled send time has passed and
    dispatch them. Called both by the every-10-day scheduler cron AND by
    any approval that schedules in the past (which the schedule helper
    bumps to +30 min so this path is the typical one).

    Returns {"dispatched": N, "failed": M} summary so callers can log.
    """
    now = _now()
    due_ids = (
        await session.execute(
            select(NewsletterIssue.id)
            .where(NewsletterIssue.status == "approved")
            .where(NewsletterIssue.scheduled_send_at.is_not(None))
            .where(NewsletterIssue.scheduled_send_at <= now)
        )
    ).scalars().all()

    dispatched = 0
    failed = 0
    for issue_id in due_ids:
        try:
            await _dispatch_issue(session, issue_id)
            dispatched += 1
        except Exception:  # noqa: BLE001
            logger.exception("Dispatch failed for newsletter issue %s", issue_id)
            failed += 1
    return {"dispatched": dispatched, "failed": failed}


async def _dispatch_issue(session: AsyncSession, issue_id: str) -> NewsletterIssue:
    """Internal: do the actual send-loop for one approved issue.

    Atomic transition: status approved -> sent. Two concurrent dispatcher
    runs cannot both fire the send loop because the WHERE clause matches
    only on status=approved; whichever UPDATE lands first flips status
    to sent, the other sees zero affected rows and bails early.
    """
    from sqlalchemy import update

    now = _now()
    result = await session.execute(
        update(NewsletterIssue)
        .where(NewsletterIssue.id == issue_id)
        .where(NewsletterIssue.status == "approved")
        .values(status="sending", updated_at=now)
    )
    affected = result.rowcount if result.rowcount is not None else 0
    await session.commit()
    if affected == 0:
        # Another dispatcher already advanced this issue. Read current
        # state and return; the original caller can decide what to do.
        existing = (
            await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
        ).scalar_one_or_none()
        if existing is None:
            raise LookupError(f"Newsletter issue vanished: {issue_id}")
        return existing

    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one()

    # Snapshot the live tagged finds into finds_json so the historical
    # archive is preserved. The Compose view reads live tagged finds
    # while the issue is open; once we transition to sending, the
    # snapshot becomes immutable. Backwards-compat: if no finds are
    # tagged via newsletter_issue_id (legacy data still on
    # newsletter_pending), fall through to whatever finds_json the
    # composer originally wrote.
    live_finds = await live_finds_for_issue(session, issue.id)
    if live_finds:
        issue.finds_json = json.dumps(live_finds, ensure_ascii=False, default=str)
        await session.commit()

    # Assign the public number before external side effects. If another
    # issue wins the same MAX+1 race, the retry happens before any email or
    # public delivery rows are emitted.
    try:
        issue = await _assign_ship_number_with_retry(session, issue.id)
    except Exception:
        await session.rollback()
        try:
            failed_issue = (
                await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
            ).scalar_one_or_none()
            if failed_issue is not None and failed_issue.status == "sending":
                failed_issue.status = "awaiting_approval"
                failed_issue.scheduled_send_at = None
                failed_issue.updated_at = _now()
                failed_issue.notes = (
                    failed_issue.notes + "\nDispatch halted before external sends; operator review required."
                ).strip()
                session.add(failed_issue)
                await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()
            logger.exception("Could not revert newsletter issue %s after dispatch pre-send failure", issue_id)
        raise

    # Email send loop. Each subscriber gets their personalized unsubscribe
    # link so a single click pulls just that address off the list.
    subscribers = await active_subscribers(session)
    sender = os.getenv("MS365_SEND_AS", "no-reply@example.com")
    sent = 0
    failed = 0
    for sub in subscribers:
        html = render_issue_html(issue, unsubscribe_url=_unsubscribe_url(sub.unsubscribe_token))
        delivery = NewsletterDelivery(
            issue_id=issue.id,
            subscriber_id=sub.id,
            channel="email",
            status="pending",
        )
        session.add(delivery)
        try:
            status_value = await send_internal_email(
                issue.subject or _newsletter_label(),
                html,
                recipient=sub.email,
                sender=sender,
            )
            delivery.status = status_value
            if status_value == "sent":
                delivery.sent_at = _now()
                sent += 1
            else:
                # Skipped (no MS365 config) is not a failure; treat the
                # delivery row as pending so a future retry can pick it up.
                pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("Newsletter send failed for %s on issue %s", sub.email, issue.id)
            delivery.status = "failed"
            delivery.error = str(exc)[:512]
            failed += 1
        session.add(delivery)
    await session.commit()

    # Web delivery record for the public /library/latest/<slug> publish.
    session.add(
        NewsletterDelivery(
            issue_id=issue.id,
            channel="web",
            status="sent",
            sent_at=_now(),
        )
    )

    # LinkedIn delivery. Skipped silently when not configured; in that
    # case the admin panel's Copy for LinkedIn button is the operating
    # mechanism and the operator pastes manually.
    linkedin_result = await _post_to_linkedin(render_issue_linkedin(issue))
    session.add(
        NewsletterDelivery(
            issue_id=issue.id,
            channel="linkedin",
            status=linkedin_result.status,
            sent_at=_now() if linkedin_result.status == "sent" else None,
            error=linkedin_result.reason if linkedin_result.status == "failed" else "",
        )
    )

    # Stamp published_in_newsletter_at on every find tagged to this
    # issue. Uses the live FK (newsletter_issue_id) as the source of
    # truth so untagged-just-before-send finds are correctly excluded
    # and so we catch any drift between the snapshot and the live
    # state. Falls back to the finds_json snapshot for legacy issues
    # whose finds were never tagged via the FK (the catch-up draft
    # migration covers most of these, but the union handles edge
    # cases).
    publish_ts = _now()
    tagged = (
        await session.execute(
            select(DiscoveryFind).where(DiscoveryFind.newsletter_issue_id == issue.id)
        )
    ).scalars().all()
    for find in tagged:
        find.newsletter_pending = False
        find.published_in_newsletter_at = publish_ts
        # Keep the FK pointing at the issue so the historical record
        # ("what was in issue #5?") survives. Don't clear it on send.

    if not tagged:
        # Legacy fallback: read finds_json and update by id. This path
        # only runs for issues that pre-date the per-issue assignment
        # model and never had newsletter_issue_id wired.
        finds = json.loads(issue.finds_json) if issue.finds_json else []
        if finds:
            find_ids = [f.get("id") for f in finds if f.get("id")]
            if find_ids:
                updated = await session.execute(
                    select(DiscoveryFind).where(DiscoveryFind.id.in_(find_ids))
                )
                for find in updated.scalars():
                    find.newsletter_pending = False
                    find.published_in_newsletter_at = publish_ts

    issue.status = "sent"
    issue.sent_at = _now()
    issue.updated_at = _now()
    # Cap notes growth so an issue that gets manually re-sent many times
    # doesn't accumulate kilobytes of dispatch stats. Latest stats stay
    # at the bottom; the head of the notes gets truncated with a marker.
    new_note = f"\n[{_now().isoformat()}] sent={sent} failed={failed}"
    appended = (issue.notes + new_note).strip()
    if len(appended) > 4000:
        appended = "...[earlier notes truncated]...\n" + appended[-3900:]
    issue.notes = appended
    session.add(issue)
    await session.commit()
    await session.refresh(issue)
    return issue


async def approve_and_distribute(
    session: AsyncSession,
    issue_id: str,
    *,
    approved_by: str,
) -> NewsletterIssue:
    """Backwards-compat shim. Older test paths and any caller that
    expected the original immediate-send semantics call this. It now
    schedules with a near-immediate send time (1 second in the future)
    so the dispatcher fires synchronously below. The result matches the
    pre-refactor behavior: status -> sent, deliveries written, finds
    flagged. New code should call approve_and_schedule + the dispatcher
    separately, which is what the admin endpoint does.
    """
    immediate = _now() - timedelta(seconds=1)
    await approve_and_schedule(
        session, issue_id, approved_by=approved_by, scheduled_send_at=immediate,
    )
    # The schedule helper bumps a past timestamp to now+30min by default,
    # so we explicitly overwrite scheduled_send_at to "in the past" so
    # the dispatcher picks it up immediately.
    from sqlalchemy import update
    await session.execute(
        update(NewsletterIssue)
        .where(NewsletterIssue.id == issue_id)
        .values(scheduled_send_at=immediate)
    )
    await session.commit()
    return await _dispatch_issue(session, issue_id)


# ---------------------------------------------------------------------------
# Listing / detail helpers for the admin surface
# ---------------------------------------------------------------------------


async def list_issues(
    session: AsyncSession,
    *,
    limit: int = 50,
    include_unpublished: bool = True,
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """List newsletter issues.

    Two filter modes:
    - include_unpublished=False hides rows with unpublished_at set.
      Used by the public /library/issues.json feed so soft-deleted
      issues disappear from the archive archive.
    - public_only=True additionally restricts to status=sent so
      drafts and skipped placeholders never leak to public surfaces.

    Admin callers leave both at their defaults and see everything.
    """
    statement = select(NewsletterIssue).order_by(NewsletterIssue.created_at.desc())
    if not include_unpublished:
        statement = statement.where(NewsletterIssue.unpublished_at.is_(None))
    if public_only:
        statement = statement.where(NewsletterIssue.status == "sent")
    statement = statement.limit(max(1, min(limit, 500)))
    rows = (await session.execute(statement)).scalars().all()
    return [_serialize_issue(row) for row in rows]


async def get_issue_detail(session: AsyncSession, issue_id: str) -> dict[str, Any] | None:
    issue = (
        await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))
    ).scalar_one_or_none()
    if issue is None:
        return None
    serialized = _serialize_issue(issue)

    # Open issues (draft / awaiting_approval / approved-not-yet-sent)
    # read the live list of tagged finds so the Compose view stays in
    # sync as the operator tags and untags. Closed issues (sent,
    # skipped) read finds_json because that's the immutable historical
    # snapshot taken at send time.
    #
    # Legacy fallback: an issue composed before the per-issue model
    # rollout will have its items in finds_json but NOT tagged via
    # newsletter_issue_id. Fall through to finds_json when the live
    # query returns empty and the snapshot is populated.
    if issue.status in _OPEN_ISSUE_STATUSES:
        live = await live_finds_for_issue(session, issue.id)
        if not live and issue.finds_json and issue.finds_json != "[]":
            serialized["finds"] = json.loads(issue.finds_json)
            serialized["find_count"] = len(serialized["finds"])
        else:
            serialized["finds"] = live
            serialized["find_count"] = len(live)
    else:
        serialized["finds"] = json.loads(issue.finds_json) if issue.finds_json else []

    serialized["editorial_markdown"] = issue.editorial_markdown
    # Use a placeholder unsubscribe link for the preview so the operator
    # sees the layout without targeting a real subscriber.
    preview_finds_json = json.dumps(serialized["finds"], ensure_ascii=False, default=str)
    serialized["html_preview"] = render_issue_html(
        _preview_issue_with_finds_json(issue, preview_finds_json),
        unsubscribe_url=f"{_site_url()}/newsletter/unsubscribe?token=preview",
    )
    serialized["plain_preview"] = render_issue_plain(
        issue,
        unsubscribe_url=f"{_site_url()}/newsletter/unsubscribe?token=preview",
    )
    serialized["linkedin_post"] = render_issue_linkedin(issue)
    return serialized


# ---------------------------------------------------------------------------
# Scheduler (Phase 6): every-10-days cadence with operator approval email
# ---------------------------------------------------------------------------


async def run_scheduled_cycle(session: AsyncSession) -> dict[str, Any]:
    """Daily newsletter tick.

    Two things happen here, in order:

    1. **Approval reminder pass.** For every draft whose target_send_at
       is within the next ~24 hours and that hasn't had its reminder
       email sent yet, fire the two-link approval email and stamp
       approval_email_sent_at. The de-dupe stamp keeps the daily cron
       from spamming the operator if they don't act immediately.

    2. **Auto-pacing pass.** If no upcoming open draft / awaiting-
       approval issue exists AND the last sent cycle's target_send_at
       was more than CYCLE_DAYS - 1 ago, create a new draft anchored
       to last.target_send_at + 10 days, auto-tag eligible auto_indexed
       finds onto it, and fire the reminder if the new target is
       within the 24-hour window.

    The cron path no longer "composes" anything — drafts exist as
    containers and items get tagged in by the auto-pacing step or by
    the operator directly. The Compose verb is the editorial-writing
    step inside the admin Compose view.

    Returns a summary dict for the workflow log.
    """
    now = _now()
    summary: dict[str, Any] = {
        "action": "tick",
        "reminders_sent": 0,
        "drafts_created": 0,
        "draft_skipped_reason": "",
    }

    # ----- Approval reminder pass -----
    reminder_window = now + timedelta(hours=24)
    candidates = (
        await session.execute(
            select(NewsletterIssue)
            .where(NewsletterIssue.status == "draft")
            .where(NewsletterIssue.target_send_at.is_not(None))
            .where(NewsletterIssue.target_send_at <= reminder_window)
            .where(NewsletterIssue.approval_email_sent_at.is_(None))
        )
    ).scalars().all()
    for issue in candidates:
        try:
            status_value = await _send_approval_notification(issue)
            issue.approval_email_sent_at = _now()
            issue.notes = (
                issue.notes
                + f"\n[{_now().isoformat()}] approval reminder email status={status_value}"
            ).strip()
            session.add(issue)
            summary["reminders_sent"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Approval reminder failed for issue %s", issue.id)
            summary["draft_skipped_reason"] = f"reminder_error:{type(exc).__name__}"
    if candidates:
        await session.commit()

    # ----- Auto-pacing pass -----
    upcoming = (
        await session.execute(
            select(NewsletterIssue)
            .where(NewsletterIssue.status.in_(["draft", "awaiting_approval"]))
            .limit(1)
        )
    ).scalar_one_or_none()
    if upcoming is not None:
        summary["draft_skipped_reason"] = (
            summary["draft_skipped_reason"] or "upcoming_issue_exists"
        )
        summary["upcoming_issue_id"] = upcoming.id
        return summary

    last = (
        await session.execute(
            select(NewsletterIssue)
            .where(NewsletterIssue.status.in_(["sent", "skipped"]))
            .order_by(NewsletterIssue.sent_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None:
        anchor = (
            last.target_send_at
            or last.scheduled_send_at
            or last.sent_at
            or last.created_at
        )
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        days_since = (now - anchor).total_seconds() / 86400
        if days_since < CYCLE_DAYS - 1:
            summary["draft_skipped_reason"] = "cycle_window_not_elapsed"
            summary["days_since_last"] = round(days_since, 2)
            return summary

    new_issue = await create_empty_issue(session)
    tagged = await auto_populate_draft(session, new_issue.id)
    summary["drafts_created"] = 1
    summary["new_issue_id"] = new_issue.id
    summary["new_issue_display_label"] = new_issue.display_label
    summary["new_issue_target_send_at"] = (
        new_issue.target_send_at.isoformat() if new_issue.target_send_at else None
    )
    summary["auto_tagged_finds"] = tagged

    # If the new draft's target is already within the 24h reminder
    # window (e.g. the operator was overdue and the cycle math lands
    # the next target inside a day), fire the reminder immediately so
    # they have time to react. SQLite drops tzinfo on roundtrip even
    # when we store aware datetimes; normalize to UTC-aware before
    # comparing.
    new_target = new_issue.target_send_at
    if new_target is not None and new_target.tzinfo is None:
        new_target = new_target.replace(tzinfo=timezone.utc)
    if new_target is not None and new_target <= now + timedelta(hours=24):
        try:
            status_value = await _send_approval_notification(new_issue)
            new_issue.approval_email_sent_at = _now()
            new_issue.notes = (
                new_issue.notes
                + f"\n[{_now().isoformat()}] approval reminder email status={status_value}"
            ).strip()
            session.add(new_issue)
            await session.commit()
            summary["reminders_sent"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Initial reminder failed for new issue %s", new_issue.id)
            summary["draft_skipped_reason"] = f"reminder_error:{type(exc).__name__}"

    return summary


# Approval token: short-lived HMAC over issue_id + expiry. Lives in the
# email body so the operator can one-click approve from their inbox.
# Signed with the server-side ADMIN_POLICY_TOKEN (the same secret the
# admin UI uses); no separate signing key to manage.
#
# Format: base64url(issue_id).base64url(expiry_unix).base64url(hmac_hex)
# Expiry default: 7 days. Long enough to survive an operator who clears
# their inbox once a week, short enough that a leaked email link is not
# a forever-valid bypass of the admin token.
APPROVAL_TOKEN_TTL = timedelta(days=7)


def _approval_signing_key() -> bytes:
    """Return the signing key as bytes. Defaults to the admin token; falls
    back to a fixed dev marker so unit tests don't need env setup. In prod
    the ADMIN_POLICY_TOKEN env var is always set."""
    secret = os.getenv("ADMIN_POLICY_TOKEN", "") or "dev-approval-key"
    return secret.encode("utf-8")


def _b64url(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def make_approval_token(issue_id: str, *, ttl: timedelta = APPROVAL_TOKEN_TTL) -> str:
    """Sign an approval token tied to one issue id and an expiry."""
    expiry = int((_now() + ttl).timestamp())
    payload = f"{_b64url(issue_id)}.{_b64url(str(expiry))}"
    mac = hmac.new(_approval_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{_b64url(mac)}"


def verify_approval_token(token: str) -> str:
    """Verify the token and return the issue_id. Raises ValueError on any
    failure (bad shape, expired, bad signature)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed approval token")
    try:
        issue_id = _b64url_decode(parts[0]).decode("utf-8")
        expiry = int(_b64url_decode(parts[1]).decode("utf-8"))
        provided_mac = _b64url_decode(parts[2]).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Approval token decode failed") from exc

    expected_mac = hmac.new(
        _approval_signing_key(),
        f"{parts[0]}.{parts[1]}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, provided_mac):
        raise ValueError("Approval token signature mismatch")
    if expiry < int(_now().timestamp()):
        raise ValueError("Approval token expired")
    return issue_id


async def _send_approval_notification(issue: NewsletterIssue) -> str:
    """Email the configured approver(s) that a draft is awaiting approval.

    Two-link template:
    1. Review & edit -> /admin/newsletter (the full panel; the operator
       picks subject, editorial, schedule)
    2. One-click approve -> /admin/api/newsletter/approve-via-link with a
       signed token. Schedules the send with the default 30-minute
       delay, giving a cancel window if the operator clicked too fast.
    """
    recipients_raw = os.getenv("NEWSLETTER_APPROVAL_TO", "")
    recipients = [addr.strip() for addr in recipients_raw.split(",") if addr.strip()]
    if not recipients:
        return "skipped_no_recipient"

    site = _site_url()
    review_link = f"{site}/admin/newsletter"
    token = make_approval_token(issue.id)
    approve_link = f"{site}/admin/api/newsletter/approve-via-link?token={token}"

    safe_subject = html.escape(issue.subject or issue.slug, quote=False)
    issue_label = f"Issue {issue.display_label}" if issue.display_label else issue.slug
    subject = f"{issue_label} awaiting approval: {issue.subject or issue.slug}"
    target_label = (
        issue.target_send_at.strftime("%a %b %d, %Y at %H:%M UTC")
        if issue.target_send_at
        else "TBD"
    )
    body = (
        f"<p>{html.escape(issue_label, quote=False)} is ready for review.</p>"
        f"<p><strong>Subject:</strong> {safe_subject}</p>"
        f"<p><strong>Target send:</strong> {html.escape(target_label, quote=False)}</p>"
        f"<p><strong>Cycle window:</strong> {issue.period_start.strftime('%b %d')} - {issue.period_end.strftime('%b %d, %Y')}</p>"
        f"<p><strong>Find count:</strong> {len(json.loads(issue.finds_json) or [])}</p>"
        f"<p style=\"margin-top:24px\">"
        f"<a href=\"{html.escape(review_link, quote=True)}\" "
        f"style=\"display:inline-block;padding:10px 16px;background:#D4BF91;color:#242128;"
        f"text-decoration:none;border-radius:8px;font-weight:600\">Review &amp; edit</a>"
        f"&nbsp;&nbsp;"
        f"<a href=\"{html.escape(approve_link, quote=True)}\" "
        f"style=\"display:inline-block;padding:10px 16px;border:1px solid #D4BF91;"
        f"color:#D4BF91;text-decoration:none;border-radius:8px;font-weight:600\">"
        f"Approve with default 30-min delay</a>"
        f"</p>"
        f"<p style=\"color:#666;font-size:13px\">The one-click approval schedules the send "
        f"for 30 minutes from now so you can still pull it back via "
        f"<a href=\"{html.escape(review_link, quote=True)}\">/admin/newsletter</a> "
        f"if you change your mind. The token in the link is signed and expires in 7 days.</p>"
        f"<p style=\"color:#666;font-size:13px\">If you do nothing, the draft will continue to wait. "
        f"The next scheduler pass will not auto-compose another draft until this one is approved or rejected.</p>"
    )

    # Send one email per recipient so a single failure doesn't take down
    # the batch. We aggregate the worst status for the return value.
    statuses: list[str] = []
    for recipient in recipients:
        try:
            status = await send_internal_email(subject, body, recipient=recipient)
            statuses.append(status)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send approval notification to %s", recipient)
            statuses.append(f"failed:{type(exc).__name__}")

    if all(s == "sent" for s in statuses):
        return "sent"
    if any(s == "sent" for s in statuses):
        return "partial"
    return statuses[0] if statuses else "skipped_no_recipient"


def _serialize_issue(issue: NewsletterIssue) -> dict[str, Any]:
    return {
        "id": issue.id,
        # Sticky display label ("0a", "1c", "3"). Read this in UI / email
        # / archive code. ship_number is exposed separately for callers
        # that need to know "did this actually ship and which number".
        "display_label": issue.display_label,
        "ship_number": issue.ship_number,
        "slug": issue.slug,
        "subject": issue.subject,
        "status": issue.status,
        "period_start": issue.period_start.isoformat(),
        "period_end": issue.period_end.isoformat(),
        "approved_by": issue.approved_by,
        "approved_at": issue.approved_at.isoformat() if issue.approved_at else None,
        "target_send_at": issue.target_send_at.isoformat() if issue.target_send_at else None,
        "approval_email_sent_at": (
            issue.approval_email_sent_at.isoformat() if issue.approval_email_sent_at else None
        ),
        "scheduled_send_at": issue.scheduled_send_at.isoformat() if issue.scheduled_send_at else None,
        "sent_at": issue.sent_at.isoformat() if issue.sent_at else None,
        "unpublished_at": issue.unpublished_at.isoformat() if issue.unpublished_at else None,
        "created_at": issue.created_at.isoformat(),
        "updated_at": issue.updated_at.isoformat(),
        "notes": issue.notes,
        "find_count": len(json.loads(issue.finds_json)) if issue.finds_json else 0,
    }
