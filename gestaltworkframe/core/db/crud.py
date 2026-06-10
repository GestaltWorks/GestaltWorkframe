"""Async CRUD helpers for chat conversations, intake, and usage.

Every helper takes an AsyncSession (or opens its own fresh session via the
`_in_new_session` suffix variants). The streaming chat path needs the
fresh-session variants because the request session has already closed by
the time the SSE generator's `finally` block runs.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from gestaltworkframe.core.db.engine import async_session_maker
from gestaltworkframe.core.db.models import (
    ChatUsageRecord,
    Conversation,
    IntakeRecord,
    MessageRecord,
    TerminalIntakeRecord,
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_stored_text(value: str) -> str:
    return _CONTROL_RE.sub("", value).strip()


async def create_conversation(mode: str, session: AsyncSession) -> Conversation:
    conv = Conversation(mode=mode)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return conv


async def save_intake_record(
    conversation_id: str,
    selected_mode: str,
    intake: dict[str, str],
    session: AsyncSession,
) -> IntakeRecord:
    result = await session.execute(
        select(IntakeRecord).where(IntakeRecord.conversation_id == conversation_id)
    )
    record = result.scalar_one_or_none()
    payload = json.dumps(intake, ensure_ascii=False)
    if record is None:
        record = IntakeRecord(conversation_id=conversation_id, selected_mode=selected_mode)
        session.add(record)

    record.selected_mode = selected_mode
    record.objective = intake.get("objective", "")
    record.building = intake.get("building", "")
    record.maturity = intake.get("maturity", "")
    record.help_needed = intake.get("help_needed", "")
    record.data = payload
    record.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(record)
    return record


async def save_terminal_intake_submission(
    terminal_session_id: str,
    selected_mode: str,
    intake: dict[str, str],
    session: AsyncSession,
    *,
    source_path: str = "",
    referrer: str = "",
    user_agent: str = "",
    ip_address: str = "",
    conversation_id: str | None = None,
    contact_id: str | None = None,
    count_submission: bool = True,
) -> TerminalIntakeRecord:
    result = await session.execute(
        select(TerminalIntakeRecord)
        .where(TerminalIntakeRecord.terminal_session_id == terminal_session_id)
        .order_by(TerminalIntakeRecord.created_at.desc())
        .limit(1)
    )
    record = result.scalars().first()
    if record is None:
        record = TerminalIntakeRecord(
            terminal_session_id=terminal_session_id,
            selected_mode=selected_mode,
        )
        session.add(record)

    _apply_terminal_intake_record(
        record,
        selected_mode,
        intake,
        source_path=source_path,
        referrer=referrer,
        user_agent=user_agent,
        ip_address=ip_address,
        conversation_id=conversation_id,
        contact_id=contact_id,
        count_submission=count_submission,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        result = await session.execute(
            select(TerminalIntakeRecord).where(TerminalIntakeRecord.terminal_session_id == terminal_session_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise exc
        _apply_terminal_intake_record(
            record,
            selected_mode,
            intake,
            source_path=source_path,
            referrer=referrer,
            user_agent=user_agent,
            ip_address=ip_address,
            conversation_id=conversation_id,
            contact_id=contact_id,
            count_submission=count_submission,
        )
        await session.commit()
    await session.refresh(record)
    return record


def _apply_terminal_intake_record(
    record: TerminalIntakeRecord,
    selected_mode: str,
    intake: dict[str, str],
    *,
    source_path: str,
    referrer: str,
    user_agent: str,
    ip_address: str,
    conversation_id: str | None,
    contact_id: str | None,
    count_submission: bool,
) -> None:
    cleaned = {key: _clean_stored_text(str(value)) for key, value in intake.items()}
    record.selected_mode = _clean_stored_text(selected_mode)
    record.objective = cleaned.get("objective", "")
    record.building = cleaned.get("building", "")
    record.maturity = cleaned.get("maturity", "")
    record.help_needed = cleaned.get("help_needed", "")
    if source_path and not record.source_path:
        record.source_path = _clean_stored_text(source_path)
    if referrer and not record.referrer:
        record.referrer = _clean_stored_text(referrer)
    if user_agent and not record.user_agent:
        record.user_agent = _clean_stored_text(user_agent)
    if ip_address:
        record.ip_address = _clean_stored_text(ip_address)
    record.data = json.dumps(cleaned, ensure_ascii=False)
    if count_submission:
        record.submission_count += 1
    if conversation_id:
        record.conversation_id = conversation_id
    if contact_id:
        record.contact_id = contact_id
    record.updated_at = datetime.now(timezone.utc)


async def get_conversation(conv_id: str, session: AsyncSession) -> Conversation | None:
    result = await session.execute(select(Conversation).where(Conversation.id == conv_id))
    return result.scalar_one_or_none()


async def get_messages(conv_id: str, session: AsyncSession) -> list[MessageRecord]:
    result = await session.execute(
        select(MessageRecord)
        .where(MessageRecord.conversation_id == conv_id)
        .order_by(MessageRecord.created_at)
    )
    return result.scalars().all()


async def chat_usage_snapshot(
    session: AsyncSession,
    *,
    ip_address: str,
    session_key: str,
    ip_rate_since: datetime,
    session_rate_since: datetime,
    token_since: datetime,
) -> dict[str, int]:
    ip_count = await session.execute(
        select(func.count())
        .select_from(ChatUsageRecord)
        .where(
            ChatUsageRecord.ip_address == ip_address,
            ChatUsageRecord.input_tokens > 0,
            ChatUsageRecord.created_at >= ip_rate_since,
        )
    )
    session_count = await session.execute(
        select(func.count())
        .select_from(ChatUsageRecord)
        .where(
            ChatUsageRecord.session_key == session_key,
            ChatUsageRecord.input_tokens > 0,
            ChatUsageRecord.created_at >= session_rate_since,
        )
    )
    daily_tokens = await session.execute(
        select(func.coalesce(func.sum(ChatUsageRecord.input_tokens + ChatUsageRecord.output_tokens), 0))
        .select_from(ChatUsageRecord)
        .where(ChatUsageRecord.created_at >= token_since)
    )
    return {
        "ip_requests": int(ip_count.scalar_one()),
        "session_requests": int(session_count.scalar_one()),
        "daily_tokens": int(daily_tokens.scalar_one()),
    }


async def add_chat_usage_event(
    session: AsyncSession,
    *,
    ip_address: str,
    session_key: str,
    conversation_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ChatUsageRecord:
    record = ChatUsageRecord(
        ip_address=_clean_stored_text(ip_address)[:128],
        session_key=_clean_stored_text(session_key)[:160],
        conversation_id=conversation_id,
        input_tokens=max(input_tokens, 0),
        output_tokens=max(output_tokens, 0),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def add_chat_usage_event_in_new_session(
    *,
    ip_address: str,
    session_key: str,
    conversation_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> ChatUsageRecord:
    async with async_session_maker() as session:
        return await add_chat_usage_event(
            session,
            ip_address=ip_address,
            session_key=session_key,
            conversation_id=conversation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


async def add_message(conv_id: str, role: str, content: str, session: AsyncSession) -> MessageRecord:
    msg = MessageRecord(conversation_id=conv_id, role=role, content=content)
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def add_message_in_new_session(conv_id: str, role: str, content: str) -> MessageRecord:
    async with async_session_maker() as session:
        return await add_message(conv_id, role, content, session)
