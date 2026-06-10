import os
import logging
from typing import Literal

import httpx

from gestaltworkframe.core.deployment_config import get_deployment_config
from gestaltworkframe.core.handoff_packets import build_contact_handoff_packet, render_packet_html

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPE = "https://graph.microsoft.com/.default"
NotificationStatus = Literal["sent", "skipped"]


def _default_sender() -> str:
    override = os.getenv("MS365_SEND_AS", "").strip()
    if override:
        return override
    return get_deployment_config().identity.public_email


async def _get_token(client: httpx.AsyncClient, tenant: str, client_id: str, secret: str) -> str:
    resp = await client.post(
        _TOKEN_URL.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": _SCOPE,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _build_html(role: str, name: str, email: str, fields: dict) -> str:
    return render_packet_html(build_contact_handoff_packet(role, name, email, fields))


async def send_contact_notification(
    role: str,
    name: str,
    email: str,
    extra_fields: dict,
) -> NotificationStatus:
    sender = _default_sender()

    subject = f"[contact] {role.replace('_', ' ').title()} | {name}"
    html = _build_html(role, name, email, extra_fields)

    return await send_internal_email(
        subject,
        html,
        reply_to={"address": email, "name": name},
        sender=sender,
    )


async def send_internal_email(
    subject: str,
    html: str,
    *,
    recipient: str | None = None,
    reply_to: dict[str, str] | None = None,
    sender: str | None = None,
) -> NotificationStatus:
    tenant = os.getenv("MS365_TENANT_ID", "")
    client_id = os.getenv("MS365_CLIENT_ID", "")
    secret = os.getenv("MS365_CLIENT_SECRET", "")
    sender = sender or _default_sender()
    recipient = recipient or os.getenv("DISCOVERY_DIGEST_TO", sender)

    if not all([tenant, client_id, secret]):
        logger.warning("MS365 credentials not configured, skipping email notification")
        return "skipped"

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
        },
        "saveToSentItems": True,
    }
    if reply_to:
        payload["message"]["replyTo"] = [{"emailAddress": reply_to}]

    async with httpx.AsyncClient(timeout=15.0) as client:
        token = await _get_token(client, tenant, client_id, secret)
        resp = await client.post(
            _SEND_URL.format(sender=sender),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code not in (200, 202):
            logger.error("Graph API mail error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()

    return "sent"
