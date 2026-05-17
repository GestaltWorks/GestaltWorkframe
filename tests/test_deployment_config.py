from fastapi.testclient import TestClient
import pytest

from api.main import app
from core.deployment_config import DeploymentConfig, get_deployment_config, reload_deployment_config
import core.deployment_config as deployment_config_mod


def test_default_deployment_config_loads_test_brand(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT_ID", raising=False)
    reload_deployment_config()
    config = get_deployment_config()
    assert config.deployment_id == "test-brand"
    assert config.brand.name == "Northstar Automation Lab"
    assert config.identity.short_name == "Northstar"


def test_test_brand_renders_identity(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    config = get_deployment_config()
    assert config.brand.name == "Northstar Automation Lab"
    assert config.identity.public_email == "hello@northstar.example"


def test_public_deployment_config_redacts_admin_fields(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    response = TestClient(app).get("/api/deployment-config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brand"]["name"] == "Northstar Automation Lab"
    assert "audience_visibility" not in payload["newsletter"]
    assert "connectors" not in payload
    assert "bot_persona" not in payload["identity"]


def test_deployment_id_rejects_path_traversal(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "../../etc")
    with pytest.raises(ValueError):
        reload_deployment_config()


def test_public_payload_scrubs_secret_like_copy_keys():
    payload = DeploymentConfig(copy={"landing": {"hero": "safe", "api_key": "unsafe", "nested": {"token": "unsafe"}}}).public_payload()

    assert payload["copy"]["landing"]["hero"] == "safe"
    assert "api_key" not in payload["copy"]["landing"]
    assert "token" not in payload["copy"]["landing"]["nested"]


def test_admin_payload_redacts_connector_auth_fields():
    payload = DeploymentConfig(connectors=[{"id": "c1", "auth": {"api_key": "unsafe"}, "settings": {"base_url": "https://example.com"}}]).admin_payload()

    assert payload["connectors"][0]["auth"] == "[REDACTED]"
    assert payload["connectors"][0]["settings"]["base_url"] == "https://example.com"


def test_deployment_root_rejects_symlink_escape(tmp_path, monkeypatch):
    deployments = tmp_path / "deployments"
    deployments.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = deployments / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    monkeypatch.setattr(deployment_config_mod, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError):
        deployment_config_mod._deployment_root("linked")


def test_deployment_root_rejects_invalid_direct_caller(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment_config_mod, "REPO_ROOT", tmp_path)

    with pytest.raises(ValueError):
        deployment_config_mod._deployment_root("../bad")
