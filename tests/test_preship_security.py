"""Pre-ship security and correctness fixes (items 1-13).

Covers:
- Newsletter renderer XSS hardening + URL scheme allowlist (1, 2, 3)
- Per-email auto-reply cooldown (4)
- Same-origin guard for /newsletter/api/subscribe (5)
- Em dash removed from public discovery title builder (6)
- Atomic approval guards against double-send (7)
- approved_by is server-side only, ignores request body (8)
- Public /library/issues.json + /library/issues/{slug}.json (9)
- POST /newsletter/unsubscribe RFC 8058 one-click handler (10)
- Newsletter IP rate-limit scoped to newsletter rows only (11)
- Unsubscribe page does not echo subscriber email (13)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

import api.admin_discovery as api_admin_discovery
import api.main as api_main
from gestaltworkframe.core import newsletter as newsletter_module
from gestaltworkframe.core import subscribers as subscribers_module
from gestaltworkframe.core.db import (
    ContactRecord,
    DiscoveryFind,
    DiscoverySource,
    NewsletterDelivery,
    NewsletterIssue,
    Subscriber,
    SubscriberAutoReplyRecord,
)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    api_admin_discovery._discovery_run_once_last_started_at = 0.0
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preship.db'}")

    async def init() -> sessionmaker:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(init())

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            yield session

    api_main.app.dependency_overrides[api_main.get_session] = override_get_session
    # Stub outbound mail so tests never hit M365 Graph.
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    monkeypatch.setattr(subscribers_module, "send_auto_reply", AsyncMock(return_value=("sent", "engineer_v1", "")))
    # CORS_ALLOWED_ORIGINS is captured at import time. Force the test
    # set to include https://example.com so the origin guard tests
    # that send a same-origin Origin header pass cleanly regardless of
    # the host env var.
    monkeypatch.setattr(api_main, "CORS_ALLOWED_ORIGINS", frozenset({"https://example.com"}))
    return TestClient(api_main.app), engine, maker


# ---------------------------------------------------------------------------
# 1, 2, 3 - newsletter renderer XSS + URL scheme allowlist
# ---------------------------------------------------------------------------


def _issue_with_finds(finds: list[dict]) -> NewsletterIssue:
    return NewsletterIssue(
        slug="x",
        subject="Test subject",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown="",
        finds_json=json.dumps(finds),
        status="approved",
    )


def test_render_escapes_html_special_chars_in_title():
    issue = _issue_with_finds([
        {"title": '<script>alert("xss")</script>', "url": "https://example.com/a",
         "summary_text": "ok", "source_name": "src", "display_source_name": "Src"},
    ])
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/newsletter/unsubscribe?token=t",
    )
    assert "<script>alert" not in html_out
    assert "&lt;script&gt;alert" in html_out


def test_render_escapes_html_in_summary_and_source():
    """The dangerous pattern (unescaped tag opener) must not appear; the
    escaped form is what we expect to see."""
    issue = _issue_with_finds([
        {"title": "ok", "url": "https://example.com/a",
         "summary_text": '"><img src=x onerror=alert(1)>',
         "source_name": "src", "display_source_name": "Src<>"},
    ])
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    # The unescaped `<img` tag opener must be absent (escaping replaces
    # `<` with `&lt;`). The literal text "onerror=" may appear in the
    # ESCAPED body, which is harmless because it's text-content, not an
    # attribute; we assert the dangerous tag opener instead.
    assert "<img" not in html_out
    # Source string with brackets is escaped.
    assert "Src&lt;&gt;" in html_out
    # Escape representation of the input present (defense-in-depth check).
    assert "&lt;img" in html_out or "&quot;&gt;&lt;img" in html_out


def test_render_drops_javascript_url_scheme_in_card_href():
    issue = _issue_with_finds([
        {"title": "ok", "url": "javascript:alert(1)",
         "summary_text": "ok", "source_name": "src", "display_source_name": "Src"},
    ])
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    assert "javascript:" not in html_out
    # Disallowed schemes collapse to "#"
    assert 'href="#"' in html_out


def test_render_drops_data_url_scheme_in_card_href():
    issue = _issue_with_finds([
        {"title": "ok", "url": "data:text/html,<script>alert(1)</script>",
         "summary_text": "ok", "source_name": "src"},
    ])
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    assert "data:" not in html_out


def test_render_keeps_legitimate_http_https_urls():
    issue = _issue_with_finds([
        {"title": "ok", "url": "https://example.com/path?q=1",
         "summary_text": "ok", "source_name": "src", "display_source_name": "Src"},
    ])
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    assert "https://example.com/path?q=1" in html_out


def test_editorial_markdown_link_attribute_escape():
    """A `javascript:` URL in operator markdown must not produce a
    clickable link with that scheme. Our renderer only matches https?://
    in the markdown regex, so `javascript:` URLs are emitted as escaped
    text — visible as a string in the body, but never as an href."""
    issue = NewsletterIssue(
        slug="x", subject="S",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown='[click](javascript:alert(1))',
        finds_json="[]",
        status="approved",
    )
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    # The dangerous pattern is `href="javascript:` — that is the only
    # form that actually runs script when clicked. Plain text appearance
    # of "javascript:" inside a paragraph is harmless.
    assert 'href="javascript:' not in html_out
    assert "href='javascript:" not in html_out


def test_editorial_markdown_link_with_attribute_breaking_url_is_safe():
    """Even if an operator writes [text](https://x.com") with a stray
    quote, the renderer must escape it so it cannot break out of the
    href attribute."""
    issue = NewsletterIssue(
        slug="x", subject="S",
        period_start=datetime.now(timezone.utc) - timedelta(days=10),
        period_end=datetime.now(timezone.utc),
        editorial_markdown='[click](https://example.com/"onclick=alert(1))',
        finds_json="[]",
        status="approved",
    )
    html_out = newsletter_module.render_issue_html(
        issue, unsubscribe_url="https://example.com/u",
    )
    # The href attribute is opened with a `"`; any literal `"` in the
    # URL must be escaped so it cannot terminate the attribute.
    assert 'onclick=alert' not in html_out or "&quot;onclick=alert" in html_out


def test_render_plain_strips_disallowed_schemes_from_text_body():
    issue = _issue_with_finds([
        {"title": "ok", "url": "javascript:alert(1)",
         "summary_text": "ok", "source_name": "src"},
    ])
    plain = newsletter_module.render_issue_plain(
        issue, unsubscribe_url="https://example.com/u",
    )
    assert "javascript:" not in plain


def test_render_linkedin_strips_disallowed_schemes():
    issue = _issue_with_finds([
        {"title": "ok", "url": "javascript:alert(1)",
         "source_name": "src", "display_source_name": "Src"},
    ])
    post = newsletter_module.render_issue_linkedin(issue)
    assert "javascript:" not in post


# ---------------------------------------------------------------------------
# 4 - per-email auto-reply cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoreply_cooldown_suppresses_second_send_within_window(tmp_path, monkeypatch):
    """Two calls to subscribe_and_reply for the same email within the
    cooldown window should only trigger ONE outbound auto-reply. The
    Subscriber row is updated both times (re-opt-in semantics) and an
    audit row is written both times, but the second audit row carries
    the cooldown template id so we can see suppression happened."""
    # Use the helper directly to keep this test purely async and free
    # of TestClient (which would create a nested event loop).
    send_mock = AsyncMock(return_value=("sent", "engineer_v1", ""))
    monkeypatch.setattr(subscribers_module, "send_auto_reply", send_mock)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cooldown.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed ContactRecords so the audit rows have a valid FK.
    contact_ids: list[str] = []
    async with maker() as session:
        for i in range(2):
            cr = ContactRecord(
                role="automation_engineer",
                name="Cool Down",
                email="cooldown@example.com",
                data="{}",
                ip_address="203.0.113.50",
            )
            session.add(cr)
            await session.flush()
            contact_ids.append(cr.id)
        await session.commit()

    for contact_id in contact_ids:
        async with maker() as session:
            await subscribers_module.subscribe_and_reply(
                session,
                name="Cool Down",
                email="cooldown@example.com",
                role="automation_engineer",
                contact_id=contact_id,
            )

    # Exactly one outbound mail. Second submission was suppressed by cooldown.
    assert send_mock.await_count == 1

    # Both audit rows exist; second one carries cooldown template id.
    async with maker() as session:
        rows = (await session.execute(select(SubscriberAutoReplyRecord))).scalars().all()
    templates = sorted(r.template for r in rows)
    assert "engineer_v1" in templates
    assert "cooldown" in templates
    await engine.dispose()


# ---------------------------------------------------------------------------
# 5 - origin guard covers /newsletter/api/subscribe
# ---------------------------------------------------------------------------


def test_newsletter_subscribe_rejects_disallowed_origin(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        r = client.post(
            "/newsletter/api/subscribe",
            headers={
                "x-forwarded-for": "203.0.113.60",
                "origin": "https://evil.example",
            },
            json={"name": "X", "email": "x@example.com", "role": "automation_engineer"},
        )
        assert r.status_code == 403
        assert "origin" in r.text.lower()
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_newsletter_subscribe_accepts_allowed_origin(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        r = client.post(
            "/newsletter/api/subscribe",
            headers={
                "x-forwarded-for": "203.0.113.61",
                "origin": "https://example.com",
            },
            json={"name": "Y", "email": "y@example.com", "role": "automation_engineer"},
        )
        assert r.status_code == 201
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# 6 - github_repo title no em dash
# ---------------------------------------------------------------------------


def test_github_repo_release_title_uses_colon_not_em_dash():
    """Public-facing newsletter card titles must not carry em dashes."""
    from pathlib import Path
    source = Path("gestaltworkframe/core/discovery_handlers/github_repo.py").read_text(encoding="utf-8")
    assert "—" not in source
    # And the actual title format string uses a colon now.
    assert '{owner_repo}: {title}' in source


# ---------------------------------------------------------------------------
# 7 - atomic approval double-send guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_race_second_call_409s_and_does_not_double_send(tmp_path, monkeypatch):
    """Two concurrent approve calls: first succeeds and sends, second
    must raise ValueError so the API layer returns 409. Distribution
    must NOT happen twice."""
    monkeypatch.setattr(newsletter_module, "send_internal_email", AsyncMock(return_value="sent"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await newsletter_module.compose_pending_issue(session, force=True)
        issue_id = result.issue.id
        session.add(Subscriber(email="a@example.com", name="A", source_role="student", topics="general"))
        await session.commit()

    # First approval succeeds.
    async with maker() as session:
        await newsletter_module.approve_and_distribute(session, issue_id, approved_by="t")

    # Second approval on the same issue must raise.
    async with maker() as session:
        with pytest.raises(ValueError):
            await newsletter_module.approve_and_distribute(session, issue_id, approved_by="t")

    # Only one email delivery row.
    async with maker() as session:
        deliveries = (await session.execute(
            select(NewsletterDelivery).where(NewsletterDelivery.channel == "email")
        )).scalars().all()
    assert len(deliveries) == 1


# ---------------------------------------------------------------------------
# 8 - approved_by server-side only
# ---------------------------------------------------------------------------


def test_admin_newsletter_approve_ignores_request_body_approved_by(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        # Compose an editorial-only draft.
        draft = client.post(
            "/admin/api/newsletter/draft",
            json={"force": True},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert draft.status_code == 200
        issue_id = draft.json()["issue"]["id"]

        # Try to forge approved_by via the body.
        approve = client.post(
            f"/admin/api/newsletter/issues/{issue_id}/approve",
            json={"approved_by": "FAKE_USER"},
            headers={"X-Admin-Token": "test-admin"},
        )
        assert approve.status_code == 200

        async def fetch_approver() -> str:
            async with maker() as session:
                row = (await session.execute(select(NewsletterIssue).where(NewsletterIssue.id == issue_id))).scalar_one()
                return row.approved_by

        approved_by = asyncio.run(fetch_approver())
        assert approved_by == "admin"
        assert approved_by != "FAKE_USER"
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# 9 - public /library/issues.json and /library/issues/{slug}.json
# ---------------------------------------------------------------------------


def test_library_issues_json_only_lists_sent_issues(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                # Three issues in three states. Each one needs a unique
                # display_label now that the UNIQUE constraint is in
                # place; "0a" / "0b" pre-ship, "1" once shipped.
                session.add(NewsletterIssue(
                    slug="draft-1", display_label="0a",
                    subject="Draft", status="awaiting_approval",
                    period_start=datetime.now(timezone.utc) - timedelta(days=10),
                    period_end=datetime.now(timezone.utc),
                    finds_json="[]",
                ))
                session.add(NewsletterIssue(
                    slug="sent-1", ship_number=1, display_label="1",
                    subject="Sent issue", status="sent",
                    period_start=datetime.now(timezone.utc) - timedelta(days=10),
                    period_end=datetime.now(timezone.utc),
                    sent_at=datetime.now(timezone.utc),
                    finds_json="[]",
                ))
                session.add(NewsletterIssue(
                    slug="skipped-1", display_label="0b",
                    subject="", status="skipped",
                    period_start=datetime.now(timezone.utc) - timedelta(days=10),
                    period_end=datetime.now(timezone.utc),
                    finds_json="[]",
                ))
                await session.commit()
        asyncio.run(seed())

        r = client.get("/library/issues.json")
        assert r.status_code == 200
        slugs = [i["slug"] for i in r.json()["issues"]]
        assert slugs == ["sent-1"]
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_issue_detail_404s_for_draft(tmp_path, monkeypatch):
    """Draft / awaiting_approval issues must not be publicly readable
    by slug, even if someone guesses the slug."""
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                session.add(NewsletterIssue(
                    slug="secret-draft", subject="Internal", status="awaiting_approval",
                    period_start=datetime.now(timezone.utc) - timedelta(days=10),
                    period_end=datetime.now(timezone.utc),
                    editorial_markdown="Internal-only editorial",
                    finds_json="[]",
                ))
                await session.commit()
        asyncio.run(seed())

        r = client.get("/library/issues/secret-draft.json")
        assert r.status_code == 404
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_issue_detail_404s_for_unknown_slug(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        r = client.get("/library/issues/does-not-exist.json")
        assert r.status_code == 404
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_library_issue_detail_returns_html_for_sent_issue(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        async def seed():
            async with maker() as session:
                session.add(NewsletterIssue(
                    slug="published-1", subject="Hello world", status="sent",
                    period_start=datetime.now(timezone.utc) - timedelta(days=10),
                    period_end=datetime.now(timezone.utc),
                    sent_at=datetime.now(timezone.utc),
                    editorial_markdown="Body text.",
                    finds_json="[]",
                ))
                await session.commit()
        asyncio.run(seed())

        r = client.get("/library/issues/published-1.json")
        assert r.status_code == 200
        body = r.json()
        assert body["issue"]["slug"] == "published-1"
        assert body["issue"]["subject"] == "Hello world"
        # html field present and contains the subject.
        assert "Hello world" in body["issue"]["html"]
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# 10 - POST /newsletter/unsubscribe RFC 8058 One-Click
# ---------------------------------------------------------------------------


def test_post_unsubscribe_one_click_marks_subscriber_unsubscribed(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        # Create a subscriber.
        client.post(
            "/contact",
            headers={"x-forwarded-for": "203.0.113.70", "origin": "https://example.com"},
            json={
                "name": "Click Once", "email": "oneclick@example.com",
                "role": "automation_engineer", "platforms": ["Automation"],
                "project_types": ["Production workflows"], "llms": [], "library_consent": True,
            },
        )
        async def fetch_token() -> str:
            async with maker() as session:
                return (await session.execute(select(Subscriber))).scalars().one().unsubscribe_token

        token = asyncio.run(fetch_token())

        # RFC 8058 mail clients POST without an Origin header. Body is
        # form-encoded `List-Unsubscribe=One-Click`; we ignore it.
        r = client.post(
            f"/newsletter/unsubscribe?token={token}",
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "unsubscribed"

        async def fetch_unsub_state():
            async with maker() as session:
                return (await session.execute(select(Subscriber))).scalars().one().unsubscribed_at

        assert asyncio.run(fetch_unsub_state()) is not None
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_post_unsubscribe_missing_token_is_400(tmp_path, monkeypatch):
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        r = client.post("/newsletter/unsubscribe", data="")
        assert r.status_code == 400
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# 11 - newsletter rate-limit does NOT penalize contact-form rows
# ---------------------------------------------------------------------------


def test_newsletter_rate_limit_ignores_contact_form_rows(tmp_path, monkeypatch):
    """A visitor who submits the detailed /contact form should still be
    able to subscribe via /newsletter/api/subscribe from the same IP."""
    client, engine, _maker = _client(tmp_path, monkeypatch)
    try:
        # Five /contact submissions saturate the contact-form rate limit
        # but are NOT signup_source=newsletter rows.
        for i in range(5):
            r = client.post(
                "/contact",
                headers={"x-forwarded-for": "203.0.113.80", "origin": "https://example.com"},
                json={
                    "name": f"User {i}", "email": f"u{i}@example.com",
                    "role": "automation_engineer", "platforms": ["Automation"],
                    "project_types": ["Production workflows"], "llms": [], "library_consent": True,
                },
            )
            assert r.status_code == 201, r.text
        # Newsletter signup from the same IP must still succeed because
        # the newsletter rate-limit filters on signup_source=newsletter.
        r = client.post(
            "/newsletter/api/subscribe",
            headers={"x-forwarded-for": "203.0.113.80", "origin": "https://example.com"},
            json={"name": "Sep", "email": "sep@example.com", "role": "student"},
        )
        assert r.status_code == 201, r.text
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# 13 - unsubscribe page does not echo subscriber email
# ---------------------------------------------------------------------------


def test_unsubscribe_get_page_does_not_render_subscriber_email(tmp_path, monkeypatch):
    client, engine, maker = _client(tmp_path, monkeypatch)
    try:
        client.post(
            "/contact",
            headers={"x-forwarded-for": "203.0.113.90", "origin": "https://example.com"},
            json={
                "name": "Email Privacy", "email": "privacy-test-12345@example.com",
                "role": "student", "experience_level": "Just curious",
                "learning_topics": ["Workflow design"], "format_pref": ["Self-paced articles"],
            },
        )
        async def fetch_token() -> str:
            async with maker() as session:
                return (await session.execute(select(Subscriber))).scalars().one().unsubscribe_token

        token = asyncio.run(fetch_token())

        r = client.get(f"/newsletter/unsubscribe?token={token}")
        assert r.status_code == 200
        # The success page must NOT contain the subscriber's email
        # (privacy: forwarded link + screenshot leak).
        assert "privacy-test-12345@example.com" not in r.text
        # And must NOT contain the substring of the local-part either.
        assert "privacy-test-12345" not in r.text
        # Still confirms unsubscribe happened.
        assert "unsubscribed" in r.text.lower()
    finally:
        api_main.app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
