import httpx
import pytest

import gestaltworkframe.core.email_service as email_service
from gestaltworkframe.core.deployment_config import reload_deployment_config
from gestaltworkframe.core.email_service import _build_html


def test_contact_email_uses_current_services_url() -> None:
    html = _build_html(
        "interested_party",
        "A User",
        "user@example.com",
        {"problem_statement": "Need help with routing."},
    )

    assert "<meta charset='utf-8'>" in html
    assert "Contact intake" in html
    assert "services-RnD" not in html


def test_contact_email_escapes_user_supplied_values() -> None:
    html = _build_html(
        "interested_party",
        "<script>A User</script>",
        "user@example.com",
        {"notes": "<b>raw html</b>"},
    )

    assert "<script>" not in html
    assert "&lt;script&gt;A User&lt;/script&gt;" in html
    assert "&lt;b&gt;raw html&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# MS365 Graph send path (no network)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


class _FakeClient:
    """Records POSTs; returns a token for the oauth call and a status for sendMail."""

    instances: list["_FakeClient"] = []

    def __init__(self, *_args, **_kwargs):
        self.calls: list[tuple[str, dict]] = []
        self.send_status = 202
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "oauth2" in url:
            return _FakeResp(200, {"access_token": "tok-abc"})
        return _FakeResp(self.send_status)


@pytest.fixture
def fake_graph(monkeypatch):
    _FakeClient.instances.clear()
    holder = {"status": 202}

    def factory(*a, **k):
        client = _FakeClient(*a, **k)
        client.send_status = holder["status"]
        return client

    monkeypatch.setattr(email_service.httpx, "AsyncClient", factory)
    return holder


def _set_creds(monkeypatch):
    monkeypatch.setenv("MS365_TENANT_ID", "tenant-1")
    monkeypatch.setenv("MS365_CLIENT_ID", "client-1")
    monkeypatch.setenv("MS365_CLIENT_SECRET", "secret-1")


def test_default_sender_uses_override(monkeypatch):
    monkeypatch.setenv("MS365_SEND_AS", "ops@example.com")
    assert email_service._default_sender() == "ops@example.com"


def test_default_sender_falls_back_to_deployment_identity(monkeypatch):
    monkeypatch.delenv("MS365_SEND_AS", raising=False)
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    assert "@" in email_service._default_sender()


@pytest.mark.asyncio
async def test_send_internal_email_skipped_without_creds(monkeypatch):
    for var in ("MS365_TENANT_ID", "MS365_CLIENT_ID", "MS365_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MS365_SEND_AS", "ops@example.com")

    assert await email_service.send_internal_email("subj", "<p>hi</p>") == "skipped"


@pytest.mark.asyncio
async def test_send_internal_email_sends_when_configured(monkeypatch, fake_graph):
    _set_creds(monkeypatch)
    monkeypatch.setenv("MS365_SEND_AS", "ops@example.com")

    status = await email_service.send_internal_email(
        "Hello", "<p>body</p>", recipient="to@example.com",
        reply_to={"address": "r@example.com", "name": "R"},
    )

    assert status == "sent"
    client = _FakeClient.instances[-1]
    assert "oauth2" in client.calls[0][0]
    send_url, send_kwargs = client.calls[1]
    assert "sendMail" in send_url
    payload = send_kwargs["json"]
    assert payload["message"]["subject"] == "Hello"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "to@example.com"
    assert payload["message"]["replyTo"][0]["emailAddress"]["address"] == "r@example.com"
    assert send_kwargs["headers"]["Authorization"] == "Bearer tok-abc"


@pytest.mark.asyncio
async def test_send_internal_email_raises_on_graph_error(monkeypatch, fake_graph):
    _set_creds(monkeypatch)
    monkeypatch.setenv("MS365_SEND_AS", "ops@example.com")
    fake_graph["status"] = 500

    with pytest.raises(httpx.HTTPStatusError):
        await email_service.send_internal_email("subj", "<p>x</p>")


@pytest.mark.asyncio
async def test_send_contact_notification_sends(monkeypatch, fake_graph):
    _set_creds(monkeypatch)
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()

    status = await email_service.send_contact_notification(
        "automation_engineer", "Dana", "dana@example.com", {"company": "Acme"}
    )

    assert status == "sent"
    client = _FakeClient.instances[-1]
    _, send_kwargs = client.calls[1]
    assert send_kwargs["json"]["message"]["replyTo"][0]["emailAddress"]["address"] == "dana@example.com"
