"""Token-gated newsletter admin endpoints.

GET /admin/api/newsletter/issues
GET /admin/api/newsletter/issues/{id}
POST /admin/api/newsletter/draft         - compose from pending finds
POST /admin/api/newsletter/issues/{id}/editorial - save editorial / subject
POST /admin/api/newsletter/issues/{id}/approve   - approve + schedule send
POST /admin/api/newsletter/issues/{id}/cancel-send - pull a scheduled send back
POST /admin/api/newsletter/dispatch-due  - dispatch any due scheduled issues

Approval is the operator's final gate. Auto-pacing (every-10-days
cron via Phase 6) calls compose_pending_issue() and emails the
operator an approval link; the operator finalizes the editorial here
and clicks Approve to schedule. Send fires when the dispatcher picks
up the issue at the scheduled time.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.services import require_admin_token
from core.db import get_session
from core.newsletter import (
    approve_and_schedule,
    cancel_scheduled_send,
    compose_pending_issue,
    create_empty_issue,
    dispatch_scheduled_issues,
    get_issue_detail,
    list_assignable_issues,
    list_issues,
    next_default_target_send_at,
    run_scheduled_cycle,
    update_editorial,
    verify_approval_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api/newsletter", tags=["admin"])


@router.get("/issues")
async def admin_newsletter_list(
    limit: int = 50,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    issues = await list_issues(session, limit=limit)
    return {"issues": issues}


@router.get("/issues/{issue_id}")
async def admin_newsletter_detail(
    issue_id: str,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    detail = await get_issue_detail(session, issue_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Newsletter issue not found")
    return {"issue": detail}


class DraftRequest(BaseModel):
    # Force=True composes an issue even when no finds are newsletter_pending.
    # Useful when the operator wants to send an editorial-only issue.
    force: bool = False


@router.post("/draft")
async def admin_newsletter_draft(
    request_body: DraftRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Compose a fresh draft from current newsletter_pending finds.

    Returns the new issue. If no finds are pending and force=False, the
    cycle is recorded as skipped and the response reflects that so the
    caller can show 'no issue this cycle' instead of an approval link.
    """
    result = await compose_pending_issue(session, force=request_body.force)
    detail = await get_issue_detail(session, result.issue.id)
    return {"issue": detail, "created": result.created}


class EditorialRequest(BaseModel):
    editorial_markdown: str = Field(default="", max_length=20000)
    subject: str | None = Field(default=None, max_length=200)


