import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from api import contact
from core.db import ContactNotificationRecord, ContactRecord


async def _test_app(tmp_path, monkeypatch, notification=None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contact.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = FastAPI()
    app.middleware("http")(contact.contact_body_size_limit)
    app.include_router(contact.router)

    async def override_session():
        async with maker() as session:
            yield session

    notification = notification or AsyncMock(return_value="sent")
    app.dependency_overrides[contact.get_session] = override_session
    monkeypatch.setattr(contact, "send_contact_notification", notification)
    return app, maker, engine, notification


async def _records(maker, model):
    async with maker() as session:
        result = await session.execute(select(model))
        return result.scalars().all()


def _request_with_client(client_host: str, forwarded_for: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/contact",
            "headers": [(b"x-forwarded-for", forwarded_for.encode())],
            "client": (client_host, 12345),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_client_ip_only_trusts_forwarded_for_from_local_proxy() -> None:
    trusted = _request_with_client("127.0.0.1", "203.0.113.20")
    untrusted = _request_with_client("198.51.100.99", "203.0.113.20")

    assert contact.client_ip(trusted) == "203.0.113.20"
    assert contact.client_ip(untrusted) == "198.51.100.99"


@pytest.mark.asyncio
async def test_contact_accepts_frontend_lead_payload_and_logs_notification(tmp_path, monkeypatch):
    app, maker, engine, notification = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/contact",
                headers={"x-forwarded-for": "203.0.113.10"},
                json={
                    "name": " A\x00 User ",
                    "email": "USER@example.com",
                    "role": "interested_party",
                    "company": "Acme",
                    "dream_automations": ["Ticket routing and triage"],
                    "automation_journey": "Just starting to figure out what's possible",
                    "timeline": "ASAP",
                    "notes": "Need help\x07 with routing.",
                },
            )

        assert response.status_code == 201
        records = await _records(maker, ContactRecord)
        notifications = await _records(maker, ContactNotificationRecord)

        assert len(records) == 1
        assert records[0].name == "A User"
        assert records[0].email == "user@example.com"
        assert records[0].ip_address == "203.0.113.10"
        assert json.loads(records[0].data)["notes"] == "Need help with routing."
        assert len(notifications) == 1
        assert notifications[0].status == "sent"
        notification.assert_awaited_once()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_duplicate_can_update_existing_profile(tmp_path, monkeypatch):
    app, maker, engine, _ = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    payload = {
        "name": "A User",
        "email": "user@example.com",
        "role": "interested_party",
        "company": "Acme",
        "automation_journey": "Just starting to figure out what's possible",
        "timeline": "ASAP",
        "notes": "First pass.",
    }

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/contact", json=payload)).status_code == 201
            duplicate = await client.post("/contact", json=payload)
            payload["notes"] = "Updated profile."
            updated = await client.post("/contact?update=true", json=payload)

        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["label"] == "interested party"
        assert updated.status_code == 200

        records = await _records(maker, ContactRecord)
        assert len(records) == 1
        assert json.loads(records[0].data)["notes"] == "Updated profile."
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_update_requires_existing_profile(tmp_path, monkeypatch):
    app, maker, engine, _ = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/contact?update=true",
                json={
                    "name": "A User",
                    "email": "missing@example.com",
                    "role": "student",
                    "learning_topics": ["Automation fundamentals"],
                },
            )

        assert response.status_code == 404
        assert await _records(maker, ContactRecord) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_notification_failure_is_logged_without_failing_submission(
    tmp_path,
    monkeypatch,
    caplog,
):
    async def fail_notification(*args, **kwargs):
        raise RuntimeError("graph down")

    app, maker, engine, _ = await _test_app(tmp_path, monkeypatch, fail_notification)
    transport = httpx.ASGITransport(app=app)
    caplog.set_level(logging.WARNING, logger="api.contact")

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/contact",
                json={
                    "name": "A User",
                    "email": "user@example.com",
                    "role": "student",
                    "learning_topics": ["Automation fundamentals"],
                },
            )

        assert response.status_code == 201
        notifications = await _records(maker, ContactNotificationRecord)
        assert notifications[0].status == "failed"
        assert notifications[0].error == "graph down"
        assert "Contact notification failed" in caplog.text
        assert "graph down" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_rate_limits_by_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(contact, "_IP_LIMIT", 1)
    app, _, engine, _ = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/contact",
                headers={"x-forwarded-for": "203.0.113.11"},
                json={
                    "name": "A User",
                    "email": "first@example.com",
                    "role": "student",
                },
            )
            second = await client.post(
                "/contact",
                headers={"x-forwarded-for": "203.0.113.11"},
                json={
                    "name": "B User",
                    "email": "second@example.com",
                    "role": "student",
                },
            )

        assert first.status_code == 201
        assert second.status_code == 429
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_rejects_oversized_request_body(tmp_path, monkeypatch):
    monkeypatch.setattr(contact, "_CONTACT_MAX_BODY_BYTES", 10)
    app, maker, engine, notification = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/contact",
                json={
                    "name": "A User",
                    "email": "user@example.com",
                    "role": "student",
                    "learning_topics": ["Automation fundamentals"],
                },
            )

        assert response.status_code == 413
        assert await _records(maker, ContactRecord) == []
        notification.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_contact_body_size_guard_reads_actual_body_without_content_length(monkeypatch):
    monkeypatch.setattr(contact, "_CONTACT_MAX_BODY_BYTES", 5)
    request = SimpleNamespace(
        url=SimpleNamespace(path="/contact"),
        headers={},
        body=AsyncMock(return_value=b"123456"),
    )

    async def call_next(_request):
        raise AssertionError("oversized contact body reached the route")

    response = await contact.contact_body_size_limit(request, call_next)

    assert response.status_code == 413
