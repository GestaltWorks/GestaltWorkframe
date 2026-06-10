"""Phase 1: contact-form auto-reply + subscriber list tests.

Covers:
- Subscriber row creation on form submit
- Auto-reply template selection by role (student / engineer / interested_party)
- Implicit re-subscribe on re-submit after unsubscribe
- Unsubscribe-by-token idempotent flow
- Public /newsletter/unsubscribe endpoint returns HTML success page

External email sending is mocked. Tests run against an isolated SQLite
file the same way tests/test_contact.py does so we don't touch the
production DB or the M365 Graph API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from api import contact, newsletter_public
from gestaltworkframe.core import contact_autoreply
from gestaltworkframe.core import subscribers as subscribers_module
from gestaltworkframe.core.db import Subscriber, SubscriberAutoReplyRecord


async def _test_app(tmp_path, monkeypatch):
    """Spin up an isolated FastAPI test app with mocked email sends."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'autoreply.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = FastAPI()
    app.middleware("http")(contact.contact_body_size_limit)
    app.include_router(contact.router)
    app.include_router(newsletter_public.router)

    async def override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[contact.get_session] = override_session
    app.dependency_overrides[newsletter_public.get_session] = override_session

    # Mock the internal handoff email (already tested elsewhere).
    monkeypatch.setattr(contact, "send_contact_notification", AsyncMock(return_value="sent"))
    # Mock send_auto_reply where the shared subscribe_and_reply helper
    # actually invokes it (core.subscribers re-imports it). Both the
    # /contact and /newsletter/subscribe paths route through the helper,
    # so this one patch covers both entry points.
    auto_reply_mock = AsyncMock(return_value=("sent", "student_v1", ""))
    monkeypatch.setattr(subscribers_module, "send_auto_reply", auto_reply_mock)
    return app, maker, engine, auto_reply_mock


async def _post_contact(client: httpx.AsyncClient, role: str, **fields):
    """Helper to submit the right payload shape per role."""

    base = {"name": "Test User", "email": "test@example.com", "role": role}
    role_defaults = {
        "student": {
            "experience_level": "Just curious",
            "learning_topics": ["Workflow design"],
            "format_pref": ["Self-paced articles"],
        },
        "automation_engineer": {
            "platforms": ["Automation"],
            "project_types": ["Production workflows"],
            "llms": [],
            "library_consent": True,
        },
        "interested_party": {
            "company": "Acme",
            "dream_automations": ["Ticket routing"],
            "automation_journey": "Just starting",
            "timeline": "Soon",
        },
    }
    payload = {**base, **role_defaults.get(role, {}), **fields}
    return await client.post(
        "/contact",
        headers={"x-forwarded-for": "203.0.113.10"},
        json=payload,
    )


# ---------------------------------------------------------------------------
# Subscriber creation on form submit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contact_submit_creates_subscriber_row(tmp_path, monkeypatch):
    app, maker, _engine, auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _post_contact(client, "student", email="learner@example.com", name="Lee Learner")

    assert response.status_code == 201
    async with maker() as session:
        subs = (await session.execute(select(Subscriber))).scalars().all()
    assert len(subs) == 1
    assert subs[0].email == "learner@example.com"
    assert subs[0].name == "Lee Learner"
    assert subs[0].source_role == "student"
    assert "edu" in subs[0].topics
    assert subs[0].unsubscribed_at is None
    assert subs[0].unsubscribe_token  # uuid populated
    auto_reply.assert_awaited_once()
    # The auto-reply send received the same token persisted on the row.
    args = auto_reply.await_args.args
    assert args[0] == "student"
    assert args[2] == "learner@example.com"
    assert args[3] == subs[0].unsubscribe_token


@pytest.mark.asyncio
async def test_engineer_role_gets_auto_topic_tag(tmp_path, monkeypatch):
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _post_contact(client, "automation_engineer", email="dev@example.com", name="Dev Eng")

    async with maker() as session:
        sub = (await session.execute(select(Subscriber))).scalars().one()
    assert "auto" in sub.topics
    assert "general" in sub.topics
    assert sub.source_role == "automation_engineer"