@router.post("/issues/{issue_id}/editorial")
async def admin_newsletter_save_editorial(
    issue_id: str,
    request_body: EditorialRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    try:
        await update_editorial(
            session,
            issue_id,
            editorial_markdown=request_body.editorial_markdown,
            subject=request_body.subject,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    detail = await get_issue_detail(session, issue_id)
    return {"issue": detail}


@router.post("/run-cycle")
async def admin_newsletter_run_cycle(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Phase 6 scheduler entry point.

    Idempotent. The GitHub Actions newsletter workflow hits this on a
    daily cron; it composes a new draft only when the 10-day window has
    elapsed AND there is no awaiting_approval draft sitting unreviewed.
    """
    summary = await run_scheduled_cycle(session)
    return {"summary": summary}


class ApprovalRequest(BaseModel):
    """Approval body.

    The actor identity is derived from the admin token (server-side,
    single actor) rather than from the request body so the audit trail
    cannot be forged by anyone with a valid token who chooses to claim
    a different name on the wire.

    scheduled_send_at: optional future ISO-8601 timestamp. When omitted
    or set to a past time, the issue is scheduled to fire now + 30
    minutes (DEFAULT_SCHEDULE_DELAY in core/newsletter.py), giving the
    operator a cancel window for second thoughts.
    """

    scheduled_send_at: datetime | None = Field(default=None)

    @field_validator("scheduled_send_at")
    @classmethod
    def _no_far_future(cls, value: datetime | None) -> datetime | None:
        # Bound the schedule to one year out — a typo of "year 2099"
        # should be rejected at the boundary rather than silently
        # parked indefinitely.
        if value is None:
            return None
        from datetime import datetime as _dt, timedelta, timezone
        max_future = _dt.now(timezone.utc) + timedelta(days=365)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value > max_future:
            raise ValueError("scheduled_send_at cannot be more than one year in the future")
        return value


@router.post("/issues/{issue_id}/approve")
async def admin_newsletter_approve(
    issue_id: str,
    request_body: ApprovalRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    # approved_by is fixed server-side. We have one operator concept
    # ('admin') today; the token is the actor. If a future change
    # introduces named admins, derive this from the token claims.
    try:
        issue = await approve_and_schedule(
            session,
            issue_id,
            approved_by="admin",
            scheduled_send_at=request_body.scheduled_send_at,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "issue": {
            "id": issue.id,
            "status": issue.status,
            "scheduled_send_at": issue.scheduled_send_at.isoformat() if issue.scheduled_send_at else None,
            "sent_at": issue.sent_at.isoformat() if issue.sent_at else None,
        }
    }


class CancelSendRequest(BaseModel):
    """Empty body, reserved for future flags (reason, etc.). Posting
    `{}` is fine."""
    pass


@router.post("/issues/{issue_id}/cancel-send")
async def admin_newsletter_cancel_send(
    issue_id: str,
    request_body: CancelSendRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Pull a scheduled-but-not-yet-sent issue back to awaiting_approval.

    Valid only while status=approved and the scheduled send time is in
    the future. Returns 409 once the dispatcher has fired the send loop.
    """
    _ = request_body
    try:
        issue = await cancel_scheduled_send(
            session,
            issue_id,
            cancelled_by="admin",
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "issue": {
            "id": issue.id,
            "status": issue.status,
            "scheduled_send_at": issue.scheduled_send_at.isoformat() if issue.scheduled_send_at else None,
        }
    }


@router.post("/dispatch-due")
async def admin_newsletter_dispatch_due(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Fire the dispatcher: send every approved issue whose scheduled
    time has passed. Called by the hourly discovery cron AND manually
    from the admin panel as a "send now" affordance after approval.

    Idempotent: the dispatcher's atomic status flip prevents a second
    call from re-sending an issue."""
    summary = await dispatch_scheduled_issues(session)
    return {"summary": summary}


# ---------------------------------------------------------------------------
# Compose-Issue-#N model: per-issue helpers for the admin two-pane UI
# ---------------------------------------------------------------------------


class CreateIssueRequest(BaseModel):
    """New empty issue. target_send_at is optional; the helper defaults
    to last_issue.target_send_at + 10 days when omitted. Subject is
    optional and defaults to "Issue #N" until the operator edits it."""
    target_send_at: datetime | None = Field(default=None)
    subject: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2048)

    @field_validator("target_send_at")
    @classmethod
    def _no_far_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        from datetime import datetime as _dt, timedelta, timezone as _tz
        max_future = _dt.now(_tz.utc) + timedelta(days=365)
        if value.tzinfo is None:
            value = value.replace(tzinfo=_tz.utc)
        if value > max_future:
            raise ValueError("target_send_at cannot be more than one year in the future")
        return value


@router.post("/issues/new", status_code=201)
async def admin_newsletter_create_empty_issue(
    request_body: CreateIssueRequest,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Create a new draft with a sticky display_label.

    Used by the "+ New issue" button at the top of the admin newsletter
    panel's left rail. The returned issue is empty; the operator tags
    finds onto it from /admin/discovery or from inside the Compose view.
    No ship_number is assigned yet -- that happens at successful send.
    """
    issue = await create_empty_issue(
        session,
        target_send_at=request_body.target_send_at,
        subject=request_body.subject,
        notes=request_body.notes,
    )
    detail = await get_issue_detail(session, issue.id)
    return {"issue": detail}


@router.delete("/issues/{issue_id}", status_code=200)
async def admin_newsletter_delete_issue(
    issue_id: str,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Hard-delete a newsletter issue.

    Works on any status. Reverts tagged finds to neutral
    (newsletter_pending=false, FK cleared, published_in_newsletter_at
    cleared) and purges NewsletterDelivery rows for the issue. The
    issue row is removed from the DB. Confirmation lives in the UI
    layer; this endpoint trusts that the caller meant it.
    """
    from core.newsletter import delete_issue

    try:
        summary = await delete_issue(session, issue_id, deleted_by="admin_panel")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": summary}


@router.post("/issues/{issue_id}/unpublish", status_code=200)
async def admin_newsletter_unpublish_issue(
    issue_id: str,
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Soft-hide a sent or scheduled issue from public surfaces.

    Stamps unpublished_at = now. For scheduled (status=approved with
    future scheduled_send_at), also cancels the schedule and reverts
    status to awaiting_approval. Drafts return 409: use the delete
    endpoint instead.
    """
    from core.newsletter import unpublish_issue

    try:
        issue = await unpublish_issue(session, issue_id, unpublished_by="admin_panel")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    detail = await get_issue_detail(session, issue.id)
    return {"issue": detail}


@router.get("/assignable-issues")
async def admin_newsletter_assignable_issues(
    _: None = Depends(require_admin_token),
    session: AsyncSession = Depends(get_session),
):
    """Issues the operator can still tag finds onto.

    Powers the per-find dropdown on /admin/discovery rows. Returns
    drafts, awaiting_approval issues, and approved-but-not-yet-sent
    issues. Excludes sent / skipped issues (historical, immutable).
    """
    issues = await list_assignable_issues(session)
    payload = [
        {
            "id": issue.id,
            "display_label": issue.display_label,
            "ship_number": issue.ship_number,
            "subject": issue.subject,
            "status": issue.status,
            "target_send_at": (
                issue.target_send_at.isoformat() if issue.target_send_at else None
            ),
            "scheduled_send_at": (
                issue.scheduled_send_at.isoformat() if issue.scheduled_send_at else None
            ),
        }
        for issue in issues
    ]
    suggested = await next_default_target_send_at(session)
    return {
        "issues": payload,
        "next_default_target_send_at": suggested.isoformat(),
    }


# ---------------------------------------------------------------------------
# One-click approve from the approval-needed email.
#
# The approval email contains two links: a Review link to /admin/newsletter
# and an Approve link that hits this GET endpoint with a signed token. The
# token is HMAC'd over (issue_id, expiry) with the admin secret, so this
# endpoint can validate without an X-Admin-Token header on the request.
# That's required because the click comes from a mail client, not the
# admin SPA.
#
# Security model:
# - The token is short-lived (7 days) and bound to one issue id.
# - Successful approval schedules the send for now+30min, giving the
#   operator a cancel window via the admin panel.
# - The endpoint never returns the token in the response body, never
#   logs it, and always renders text/html so it cannot be mistaken for
#   a JSON API.
# ---------------------------------------------------------------------------


def _approval_response(*, ok: bool, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    site_link = "/admin/newsletter"
    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title, quote=False)}</title>"
        "<meta name=\"robots\" content=\"noindex\">"
        "<style>body{font-family:system-ui,sans-serif;background:#242128;color:#F5F5F5;"
        "padding:48px 24px;max-width:640px;margin:0 auto}"
        "h1{font-weight:600;color:#D4BF91}a{color:#D4BF91}"
        ".ok{border-left:4px solid #566B5B;padding-left:16px}"
        ".err{border-left:4px solid #b94a4a;padding-left:16px}</style>"
        "</head><body>"
        f"<h1>{html.escape(title, quote=False)}</h1>"
        f"<div class=\"{'ok' if ok else 'err'}\">{body}</div>"
        f"<p><a href=\"{site_link}\">Open the newsletter admin panel</a></p>"
        "</body></html>"
    )
    return HTMLResponse(content=page, status_code=status_code)


@router.get("/approve-via-link", include_in_schema=False)
async def admin_newsletter_approve_via_link(
    token: str = Query(..., min_length=20, max_length=512),
    session: AsyncSession = Depends(get_session),
):
    """Verify a signed approval token from the email and schedule the send.

    Renders an HTML status page rather than returning JSON so a mail
    client following the link lands somewhere readable. Defaults to the
    30-minute schedule delay, matching the in-app "Approve (default
    delay)" button.
    """
    try:
        issue_id = verify_approval_token(token)
    except ValueError as exc:
        logger.info("Approval token rejected: %s", exc)
        return _approval_response(
            ok=False,
            title="Approval link invalid",
            body=(
                "<p>This approval link could not be verified. It may have "
                "expired (links are valid for 7 days), been tampered with, "
                "or already been replaced by a newer draft.</p>"
                "<p>Open the admin panel below to review and approve the "
                "current draft manually.</p>"
            ),
            status_code=400,
        )

    try:
        issue = await approve_and_schedule(
            session,
            issue_id,
            approved_by="email_link",
            scheduled_send_at=None,
        )
    except LookupError:
        return _approval_response(
            ok=False,
            title="Issue not found",
            body="<p>The newsletter issue this link referred to no longer exists.</p>",
            status_code=404,
        )
    except ValueError as exc:
        return _approval_response(
            ok=False,
            title="Approval not accepted",
            body=(
                f"<p>The newsletter issue could not be approved: "
                f"{html.escape(str(exc), quote=False)}.</p>"
                "<p>This usually means it was already approved or sent. "
                "Use the admin panel to confirm the current state.</p>"
            ),
            status_code=409,
        )

    scheduled = (
        issue.scheduled_send_at.strftime("%Y-%m-%d %H:%M UTC")
        if issue.scheduled_send_at
        else "shortly"
    )
    return _approval_response(
        ok=True,
        title="Approved",
        body=(
            "<p>The newsletter issue has been approved and is scheduled "
            f"to send at <strong>{html.escape(scheduled, quote=False)}</strong>.</p>"
            "<p>If you changed your mind, open the admin panel below and "
            "click <em>Cancel scheduled send</em> before the dispatcher fires.</p>"
        ),
    )
