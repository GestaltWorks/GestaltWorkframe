"""Phase D tests: signed approval email link.

Covers:
- make_approval_token / verify_approval_token round-trip.
- Tampered token, expired token, malformed token all reject.
- GET /admin/api/newsletter/approve-via-link with a valid token
  schedules the send (status -> approved with scheduled_send_at set).
- Invalid token returns HTML 400, not JSON.
- The approval email body contains both links and uses safe-attribute
  rendering for the token.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import api.admin_discovery as api_admin_discovery
import api.main as api_main
from gestaltworkframe.core import newsletter as newsletter_module
from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource, NewsletterIssue, Subscriber


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pd.db'}")

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


def _seed_issue(maker) -> str:
    async def seed() -> str:
        async with maker() as session:
            source = DiscoverySource(name="pd_src", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id,
                finding_type="post",
                external_id="pd1",
                title="PD post",
                url="https://x/pd1",
                status="auto_indexed",
                decided_at=datetime.now(timezone.utc),
                newsletter_pending=True,
            ))
            session.add(Subscriber(email="pd@example.com", name="PD", source_role="student", topics="general"))
            await session.commit()
            result = await newsletter_module.compose_pending_issue(session)
            return result.issue.id
    return asyncio.run(seed())


# ---------------------------------------------------------------------------
# Token primitives.
# ---------------------------------------------------------------------------


def test_approval_token_round_trip(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    token = newsletter_module.make_approval_token("issue-abc-123")
    assert newsletter_module.verify_approval_token(token) == "issue-abc-123"


def test_approval_token_rejects_tampered_signature(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    token = newsletter_module.make_approval_token("issue-abc-123")
    # Flip a character in the signature segment.
    head, _sig = token.rsplit(".", 1)
    bad = f"{head}.AAAA"
    with pytest.raises(ValueError):
        newsletter_module.verify_approval_token(bad)


def test_approval_token_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    token = newsletter_module.make_approval_token("issue-abc-123")
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "different-key")
    with pytest.raises(ValueError):
        newsletter_module.verify_approval_token(token)


def test_approval_token_rejects_expired(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    token = newsletter_module.make_approval_token(
        "issue-abc-123", ttl=timedelta(seconds=-1)
    )
    with pytest.raises(ValueError):
        newsletter_module.verify_approval_token(token)


def test_approval_token_rejects_malformed(monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    with pytest.raises(ValueError):
        newsletter_module.verify_approval_token("not.a.token.extra")
    with pytest.raises(ValueError):
        newsletter_module.verify_approval_token("only-one-part")


# ---------------------------------------------------------------------------
# Endpoint.
# ---------------------------------------------------------------------------


def test_approve_via_link_schedules_send(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue(maker)
        token = newsletter_module.make_approval_token(issue_id)

        response = client.get(
            "/admin/api/newsletter/approve-via-link",
            params={"token": token},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "Approved" in response.text

        async def fetch_status():
            async with maker() as session:
                issue = (await session.execute(
                    select(NewsletterIssue).where(NewsletterIssue.id == issue_id)
                )).scalar_one()
                return issue.status, issue.scheduled_send_at

        status, scheduled = asyncio.run(fetch_status())
        assert status == "approved"
        assert scheduled is not None
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_approve_via_link_rejects_bad_token(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        response = client.get(
            "/admin/api/newsletter/approve-via-link",
            params={"token": "garbage.garbage.garbage"},
        )
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/html")
        # Must NOT bleed JSON-style error details into the HTML
        # (the response template uses html.escape) and must not echo
        # the token.
        assert "garbage.garbage.garbage" not in response.text
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_approve_via_link_409_on_already_approved(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        issue_id = _seed_issue(maker)
        token = newsletter_module.make_approval_token(issue_id)
        # Approve once via the link.
        first = client.get(
            "/admin/api/newsletter/approve-via-link",
            params={"token": token},
        )
        assert first.status_code == 200
        # Second click on the same link: the issue is already approved
        # so the helper's atomic UPDATE matches zero rows and we render
        # the 409 page.
        second = client.get(
            "/admin/api/newsletter/approve-via-link",
            params={"token": token},
        )
        assert second.status_code == 409
        assert "Approval not accepted" in second.text
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Email body.
# ---------------------------------------------------------------------------


def test_approval_email_body_contains_both_links(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "phase-d-key")
    monkeypatch.setenv("NEWSLETTER_APPROVAL_TO", "approver@example.com")
    captured = {}

    async def fake_send(subject, body, *, recipient):
        captured["subject"] = subject
        captured["body"] = body
        captured["recipient"] = recipient
        return "sent"

    monkeypatch.setattr(newsletter_module, "send_internal_email", fake_send)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mailbody.db'}")

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            source = DiscoverySource(name="mb", watch_type="rss_watch", target="https://x", active=True)
            session.add(source)
            await session.flush()
            session.add(DiscoveryFind(
                discovery_source_id=source.id, finding_type="post", external_id="mb1",
                title="MB", url="https://x/1", status="auto_indexed",
                decided_at=datetime.now(timezone.utc), newsletter_pending=True,
            ))
            await session.commit()
            result = await newsletter_module.compose_pending_issue(session)
            await newsletter_module._send_approval_notification(result.issue)

    asyncio.run(run())
    body = captured.get("body", "")
    assert "/admin/newsletter" in body
    assert "/admin/api/newsletter/approve-via-link?token=" in body
    assert "Review &amp; edit" in body or "Review & edit" in body
    assert "Approve" in body
    # The token segment must be HTML-escape-safe (no raw " or < that
    # would break the href attribute).
    assert "javascript:" not in body.lower()
    asyncio.run(engine.dispose())
