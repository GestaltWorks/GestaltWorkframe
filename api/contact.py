import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.request_helpers import client_ip, make_body_size_limit
from core.db import ContactNotificationRecord, ContactRecord, get_session
from core.email_service import send_contact_notification
from core.subscribers import subscribe_and_reply

router = APIRouter(prefix="/contact", tags=["contact"])
logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL_ROLE_WINDOW = timedelta(hours=24)
_IP_WINDOW = timedelta(hours=24)
_IP_LIMIT = 5
_CONTACT_MAX_BODY_BYTES = int(os.getenv("CONTACT_MAX_BODY_BYTES", "65536"))
_LIST_LIMIT = 20
_LIST_ITEM_LIMIT = 120
_ERROR_LIMIT = 1000

_ROLE_LABELS = {
    "automation_engineer": "automation engineer",
    "student": "student",
    "interested_party": "interested party",
}


def _clean_text(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


def _clean_list(values: list[object]) -> list[str]:
    if len(values) > _LIST_LIMIT:
        raise ValueError(f"List fields are limited to {_LIST_LIMIT} items")

    cleaned: list[str] = []
    for value in values:
        item = _clean_text(str(value))
        if len(item) > _LIST_ITEM_LIMIT:
            raise ValueError(f"List items are limited to {_LIST_ITEM_LIMIT} characters")
        if item:
            cleaned.append(item)
    return cleaned


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


contact_body_size_limit = make_body_size_limit(
    path="/contact",
    # Lazy lookup so tests can monkeypatch _CONTACT_MAX_BODY_BYTES on the module.
    max_bytes=lambda: _CONTACT_MAX_BODY_BYTES,
    detail="Contact request body is too large.",
)


class _Base(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=320)

    @field_validator("*")
    @classmethod
    def _sanitize_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return _clean_text(value)
        if isinstance(value, list):
            return _clean_list(value)
        return value

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        email = value.strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise ValueError("Invalid email address")
        return email


class EngPayload(_Base):
    role: Literal["automation_engineer"]
    platforms: list[str] = Field(default_factory=list)
    project_types: list[str] = Field(default_factory=list)
    llm_tools: list[str] = Field(default_factory=list)
    tool_recommendations: str = Field(default="", max_length=1000)
    repo_url: str = Field(default="", max_length=500)
    community_interest: str = Field(default="", max_length=1000)
    willing_to_contribute: bool = False


class StudentPayload(_Base):
    role: Literal["student"]
    learning_topics: list[str] = Field(default_factory=list)
    experience_level: str = Field(default="", max_length=200)
    format_preferences: list[str] = Field(default_factory=list)
    learning_notes: str = Field(default="", max_length=1000)


class LeadPayload(_Base):
    role: Literal["interested_party"]
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=200)
    dream_automations: list[str] = Field(default_factory=list)
    automation_journey: str = Field(default="", max_length=300)
    current_tools: str = Field(default="", max_length=500)
    timeline: str = Field(default="", max_length=100)
    referral_source: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)


ContactPayload = Annotated[
    Union[EngPayload, StudentPayload, LeadPayload],
    Field(discriminator="role"),
]


async def _latest_contact(
    session: AsyncSession,
    email: str,
    role: str,
) -> ContactRecord | None:
    result = await session.execute(
        select(ContactRecord)
        .where(ContactRecord.email == email, ContactRecord.role == role)
        .order_by(ContactRecord.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _ip_submission_count(
    session: AsyncSession,
    ip_address: str,
    since: datetime,
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(ContactRecord)
        .where(ContactRecord.ip_address == ip_address, ContactRecord.created_at >= since)
    )
    return int(result.scalar_one())


async def _enforce_rate_limits(
    session: AsyncSession,
    payload: ContactPayload,
    ip_address: str,
    latest: ContactRecord | None,
) -> None:
    since = datetime.now(timezone.utc) - _EMAIL_ROLE_WINDOW
    if latest and _as_utc(latest.created_at) >= since:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_contact",
                "label": _ROLE_LABELS.get(payload.role, payload.role),
            },
        )

    await _enforce_ip_rate_limit(session, ip_address)


async def _enforce_ip_rate_limit(session: AsyncSession, ip_address: str) -> None:
    ip_count = await _ip_submission_count(
        session,
        ip_address,
        datetime.now(timezone.utc) - _IP_WINDOW,
    )
    if ip_count >= _IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many submissions from this network. Please try again later.",
        )


def _extra_fields(payload: ContactPayload) -> dict[str, object]:
    data = payload.model_dump()
    # Only role-specific fields are stored in the JSON blob; identity stays columnar.
    return {key: value for key, value in data.items() if key not in {"role", "name", "email"}}


async def _save_notification(
    session: AsyncSession,
    contact_id: str,
    status_value: str,
    error: str = "",
) -> None:
    session.add(
        ContactNotificationRecord(
            contact_id=contact_id,
            status=status_value,
            error=error[:_ERROR_LIMIT],
        )
    )
    await session.commit()


async def _notify_contact(
    session: AsyncSession,
    record: ContactRecord,
    payload: ContactPayload,
    extra: dict[str, object],
) -> None:
    try:
        notification_status = await send_contact_notification(
            payload.role,
            payload.name,
            payload.email,
            extra,
        )
        await _save_notification(session, record.id, notification_status)
    except Exception as exc:
        logger.warning(
            "Contact notification failed for contact %s: %s",
            record.id,
            exc,
            exc_info=True,
        )
        await _save_notification(session, record.id, "failed", str(exc))


# Note: subscriber list + auto-reply logic is shared with the lightweight
# /newsletter/subscribe endpoint via core.subscribers.subscribe_and_reply.
# Both paths upsert the same Subscriber row and send the same role-based
# auto-reply; the contact form additionally creates a detailed
# ContactRecord that the newsletter signup does not.


@router.post("", status_code=201)
async def submit_contact(
    request: Request,
    response: Response,
    payload: ContactPayload,
    update: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    ip_address = client_ip(request)
    latest = await _latest_contact(session, payload.email, payload.role)

    if not update:
        await _enforce_rate_limits(session, payload, ip_address, latest)
    elif not latest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact profile not found.",
        )
    else:
        await _enforce_ip_rate_limit(session, ip_address)

    extra = _extra_fields(payload)
    now = datetime.now(timezone.utc)

    if update:
        record = latest
        record.name = payload.name
        record.data = json.dumps(extra)
        record.ip_address = ip_address
        record.updated_at = now
        response.status_code = status.HTTP_200_OK
    else:
        record = ContactRecord(
            role=payload.role,
            name=payload.name,
            email=payload.email,
            data=json.dumps(extra),
            ip_address=ip_address,
            created_at=now,
        )

    session.add(record)
    await session.commit()
    await session.refresh(record)

    await _notify_contact(session, record, payload, extra)
    await subscribe_and_reply(
        session,
        name=payload.name,
        email=payload.email,
        role=payload.role,
        contact_id=record.id,
    )

    return {"status": "received", "id": record.id}