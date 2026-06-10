"""Phase C tests: scheduled send + cancel + dispatch.

Covers:
- POST /issues/{id}/approve schedules a future send (status -> approved,
  scheduled_send_at set) and returns the timestamp.
- Default schedule (no body / null scheduled_send_at) lands at now +
  DEFAULT_SCHEDULE_DELAY.
- POST /issues/{id}/cancel-send pulls a scheduled issue back to
  awaiting_approval and clears scheduled_send_at.
- Cancel after dispatcher fires returns 409.
- POST /dispatch-due fires every approved issue with a passed scheduled
  send time and clears the queue.
- A second dispatcher call is a no-op (atomic transition).
- Year-out-of-bounds approval body is rejected at the Pydantic layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import api.admin_discovery as api_admin_discovery
import api.main as api_main
from gestaltworkframe.core import newsletter as newsletter_module
from gestaltworkframe.core.db import (
    DiscoveryFind,
    DiscoverySource,
    NewsletterIssue,
    Subscriber,
)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pc.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(init())

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    return TestClient(api_main.app), engine, maker


def _seed_issue_and_sub(maker, *, status: str = "awaiting_approval") -> str:
    """Compose an issue ready for approval and add one active subscriber."""
    async def seed() -> str:
        async with maker() as session:
            source = DiscoverySource(name="phc_src", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="post",
                external_id="phc1",
                title="PhC post",
                url="https://x/phc1",
                status="auto_indexed",
                decided_at=datetime.now(timezone.utc),
                newsletter_pending=True,
            ))
            session.add(Subscriber(email="phc@example.com", name="PhC", source_role="student", topics="general"))
            await session.commit()
            result = await newsletter_module.compose_pending_issue(session)
            issue_id = result.issue.id
            if status != "awaiting_approval":
                await session.execute(
                    update(NewsletterIssue)
                    .where(NewsletterIssue.id == issue_id)
                    .values(status=status)
                )
                await session.commit()
            return issue_id
    return asyncio.run(seed())


def test_approve_with_scheduled_send_at_sets_status_and_timestamp(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        response = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={"scheduled_send_at": future.isoformat()},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["issue"]
        assert body["status"] == "approved"
        assert body["scheduled_send_at"] is not None
        assert body["sent_at"] is None
        # Round-trip the timestamp through ISO parsing; tolerate
        # SQLite's tz stripping by comparing without tzinfo.
        scheduled = datetime.fromisoformat(body["scheduled_send_at"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        assert abs((scheduled - future).total_seconds()) < 2
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_approve_without_schedule_defaults_to_30_minute_delay(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        now = datetime.now(timezone.utc)
        response = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["issue"]
        assert body["status"] == "approved"
        scheduled = datetime.fromisoformat(body["scheduled_send_at"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        delta = scheduled - now
        # 30 minutes +/- a generous test margin.
        assert timedelta(minutes=28) < delta < timedelta(minutes=32)
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_approve_rejects_far_future_schedule(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        far_future = datetime.now(timezone.utc) + timedelta(days=400)
        response = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={"scheduled_send_at": far_future.isoformat()},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 422
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_cancel_send_pulls_issue_back_to_awaiting_approval(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={"scheduled_send_at": future.isoformat()},
            headers={"X-Admin-Token": "test-admin"},
        )
        response = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/cancel-send",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200, response.text
        body = response.json()["issue"]
        assert body["status"] == "awaiting_approval"
        assert body["scheduled_send_at"] is None
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_cancel_send_409_when_status_not_approved(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        # Issue stays at awaiting_approval — never approved.
        issue_id = _seed_issue_and_sub(maker)
        response = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/cancel-send",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 409
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_dispatch_due_sends_approved_past_schedule(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        # Approve, then move the scheduled time into the past so the
        # dispatcher will pick it up immediately.
        client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={"scheduled_send_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            headers={"X-Admin-Token": "test-admin"},
        )

        async def expire() -> None:
            async with maker() as session:
                await session.execute(
                    update(NewsletterIssue)
                    .where(NewsletterIssue.id == issue_id)
                    .values(scheduled_send_at=datetime.now(timezone.utc) - timedelta(minutes=1))
                )
                await session.commit()

        asyncio.run(expire())

        response = client.post(
            "/admin/api/newsletter/dispatch-due",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        assert summary["dispatched"] == 1
        assert summary["failed"] == 0

        # Second call: no due issues left, dispatched=0.
        response2 = client.post(
            "/admin/api/newsletter/dispatch-due",
            json={},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert response2.json()["summary"]["dispatched"] == 0

        # Verify the issue is fully sent and newsletter_pending is cleared.
        async def fetch_state():
            async with maker() as session:
                issue = (await session.execute(
                    select(NewsletterIssue).where(NewsletterIssue.id == issue_id)
                )).scalar_one()
                finds = (await session.execute(
                    select(DiscoveryFind).where(DiscoveryFind.newsletter_pending.is_(True))
                )).scalars().all()
                return issue.status, len(list(finds))

        status, still_pending = asyncio.run(fetch_state())
        assert status == "sent"
        assert still_pending == 0
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_cancel_and_dispatch_require_admin_token(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue_and_sub(maker)
        # cancel-send without the token
        resp1 = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/cancel-send",
            json={},
        )
        assert resp1.status_code in (401, 403)
        # dispatch-due without the token
        resp2 = client.post(
            "/admin/api/newsletter/dispatch-due",
            json={},
        )
        assert resp2.status_code in (401, 403)
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
