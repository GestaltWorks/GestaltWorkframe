from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.connector_registry import connector_registry
from gestalt_connector_protocol import WebhookRequest


router = APIRouter(tags=["connector-webhooks"])
FORWARDED_WEBHOOK_HEADER_ALLOWLIST = {"content-type", "user-agent"}


class ConnectorWebhookResponse(BaseModel):
    accepted: bool
    connector_id: str
    documents_emitted: int = Field(ge=0)
    message: str = ""


@router.post("/connectors/webhook/{connector_id}")
async def connector_webhook(connector_id: str, request: Request) -> ConnectorWebhookResponse:
    registered = connector_registry.get(connector_id)
    if registered is None:
        raise HTTPException(status_code=404, detail="connector not registered")
    body = await request.body()
    if not _authenticated(registered.config.auth, request.headers, body):
        # Return the same 404 as an unknown connector so callers cannot enumerate registered webhook IDs.
        raise HTTPException(status_code=404, detail="connector not registered")
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="webhook payload must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="webhook payload must be an object")
    result = await registered.connector.webhook_handler(
        registered.config,
        WebhookRequest(connector_id=connector_id, headers=_forwarded_headers(request.headers), payload=payload),
    )
    if not result.accepted:
        raise HTTPException(status_code=400, detail=result.message or "webhook rejected")
    return ConnectorWebhookResponse(accepted=True, connector_id=connector_id, documents_emitted=len(result.documents), message=result.message)


def _authenticated(auth: Any, headers: Any, body: bytes) -> bool:
    if not isinstance(auth, dict):
        return False
    secret = str(auth.get("webhook_secret") or auth.get("secret") or "")
    if not secret:
        return False
    signature = headers.get("x-webhook-signature", "")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _forwarded_headers(headers: Any) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in FORWARDED_WEBHOOK_HEADER_ALLOWLIST or lowered.startswith("x-"):
            forwarded[lowered] = value
    return forwarded
