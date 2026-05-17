"""Contact-form auto-reply composition and dispatch.

Three reply templates, one per ContactRecord.role:

- student → newsletter signup confirmation + education note
- automation_engineer → newsletter signup + community invite
- interested_party → scoping note + booking link

All templates carry an unsubscribe URL. Sending is best-effort; failures
are logged but never roll back the contact submission or subscriber row.

Per-deployment values (org name, site URL, signature, contact path,
library path) are pulled from DeploymentConfig. External URLs come from
env:

- COMMUNITY_INVITE_URL — community/chat invite, defaults to placeholder
- BOOKING_URL — calendar/booking link, defaults to placeholder
- SITE_PUBLIC_URL — overrides DeploymentConfig.site.base_url when set
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from core.deployment_config import get_deployment_config
from core.email_service import NotificationStatus, send_internal_email

logger = logging.getLogger(__name__)

ROLE_STUDENT = "student"
ROLE_ENGINEER = "automation_engineer"
ROLE_INTERESTED = "interested_party"

_PLACEHOLDER_COMMUNITY = "https://example.com/community-invite"
_PLACEHOLDER_BOOKING = "https://example.com/booking"


def _site_url() -> str:
    override = os.getenv("SITE_PUBLIC_URL", "").strip()
    if override:
        return override.rstrip("/")
    return get_deployment_config().site.base_url.rstrip("/")


def _org_name() -> str:
    return get_deployment_config().identity.organization_name


def _signature() -> str:
    return get_deployment_config().identity.signature_name


def _contact_path() -> str:
    return get_deployment_config().identity.contact_path


def _library_path() -> str:
    return get_deployment_config().identity.library_path


def _public_email() -> str:
    return get_deployment_config().identity.public_email


def _community_url() -> str:
    return os.getenv("COMMUNITY_INVITE_URL", _PLACEHOLDER_COMMUNITY)


def _booking_url() -> str:
    return os.getenv("BOOKING_URL", _PLACEHOLDER_BOOKING)


def _unsubscribe_url(token: str) -> str:
    return f"{_site_url()}/newsletter/unsubscribe?token={token}"


@dataclass(frozen=True)
class AutoReply:
    subject: str
    html: str
    plain: str
    template_id: str


def compose_auto_reply(role: str, name: str, unsubscribe_token: str) -> AutoReply:
    """Pick the right template for the role and fill in name/links.

    Unknown roles get the generic newsletter-only template so a future
    role addition does not silently skip the confirmation.
    """

    first_name = (name.split(" ", 1)[0] if name else "there").strip() or "there"
    unsub = _unsubscribe_url(unsubscribe_token)

    if role == ROLE_STUDENT:
        return _student_template(first_name, unsub)
    if role == ROLE_ENGINEER:
        return _engineer_template(first_name, unsub, _community_url())
    if role == ROLE_INTERESTED:
        return _interested_template(first_name, unsub, _booking_url())
    return _generic_template(first_name, unsub)


async def send_auto_reply(
    role: str,
    name: str,
    email: str,
    unsubscribe_token: str,
) -> tuple[NotificationStatus, str, str]:
    """Compose and send the role-appropriate auto-reply.

    Returns (status, template_id, error) so the caller can persist an
    audit row. Failures are logged but never raised.
    """

    reply = compose_auto_reply(role, name, unsubscribe_token)
    try:
        status = await send_internal_email(
            reply.subject,
            reply.html,
            recipient=email,
            sender=os.getenv("AUTOREPLY_SENDER", _public_email()),
        )
        return status, reply.template_id, ""
    except Exception as exc:  # noqa: BLE001 — auto-reply must never break form submit
        logger.exception("Auto-reply send failed for %s (%s)", email, role)
        return "skipped", reply.template_id, str(exc)[:512]


# ---------------------------------------------------------------------------
# Templates. Plain, direct, no em dashes, no manufactured enthusiasm.
# Each template renders both HTML and plain text so spam filters and
# accessibility tools have a clean text fallback. Org name, site URL,
# signature, contact path, and library path come from DeploymentConfig.
# ---------------------------------------------------------------------------

_BRAND_FOOTER_HTML_TEMPLATE = (
    "<hr style=\"border:none;border-top:1px solid #d4bf9133;margin:32px 0 16px\"/>"
    "<p style=\"color:#888;font-size:12px;line-height:18px;margin:0\">"
    "{org} &middot; {site} &middot; "
    "Sent because you submitted the contact form at {site}{contact}. "
    "<a href=\"{unsub}\" style=\"color:#888;text-decoration:underline\">Unsubscribe</a>"
    "</p>"
)

_BRAND_FOOTER_PLAIN_TEMPLATE = (
    "\n\n--\n"
    "{org} / {site}\n"
    "Sent because you submitted the contact form at {site}{contact}.\n"
    "Unsubscribe: {unsub}"
)


def _footer_html(unsub: str) -> str:
    return _BRAND_FOOTER_HTML_TEMPLATE.format(
        org=_org_name(), site=_site_url(), contact=_contact_path(), unsub=unsub,
    )


def _footer_plain(unsub: str) -> str:
    return _BRAND_FOOTER_PLAIN_TEMPLATE.format(
        org=_org_name(), site=_site_url(), contact=_contact_path(), unsub=unsub,
    )


def _wrap_html(body: str, unsub: str) -> str:
    return (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Inter,sans-serif;"
        "max-width:560px;margin:0 auto;padding:24px;color:#242128;line-height:1.6\">"
        f"{body}"
        f"{_footer_html(unsub)}"
        "</div>"
    )


def _sig_html() -> str:
    return f"<p>- {_signature()}<br/>{_org_name()}</p>"


def _sig_plain() -> str:
    return f"- {_signature()}\n{_org_name()}"


def _student_template(first_name: str, unsub: str) -> AutoReply:
    site = _site_url()
    org = _org_name()
    lib = _library_path()
    subject = "Thanks for reaching out about learning"
    body_html = (
        f"<p>Hi {first_name},</p>"
        f"<p>Thanks for reaching out about learning with {org}. You're on the list "
        f"and will receive the newsletter on its regular cadence.</p>"
        f"<p>The library is browsable at "
        f"<a href=\"{site}{lib}\">{site}{lib}</a>.</p>"
        "<p>Reply to this email with a specific topic you want covered first.</p>"
        f"{_sig_html()}"
    )
    body_plain = (
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out about learning with {org}. You're on the list "
        f"and will receive the newsletter on its regular cadence.\n\n"
        f"The library is browsable at {site}{lib}.\n\n"
        "Reply to this email with a specific topic you want covered first.\n\n"
        f"{_sig_plain()}"
        + _footer_plain(unsub)
    )
    return AutoReply(subject=subject, html=_wrap_html(body_html, unsub), plain=body_plain, template_id="student_v1")


def _engineer_template(first_name: str, unsub: str, community_url: str) -> AutoReply:
    site = _site_url()
    lib = _library_path()
    subject = "Welcome to the list"
    body_html = (
        f"<p>Hi {first_name},</p>"
        "<p>Thanks for the contact. You're on the list and will receive the "
        "newsletter on its regular cadence.</p>"
        "<p>Community discussion happens here: "
        f"<a href=\"{community_url}\">{community_url}</a></p>"
        f"<p>The library is at <a href=\"{site}{lib}\">{site}{lib}</a>.</p>"
        "<p>Reply to this email if you have a specific build in flight you want a second pair of eyes on.</p>"
        f"{_sig_html()}"
    )
    body_plain = (
        f"Hi {first_name},\n\n"
        "Thanks for the contact. You're on the list and will receive the newsletter "
        "on its regular cadence.\n\n"
        f"Community discussion happens here: {community_url}\n\n"
        f"The library is at {site}{lib}.\n\n"
        "Reply to this email if you have a specific build in flight you want a second pair of eyes on.\n\n"
        f"{_sig_plain()}"
        + _footer_plain(unsub)
    )
    return AutoReply(subject=subject, html=_wrap_html(body_html, unsub), plain=body_plain, template_id="engineer_v1")


def _interested_template(first_name: str, unsub: str, booking_url: str) -> AutoReply:
    site = _site_url()
    org = _org_name()
    lib = _library_path()
    subject = "Scoping a project"
    body_html = (
        f"<p>Hi {first_name},</p>"
        "<p>Thanks for reaching out about working together. We will follow up "
        "directly about the specifics you described.</p>"
        "<p>To put time on the calendar to talk through the work: "
        f"<a href=\"{booking_url}\">{booking_url}</a></p>"
        "<p>On that call we will want to understand the systems involved, the current "
        "process, what 'better' looks like for you, and the support expectation after launch. "
        f"That is usually enough to know whether {org} is the right fit and what the "
        "engagement shape would be.</p>"
        f"<p>You are also on the newsletter list. The library is at "
        f"<a href=\"{site}{lib}\">{site}{lib}</a>.</p>"
        f"{_sig_html()}"
    )
    body_plain = (
        f"Hi {first_name},\n\n"
        "Thanks for reaching out about working together. We will follow up directly "
        "about the specifics you described.\n\n"
        f"To put time on the calendar: {booking_url}\n\n"
        "On that call we will want to understand the systems involved, the current "
        "process, what 'better' looks like for you, and the support expectation after launch. "
        f"That is usually enough to know whether {org} is the right fit and what the "
        "engagement shape would be.\n\n"
        f"You are also on the newsletter list. The library is at {site}{lib}.\n\n"
        f"{_sig_plain()}"
        + _footer_plain(unsub)
    )
    return AutoReply(subject=subject, html=_wrap_html(body_html, unsub), plain=body_plain, template_id="interested_v1")


def _generic_template(first_name: str, unsub: str) -> AutoReply:
    site = _site_url()
    lib = _library_path()
    subject = f"Thanks for reaching out"
    body_html = (
        f"<p>Hi {first_name},</p>"
        "<p>Thanks for the contact. You're on the newsletter list. The library is at "
        f"<a href=\"{site}{lib}\">{site}{lib}</a>.</p>"
        "<p>We will follow up directly about your submission.</p>"
        f"{_sig_html()}"
    )
    body_plain = (
        f"Hi {first_name},\n\n"
        "Thanks for the contact. You're on the newsletter list. The library is at "
        f"{site}{lib}.\n\n"
        "We will follow up directly about your submission.\n\n"
        f"{_sig_plain()}"
        + _footer_plain(unsub)
    )
    return AutoReply(subject=subject, html=_wrap_html(body_html, unsub), plain=body_plain, template_id="generic_v1")
