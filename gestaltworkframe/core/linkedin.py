"""LinkedIn post helper for the newsletter cycle.

Dark by default. The auto-post path only activates when all required
env vars are populated:

    LINKEDIN_CLIENT_ID
    LINKEDIN_CLIENT_SECRET
    LINKEDIN_REFRESH_TOKEN
    LINKEDIN_AUTHOR_URN   (e.g. "urn:li:person:abc123" or "urn:li:organization:456")

Until those exist, approve_and_distribute records a `linkedin` delivery
with status="skipped" and reason="not_configured", and the admin
panel's "Copy for LinkedIn" button is the operating mechanism.

To enable LinkedIn auto-post you need:

1. A LinkedIn Developer app at https://developer.linkedin.com/
2. Marketing Developer Platform approval (often weeks) for these scopes:
   - r_liteprofile (to discover your member id)
   - w_member_social (to post on behalf of the authorized member)
3. An OAuth flow that gets you a refresh token. Set the resulting
   refresh_token + client_id + client_secret + author_urn on the VPS,
   then restart the service.
4. LinkedIn's 2024 posts API endpoint is https://api.linkedin.com/rest/posts
   with LinkedIn-Version: 202405 (or current) and the Restli protocol
   version header. This module uses that endpoint.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

LINKEDIN_API_VERSION = "202405"
LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class LinkedInResult:
    status: str  # "sent" | "skipped" | "failed"
    reason: str
    post_urn: str = ""


def _config() -> dict[str, str]:
    return {
        "client_id": os.getenv("LINKEDIN_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("LINKEDIN_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("LINKEDIN_REFRESH_TOKEN", "").strip(),
        "author_urn": os.getenv("LINKEDIN_AUTHOR_URN", "").strip(),
    }


def is_configured() -> bool:
    cfg = _config()
    return all(cfg.values())


async def _refresh_access_token(client: httpx.AsyncClient, cfg: dict[str, str]) -> str:
    response = await client.post(
        LINKEDIN_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": cfg["refresh_token"],
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("LinkedIn refresh response had no access_token")
    return token


async def post_to_linkedin(content: str) -> LinkedInResult:
    """Post the text as a LinkedIn share. Returns a LinkedInResult.

    Errors and timeouts are caught and returned as status="failed";
    callers (approve_and_distribute) record the result on a
    NewsletterDelivery row.
    """
    cfg = _config()
    if not all(cfg.values()):
        return LinkedInResult(status="skipped", reason="not_configured")

    payload = {
        "author": cfg["author_urn"],
        "commentary": content,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            token = await _refresh_access_token(client, cfg)
            response = await client.post(
                LINKEDIN_POSTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "LinkedIn-Version": LINKEDIN_API_VERSION,
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code in (200, 201):
                post_urn = response.headers.get("x-restli-id", "")
                return LinkedInResult(status="sent", reason="published", post_urn=post_urn)
            logger.error("LinkedIn post failed %s: %s", response.status_code, response.text[:500])
            return LinkedInResult(
                status="failed",
                reason=f"http_{response.status_code}: {response.text[:200]}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("LinkedIn post raised exception")
        return LinkedInResult(status="failed", reason=f"exception: {type(exc).__name__}: {exc}")
