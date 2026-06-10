"""Public newsletter endpoints: signup + unsubscribe.

This module is intentionally separate from gestaltworkframe.api/admin_newsletter.py
(token-gated approval/distribution). Everything here must be safe for
an unauthenticated visitor to reach: rate-limited, no internal state
leakage, and tokens used only for the bearer to act on their own row.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gestaltworkframe.api.request_helpers import client_ip
from gestaltworkframe.core.db import ContactRecord, get_session
from gestaltworkframe.core.subscribers import ROLE_TOPICS, subscribe_and_reply, unsubscribe_by_token

router = APIRouter(prefix="/newsletter", tags=["newsletter"])
logger = logging.getLogger(__name__)


_UNSUB_CONFIRM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Unsubscribed</title>
<meta name="robots" content="noindex"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #242128; color: #F5F5F5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    line-height: 1.6; }}
  main {{ max-width: 520px; padding: 32px; }}
  h1 {{ font-family: "Rajdhani", sans-serif; font-weight: 600; font-size: 32px;
    margin: 0 0 16px; color: #D4BF91; }}
  p {{ margin: 0 0 16px; color: rgba(245,245,245,.78); }}
  a {{ color: #D4BF91; }}
  a:hover {{ color: #DCD077; }}
</style></head>
<body>
<main>
  <h1>{heading}</h1>
  <p>{body}</p>
  <p><a href="{home_url}">Return to site</a></p>
</main>
</body></html>"""


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IP_WINDOW = timedelta(hours=24)
_IP_LIMIT = 10  # signups per 24h per IP. Higher than contact form (5)
                # because newsletter signup is lower-trust / lower-effort
                # so casual repeats / fixed typos are more common.

VALID_ROLES = frozenset({"student", "automation_engineer", "interested_party"})


def _clean(text: str, max_len: int) -> str:
    cleaned = _CONTROL_RE.sub("", text).strip()
    return cleaned[:max_len]


class NewsletterSignupPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320)
    company: str = Field(default="", max_length=200)
    role: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        email = value.strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError("Invalid email address")
        return email

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        if value not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
        return value

    @field_validator("name", "company")
    @classmethod
    def _strip_control(cls, value: str) -> str:
        return _CONTROL_RE.sub("", value).strip()


async def _enforce_signup_ip_rate_limit(session: AsyncSession, ip_address: str) -> None:
    """IP-rate-limit window for newsletter signups, scoped to newsletter
    rows only.

    Previously this counted EVERY ContactRecord with the IP in the
    window, which penalized visitors who had already submitted the
    detailed contact form. The signup_source marker is set explicitly
    when the newsletter endpoint creates the row, so we can narrow the
    query to just that subset and stop double-counting.

    SQLite LIKE is case-sensitive on ASCII for the substring we care
    about, and the marker is a fixed JSON fragment we emit ourselves.
    """
    since = datetime.now(timezone.utc) - _IP_WINDOW
    result = await session.execute(
        select(func.count()).select_from(ContactRecord).where(
            ContactRecord.ip_address == ip_address,
            ContactRecord.created_at >= since,
            ContactRecord.data.like('%"signup_source": "newsletter"%'),
        )
    )
    count = int(result.scalar_one() or 0)
    if count >= _IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many signups from this IP in the last 24h. Try again later.",
        )


