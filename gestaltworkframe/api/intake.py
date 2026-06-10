import os
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gestaltworkframe.api.request_helpers import client_ip, make_body_size_limit
from gestaltworkframe.core.db import TerminalIntakeRecord, get_session, save_terminal_intake_submission

# Re-export for api/main.py which imports clean_text and client_ip from this module.
__all__ = ["clean_text", "client_ip", "intake_body_size_limit", "router"]

router = APIRouter(prefix="/intake", tags=["intake"])

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INTAKE_MAX_BODY_BYTES = int(os.getenv("INTAKE_MAX_BODY_BYTES", "32768"))
_IP_WINDOW = timedelta(hours=24)
_SESSION_WINDOW = timedelta(hours=24)
_IP_LIMIT = int(os.getenv("INTAKE_IP_LIMIT", "20"))
_SESSION_LIMIT = int(os.getenv("INTAKE_SESSION_LIMIT", "3"))


def clean_text(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


intake_body_size_limit = make_body_size_limit(
    path="/intake/submissions",
    # Lazy lookup so tests can monkeypatch _INTAKE_MAX_BODY_BYTES on the module.
    max_bytes=lambda: _INTAKE_MAX_BODY_BYTES,
    detail="Intake request body is too large.",
)


class IntakeSubmissionAnswers(BaseModel):
    objective: str = Field(min_length=1, max_length=300)
    building: str = Field(min_length=1, max_length=1000)
    maturity: str = Field(min_length=1, max_length=200)
    help_needed: str = Field(min_length=1, max_length=200)

    @field_validator("*")
    @classmethod
    def _sanitize_strings(cls, value: str) -> str:
        return clean_text(value)


class IntakeSubmissionPayload(BaseModel):
    terminal_session_id: str = Field(min_length=8, max_length=100)
    selected_mode: Literal["pipeline", "automator", "educator"]
    intake: IntakeSubmissionAnswers
    source_path: str = Field(default="", max_length=300)

    @field_validator("terminal_session_id", "source_path")
    @classmethod
    def _sanitize_strings(cls, value: str) -> str:
        return clean_text(value)


async def _latest_submission(session: AsyncSession, terminal_session_id: str) -> TerminalIntakeRecord | None:
    result = await session.execute(
        select(TerminalIntakeRecord)
        .where(TerminalIntakeRecord.terminal_session_id == terminal_session_id)
        .order_by(TerminalIntakeRecord.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _submission_count(session: AsyncSession, field, value: str, since: datetime) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(TerminalIntakeRecord)
        .where(field == value, TerminalIntakeRecord.created_at >= since)
    )
    return int(result.scalar_one())


async def _enforce_rate_limits(
    session: AsyncSession,
    terminal_session_id: str,
    ip_address: str,
    existing: TerminalIntakeRecord | None,
) -> None:
    now = datetime.now(timezone.utc)
    ip_count = await _submission_count(
        session,
        TerminalIntakeRecord.ip_address,
        ip_address,
        now - _IP_WINDOW,
    )
    if ip_count >= _IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many intake submissions from this network. Please try again later.",
        )

    if existing is not None:
        last_seen = existing.updated_at or existing.created_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen >= now - _SESSION_WINDOW and existing.submission_count >= _SESSION_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many intake submissions from this session. Please try again later.",
            )
        return

    session_count = await _submission_count(
        session,
        TerminalIntakeRecord.terminal_session_id,
        terminal_session_id,
        now - _SESSION_WINDOW,
    )
    if session_count >= _SESSION_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many intake submissions from this session. Please try again later.",
        )

@router.post("/submissions", status_code=201)
async def submit_intake(
    request: Request,
    response: Response,
    payload: IntakeSubmissionPayload,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    ip_address = client_ip(request)
    existing = await _latest_submission(session, payload.terminal_session_id)
    await _enforce_rate_limits(session, payload.terminal_session_id, ip_address, existing)
    if existing is not None:
        response.status_code = status.HTTP_200_OK

    record = await save_terminal_intake_submission(
        payload.terminal_session_id,
        payload.selected_mode,
        payload.intake.model_dump(),
        session,
        source_path=payload.source_path,
        referrer=clean_text(request.headers.get("referer", ""))[:500],
        user_agent=clean_text(request.headers.get("user-agent", ""))[:500],
        ip_address=ip_address,
    )
    return {"status": "received", "id": record.id}