@pytest.mark.asyncio
async def test_interested_party_gets_service_topic_tag(tmp_path, monkeypatch):
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _post_contact(client, "interested_party", email="buyer@example.com", name="B Buyer")

    async with maker() as session:
        sub = (await session.execute(select(Subscriber))).scalars().one()
    assert "service" in sub.topics


@pytest.mark.asyncio
async def test_autoreply_audit_row_persisted(tmp_path, monkeypatch):
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _post_contact(client, "student", email="audit@example.com", name="A Test")

    async with maker() as session:
        audits = (await session.execute(select(SubscriberAutoReplyRecord))).scalars().all()
    assert len(audits) == 1
    assert audits[0].role == "student"
    assert audits[0].template == "student_v1"
    assert audits[0].status == "sent"


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------

def test_unknown_role_falls_back_to_generic_template():
    reply = contact_autoreply.compose_auto_reply("alien_overlord", "Visitor", "tok-x")
    assert reply.template_id == "generic_v1"
    assert "newsletter" in reply.html.lower()


def test_no_em_dashes_in_any_template():
    """Brand voice rule: no em dashes. The CLAUDE.md voice rules apply to
    every customer-facing template."""
    for role in ("student", "automation_engineer", "interested_party", "unknown"):
        reply = contact_autoreply.compose_auto_reply(role, "Name", "tok")
        assert "—" not in reply.html, f"em dash in {role} html"
        assert "—" not in reply.plain, f"em dash in {role} plain"


# ---------------------------------------------------------------------------
# Unsubscribe flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsubscribe_endpoint_marks_subscriber_unsubscribed(tmp_path, monkeypatch):
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _post_contact(client, "student", email="bye@example.com")
        async with maker() as session:
            sub = (await session.execute(select(Subscriber))).scalars().one()
            token = sub.unsubscribe_token

        response = await client.get(f"/newsletter/unsubscribe?token={token}")
        assert response.status_code == 200
        assert "unsubscribed" in response.text.lower()

    async with maker() as session:
        sub_after = (await session.execute(select(Subscriber))).scalars().one()
    assert sub_after.unsubscribed_at is not None
    assert sub_after.name == ""
    assert sub_after.source_role == ""
    assert sub_after.topics == ""


@pytest.mark.asyncio
async def test_unsubscribe_endpoint_idempotent(tmp_path, monkeypatch):
    """Clicking the link twice still reports success and doesn't error."""
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await _post_contact(client, "student", email="twice@example.com")
        async with maker() as session:
            token = (await session.execute(select(Subscriber))).scalars().one().unsubscribe_token

        first = await client.get(f"/newsletter/unsubscribe?token={token}")
        second = await client.get(f"/newsletter/unsubscribe?token={token}")

        assert first.status_code == 200
        assert second.status_code == 200
        assert "unsubscribed" in second.text.lower()


@pytest.mark.asyncio
async def test_unsubscribe_unknown_token_does_not_leak_enumeration(tmp_path, monkeypatch):
    """Unknown tokens get the same success page as valid ones so a
    scanner cannot enumerate which tokens correspond to real subscribers."""
    app, _maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/newsletter/unsubscribe?token=definitely-not-a-real-token")
    assert response.status_code == 200
    assert "unsubscribed" in response.text.lower()


@pytest.mark.asyncio
async def test_unsubscribe_missing_token_returns_400(tmp_path, monkeypatch):
    app, _maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/newsletter/unsubscribe")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_resubmit_after_unsubscribe_re_opts_in_with_new_token(tmp_path, monkeypatch):
    app, maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First submit - creates subscriber.
        await _post_contact(client, "student", email="returner@example.com", name="R One")
        async with maker() as session:
            sub = (await session.execute(select(Subscriber))).scalars().one()
            first_token = sub.unsubscribe_token

        # Unsubscribe.
        await client.get(f"/newsletter/unsubscribe?token={first_token}")
        async with maker() as session:
            sub_unsub = (await session.execute(select(Subscriber))).scalars().one()
            assert sub_unsub.unsubscribed_at is not None

        # Resubmit form (this time as engineer with a different role) - should re-opt-in.
        await _post_contact(
            client,
            "automation_engineer",
            email="returner@example.com",
            name="R One",
        )

    async with maker() as session:
        sub_after = (await session.execute(select(Subscriber))).scalars().one()
    assert sub_after.unsubscribed_at is None
    assert sub_after.unsubscribe_token != first_token  # token rotated on re-subscribe
    assert sub_after.source_role == "automation_engineer"
    # Unsubscribe minimizes old segmentation data; re-subscribe records the new visit only.
    assert "edu" not in sub_after.topics
    assert "auto" in sub_after.topics


