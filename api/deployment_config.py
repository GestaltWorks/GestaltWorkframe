from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from api.services import require_admin_token
from core.deployment_config import get_deployment_config


router = APIRouter(tags=["deployment-config"])


@router.get("/api/deployment-config")
async def deployment_config() -> dict[str, Any]:
    return get_deployment_config().public_payload()


@router.get("/admin/api/deployment-config")
async def admin_deployment_config(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    return get_deployment_config().admin_payload()
