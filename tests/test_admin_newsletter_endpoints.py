"""Direct unit tests for api/admin_newsletter.py endpoint error mapping.

The endpoints translate core-layer LookupError -> 404 and ValueError -> 409,
and the request models bound the schedule window. The full-stack newsletter
tests drive the happy paths; this reaches the exception branches and the
validators directly by calling the endpoint coroutines with a stub session and
monkeypatched core helpers — no app, no DB, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import gestaltworkframe.api.admin_newsletter as nl
import gestaltworkframe.core.newsletter as newsletter_core

_SESSION = SimpleNamespace()


def _raise(exc):
    async def _inner(*_a, **_kw):
        raise exc

    return _inner


async def _return(value):
    return value


# --- request-model validators ----------------------------------------------


def test_approval_request_rejects_far_future():
    with pytest.raises(ValidationError):
        nl.ApprovalRequest(scheduled_send_at=datetime.now(timezone.utc) + timedelta(days=400))


def test_approval_request_attaches_utc_to_naive_datetime():
    soon = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    req = nl.ApprovalRequest(scheduled_send_at=soon)
    assert req.scheduled_send_at.tzinfo is not None


def test_approval_request_allows_none():
    assert nl.ApprovalRequest().scheduled_send_at is None


def test_create_issue_request_rejects_far_future():
    with pytest.raises(ValidationError):
        nl.CreateIssueRequest(target_send_at=datetime.now(timezone.utc) + timedelta(days=400))


def test_create_issue_request_attaches_utc_to_naive_datetime():
    soon = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    req = nl.CreateIssueRequest(target_send_at=soon)
    assert req.target_send_at.tzinfo is not None


# --- list / detail / run-cycle happy paths ---------------------------------


async def test_list_returns_issues(monkeypatch):
    monkeypatch.setattr(nl, "list_issues", lambda *a, **k: _return([{"id": "1"}]))
    out = await nl.admin_newsletter_list(limit=10, _=None, session=_SESSION)
    assert out == {"issues": [{"id": "1"}]}


async def test_detail_404_when_missing(monkeypatch):
    monkeypatch.setattr(nl, "get_issue_detail", lambda *a, **k: _return(None))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_detail("missing", _=None, session=_SESSION)
    assert exc.value.status_code == 404


async def test_detail_returns_issue(monkeypatch):
    monkeypatch.setattr(nl, "get_issue_detail", lambda *a, **k: _return({"id": "1"}))
    out = await nl.admin_newsletter_detail("1", _=None, session=_SESSION)
    assert out == {"issue": {"id": "1"}}


async def test_run_cycle_returns_summary(monkeypatch):
    monkeypatch.setattr(nl, "run_scheduled_cycle", lambda *a, **k: _return({"composed": False}))
    out = await nl.admin_newsletter_run_cycle(_=None, session=_SESSION)
    assert out == {"summary": {"composed": False}}


# --- editorial / approve / cancel error mapping ----------------------------


async def test_save_editorial_404_on_lookup_error(monkeypatch):
    monkeypatch.setattr(nl, "update_editorial", _raise(LookupError("gone")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_save_editorial(
            "x", nl.EditorialRequest(editorial_markdown="hi"), _=None, session=_SESSION
        )
    assert exc.value.status_code == 404


async def test_save_editorial_409_on_value_error(monkeypatch):
    monkeypatch.setattr(nl, "update_editorial", _raise(ValueError("already sent")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_save_editorial(
            "x", nl.EditorialRequest(editorial_markdown="hi"), _=None, session=_SESSION
        )
    assert exc.value.status_code == 409


async def test_approve_404_on_lookup_error(monkeypatch):
    monkeypatch.setattr(nl, "approve_and_schedule", _raise(LookupError("gone")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_approve("x", nl.ApprovalRequest(), _=None, session=_SESSION)
    assert exc.value.status_code == 404


async def test_approve_409_on_value_error(monkeypatch):
    monkeypatch.setattr(nl, "approve_and_schedule", _raise(ValueError("bad state")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_approve("x", nl.ApprovalRequest(), _=None, session=_SESSION)
    assert exc.value.status_code == 409


async def test_cancel_send_404_on_lookup_error(monkeypatch):
    monkeypatch.setattr(nl, "cancel_scheduled_send", _raise(LookupError("gone")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_cancel_send("x", nl.CancelSendRequest(), _=None, session=_SESSION)
    assert exc.value.status_code == 404


async def test_cancel_send_409_on_value_error(monkeypatch):
    monkeypatch.setattr(nl, "cancel_scheduled_send", _raise(ValueError("already sent")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_cancel_send("x", nl.CancelSendRequest(), _=None, session=_SESSION)
    assert exc.value.status_code == 409


# --- delete / unpublish (helpers imported inside the function) --------------


async def test_delete_issue_404_on_lookup_error(monkeypatch):
    monkeypatch.setattr(newsletter_core, "delete_issue", _raise(LookupError("gone")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_delete_issue("x", _=None, session=_SESSION)
    assert exc.value.status_code == 404


async def test_unpublish_404_on_lookup_error(monkeypatch):
    monkeypatch.setattr(newsletter_core, "unpublish_issue", _raise(LookupError("gone")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_unpublish_issue("x", _=None, session=_SESSION)
    assert exc.value.status_code == 404


async def test_unpublish_409_on_value_error(monkeypatch):
    monkeypatch.setattr(newsletter_core, "unpublish_issue", _raise(ValueError("draft")))
    with pytest.raises(HTTPException) as exc:
        await nl.admin_newsletter_unpublish_issue("x", _=None, session=_SESSION)
    assert exc.value.status_code == 409


# --- approve-via-link (HTML responses, no token header) --------------------


async def test_approve_via_link_invalid_token_renders_400(monkeypatch):
    monkeypatch.setattr(nl, "verify_approval_token", lambda token: (_ for _ in ()).throw(ValueError("bad")))
    resp = await nl.admin_newsletter_approve_via_link(token="x" * 30, session=_SESSION)
    assert resp.status_code == 400
    assert b"Approval link invalid" in resp.body


async def test_approve_via_link_missing_issue_renders_404(monkeypatch):
    monkeypatch.setattr(nl, "verify_approval_token", lambda token: "issue-1")
    monkeypatch.setattr(nl, "approve_and_schedule", _raise(LookupError("gone")))
    resp = await nl.admin_newsletter_approve_via_link(token="x" * 30, session=_SESSION)
    assert resp.status_code == 404
    assert b"Issue not found" in resp.body