# ---------------------------------------------------------------------------
# Lightweight POST /newsletter/subscribe (shares the upsert + auto-reply
# path with the /contact form via core.subscribers.subscribe_and_reply).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newsletter_subscribe_endpoint_creates_subscriber_and_sends_reply(tmp_path, monkeypatch):
    app, maker, _engine, auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/newsletter/api/subscribe",
            headers={"x-forwarded-for": "203.0.113.99"},
            json={
                "name": "Jane Tester",
                "email": "jane@example.com",
                "company": "Acme",
                "role": "automation_engineer",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "subscribed"
    # ContactRecord exists with signup_source=newsletter; Subscriber row
    # exists with the engineer topics; the auto-reply mock was called
    # with the engineer template.
    from gestaltworkframe.core.db import ContactRecord, Subscriber
    async with maker() as session:
        records = (await session.execute(select(ContactRecord))).scalars().all()
        subs = (await session.execute(select(Subscriber))).scalars().all()
    assert len(records) == 1
    assert records[0].role == "automation_engineer"
    assert "newsletter" in records[0].data
    assert "Acme" in records[0].data
    assert len(subs) == 1
    assert subs[0].email == "jane@example.com"
    assert "auto" in subs[0].topics
    auto_reply.assert_awaited_once()
    args = auto_reply.await_args.args
    assert args[0] == "automation_engineer"


@pytest.mark.asyncio
async def test_newsletter_subscribe_rejects_invalid_role(tmp_path, monkeypatch):
    app, _maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/newsletter/api/subscribe",
            headers={"x-forwarded-for": "203.0.113.100"},
            json={"name": "X", "email": "x@example.com", "company": "", "role": "alien_overlord"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_newsletter_subscribe_rejects_invalid_email(tmp_path, monkeypatch):
    app, _maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/newsletter/api/subscribe",
            headers={"x-forwarded-for": "203.0.113.101"},
            json={"name": "X", "email": "not-an-email", "role": "student"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_newsletter_subscribe_rate_limited_by_ip(tmp_path, monkeypatch):
    """Reuses the ContactRecord IP-rate-limit window for newsletter
    signups (10 per 24h per IP)."""
    app, _maker, _engine, _auto_reply = await _test_app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 10 signups from the same IP should succeed.
        for i in range(10):
            r = await client.post(
                "/newsletter/api/subscribe",
                headers={"x-forwarded-for": "203.0.113.222"},
                json={"name": f"User {i}", "email": f"u{i}@example.com", "role": "student"},
            )
            assert r.status_code == 201, r.text
        # 11th should be rate-limited.
        over = await client.post(
            "/newsletter/api/subscribe",
            headers={"x-forwarded-for": "203.0.113.222"},
            json={"name": "Eleventh", "email": "u11@example.com", "role": "student"},
        )
    assert over.status_code == 429


def test_subscribe_and_reply_helper_is_shared_by_both_paths():
    """No duplicated subscribe+reply code path: /contact and
    /newsletter/subscribe both call gestaltworkframe.core.subscribers.subscribe_and_reply.
    Guards against future drift where one entry point silently stops
    persisting Subscriber rows or sending auto-replies."""
    from pathlib import Path
    contact = Path("api/contact.py").read_text(encoding="utf-8")
    nl = Path("api/newsletter_public.py").read_text(encoding="utf-8")
    helper = Path("gestaltworkframe/core/subscribers.py").read_text(encoding="utf-8")

    assert "from gestaltworkframe.core.subscribers import subscribe_and_reply" in contact
    assert "subscribe_and_reply" in nl
    assert "async def subscribe_and_reply" in helper
    # The legacy private _subscribe_and_reply on api/contact.py must be gone
    # so future readers don't think there's a second path to maintain.
    assert "_subscribe_and_reply" not in contact
