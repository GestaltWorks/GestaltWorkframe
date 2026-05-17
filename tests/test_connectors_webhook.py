import hashlib
import hmac

from fastapi.testclient import TestClient

from api.main import app
from core.connector_registry import connector_registry
from gestalt_connector_protocol import ConnectorConfig, Document, SourceMetadata, WebhookRequest, WebhookResult


class AcceptingConnector:
    async def webhook_handler(self, config: ConnectorConfig, request: WebhookRequest) -> WebhookResult:
        return WebhookResult(
            accepted=True,
            documents=(Document(doc_id="webhook-doc", source=SourceMetadata(connector_id=config.connector_id, source_type="webhook", external_id="webhook-doc"), body_text="Webhook body"),),
            message="accepted",
        )


class RejectingConnector:
    async def webhook_handler(self, config: ConnectorConfig, request: WebhookRequest) -> WebhookResult:
        return WebhookResult(accepted=False, message="bad payload")


class CapturingConnector(AcceptingConnector):
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    async def webhook_handler(self, config: ConnectorConfig, request: WebhookRequest) -> WebhookResult:
        self.headers = dict(request.headers)
        return await super().webhook_handler(config, request)


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_connector_webhook_dispatches_to_registered_connector():
    body = b'{"event":"created"}'
    connector_registry.register("fixture", AcceptingConnector(), ConnectorConfig(connector_id="fixture", auth={"webhook_secret": "ok"}))
    response = TestClient(app).post("/connectors/webhook/fixture", headers={"x-webhook-signature": _signature("ok", body)}, content=body)
    assert response.status_code == 200
    assert response.json()["documents_emitted"] == 1


def test_connector_webhook_rejects_bad_signature():
    connector_registry.register("fixture-bad", AcceptingConnector(), ConnectorConfig(connector_id="fixture-bad", auth={"webhook_secret": "ok"}))
    response = TestClient(app).post("/connectors/webhook/fixture-bad", headers={"x-webhook-signature": "sha256=no"}, json={"event": "created"})
    assert response.status_code == 404


def test_connector_webhook_rejects_missing_auth_without_leaking_connector_id():
    connector_registry.register("fixture-no-auth", AcceptingConnector(), ConnectorConfig(connector_id="fixture-no-auth"))
    assert TestClient(app).post("/connectors/webhook/fixture-no-auth", json={"event": "created"}).status_code == 404
    assert TestClient(app).post("/connectors/webhook/fixture-no-auth", headers={"x-webhook-signature": "sha256=no"}, json={"event": "created"}).status_code == 404


def test_connector_webhook_unknown_connector_is_404():
    response = TestClient(app).post("/connectors/webhook/not-registered", json={"event": "created"})
    assert response.status_code == 404


def test_connector_webhook_rejects_invalid_json():
    body = b"{"
    connector_registry.register("fixture-json", AcceptingConnector(), ConnectorConfig(connector_id="fixture-json", auth={"webhook_secret": "ok"}))
    response = TestClient(app).post("/connectors/webhook/fixture-json", headers={"x-webhook-signature": _signature("ok", body)}, content=body)
    assert response.status_code == 400


def test_connector_webhook_accepts_hmac_signature():
    body = b'{"event":"created"}'
    secret = "ok"
    signature = _signature(secret, body)
    connector_registry.register("fixture-hmac", AcceptingConnector(), ConnectorConfig(connector_id="fixture-hmac", auth={"webhook_secret": secret}))

    response = TestClient(app).post("/connectors/webhook/fixture-hmac", headers={"x-webhook-signature": signature}, content=body)

    assert response.status_code == 200


def test_connector_webhook_returns_400_for_authenticated_rejected_payload():
    body = b'{"event":"created"}'
    connector_registry.register("fixture-reject", RejectingConnector(), ConnectorConfig(connector_id="fixture-reject", auth={"webhook_secret": "ok"}))

    response = TestClient(app).post("/connectors/webhook/fixture-reject", headers={"x-webhook-signature": _signature("ok", body)}, content=body)

    assert response.status_code == 400


def test_connector_webhook_filters_sensitive_headers_before_handler():
    body = b'{"event":"created"}'
    connector = CapturingConnector()
    connector_registry.register("fixture-headers", connector, ConnectorConfig(connector_id="fixture-headers", auth={"webhook_secret": "ok"}))

    response = TestClient(app).post(
        "/connectors/webhook/fixture-headers",
        headers={"x-webhook-signature": _signature("ok", body), "authorization": "Bearer nope", "cookie": "session=nope", "x-event-type": "created"},
        content=body,
    )

    assert response.status_code == 200
    assert "authorization" not in connector.headers
    assert "cookie" not in connector.headers
    assert connector.headers["x-event-type"] == "created"