@router.post("/api/subscribe", status_code=201)
async def newsletter_subscribe(
    request: Request,
    payload: NewsletterSignupPayload,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Lightweight newsletter signup: name, email, company, role.

    Creates a ContactRecord (so newsletter signups show up in the same
    operator audit stream as the detailed contact form submissions),
    upserts a Subscriber, and sends the role-appropriate auto-reply.
    All three steps run through the same core.subscribers.subscribe_and_reply
    helper the /contact form uses; no duplicate path.
    """
    import json as _json

    ip_address = client_ip(request)
    await _enforce_signup_ip_rate_limit(session, ip_address)

    name = _clean(payload.name, 200)
    company = _clean(payload.company, 200)
    if not name:
        raise HTTPException(status_code=422, detail="Name is required")

    now = datetime.now(timezone.utc)
    # `signup_source` value must match the LIKE marker in
    # _enforce_signup_ip_rate_limit exactly. json.dumps produces
    # `"signup_source": "newsletter"` with a single space after the
    # colon (the default separator), which is what the LIKE pattern
    # matches. Keep these in sync if either side is ever edited.
    record = ContactRecord(
        role=payload.role,
        name=name,
        email=payload.email,
        data=_json.dumps({"signup_source": "newsletter", "company": company}),
        ip_address=ip_address,
        created_at=now,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    # subscribe_and_reply handles upsert + auto-reply + audit. Best-effort;
    # failures inside it never raise back to us. If the subscriber upsert
    # fails for any reason we still return 201 because the ContactRecord
    # captured the submission and the operator can follow up manually.
    await subscribe_and_reply(
        session,
        name=name,
        email=payload.email,
        role=payload.role,
        contact_id=record.id,
    )

    return {"status": "subscribed", "id": record.id}


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    token: str = Query(default="", min_length=0, max_length=128),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Token-driven unsubscribe.

    The token comes from the unsubscribe URL we embed in every newsletter
    and auto-reply email. Idempotent: a second click on the same link
    still reports success. Unknown tokens get the same success page so
    we don't leak whether an address is on the list (avoid token-guess
    enumeration).
    """

    from gestaltworkframe.core.deployment_config import get_deployment_config

    home_url = get_deployment_config().site.base_url.rstrip("/")
    token = (token or "").strip()
    if not token:
        return HTMLResponse(
            _UNSUB_CONFIRM_HTML.format(
                heading="Unsubscribe link not recognized",
                home_url=home_url,
                body=(
                    "This URL is missing the token that identifies your subscription. "
                    "If you wanted to unsubscribe, reply to the most recent newsletter email "
                    "and we'll remove you manually."
                ),
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    sub = await unsubscribe_by_token(session, token)
    await session.commit()
    # Both valid-token and unknown-token branches return the same
    # generic message. Two reasons: (1) we don't reveal whether the
    # token corresponds to a real subscriber, which blocks address
    # enumeration via token-guessing; (2) we don't echo the subscriber
    # email back in the page body, so a leaked link (forwarded mail,
    # screenshot, mail-server log) does not reveal the address it was
    # paired with.
    if sub is None:
        logger.info("Unsubscribe request with unrecognized token")
    return HTMLResponse(
        _UNSUB_CONFIRM_HTML.format(
            heading="You're unsubscribed",
            home_url=home_url,
            body=(
                "You won't receive any more newsletter emails at the "
                "address linked to this unsubscribe URL. You can re-subscribe "
                "anytime via the contact form."
            ),
        ),
    )


@router.head("/unsubscribe")
async def unsubscribe_head() -> dict[str, str]:
    """HEAD support so email clients that pre-fetch the unsubscribe link
    get a successful response without actually unsubscribing the user.
    The POST variant below is the One-Click action."""

    return {"status": "ok"}


@router.post("/unsubscribe")
async def unsubscribe_post(
    request: Request,
    token: str = Query(default="", min_length=0, max_length=128),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """RFC 8058 One-Click unsubscribe target.

    Gmail / Yahoo / Outlook bulk-sender compliance: when a newsletter
    email carries the headers

        List-Unsubscribe: <https://example.com/newsletter/unsubscribe?token=XYZ>
        List-Unsubscribe-Post: List-Unsubscribe=One-Click

    the mail client POSTs to this endpoint (form-encoded body
    `List-Unsubscribe=One-Click`) on the user's behalf. The POST body
    itself is ignored; the token comes from the URL query so it stays
    consistent with the GET path.

    Returns 200 on success regardless of token validity, again to avoid
    leaking which tokens belong to real subscribers.
    """
    token = (token or "").strip()
    if not token:
        # Mail-client one-click without a token is malformed; refuse
        # politely without claiming success.
        raise HTTPException(status_code=400, detail="Missing token")
    sub = await unsubscribe_by_token(session, token)
    await session.commit()
    if sub is None:
        logger.info("One-Click unsubscribe POST with unrecognized token")
    return {"status": "unsubscribed"}
