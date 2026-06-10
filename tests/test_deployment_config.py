from fastapi.testclient import TestClient
import pytest

from gestaltworkframe.api.main import app
from gestaltworkframe.core.deployment_config import DeploymentConfig, get_deployment_config, reload_deployment_config
import gestaltworkframe.core.deployment_config as deployment_config_mod


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


# ---------------------------------------------------------------------------
# A1: RoutingConfig from routing.yaml
# ---------------------------------------------------------------------------

def test_routing_config_loads_from_yaml(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    config = get_deployment_config()
    routing = config.routing
    assert "learn" in routing.learning_signals
    assert "workflow" in routing.technical_terms
    assert "hire" in routing.direct_service_handoff_terms
    assert "i want to build" in routing.build_intent_terms


def test_routing_config_fallback_when_missing(tmp_path, monkeypatch):
    """A deployment without routing.yaml should load with all-empty RoutingConfig (use defaults)."""
    deployments = tmp_path / "deployments" / "bare"
    deployments.mkdir(parents=True)
    monkeypatch.setattr(deployment_config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DEPLOYMENT_ID", "bare")
    reload_deployment_config()
    config = get_deployment_config()
    routing = config.routing
    # All lists are empty — callers fall back to framework defaults
    assert routing.learning_signals == []
    assert routing.technical_terms == []
    assert routing.service_signals == []


# ---------------------------------------------------------------------------
# A2: PersonasConfig from personas.yaml
# ---------------------------------------------------------------------------

def test_personas_config_loads_from_yaml(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    config = get_deployment_config()
    modes = {m.id: m for m in config.personas.modes}
    assert "pipeline" in modes
    assert modes["pipeline"].name == "Engagement Advisor"
    assert modes["automator"].name == "Operations Guide"
    assert modes["educator"].name == "Learning Guide"
    # test-brand only overrides name/description — system_prompt stays empty
    assert modes["pipeline"].system_prompt == ""
    assert modes["pipeline"].allowed_tools == []


def test_personas_config_fallback_when_missing(tmp_path, monkeypatch):
    """Deployment without personas.yaml loads with empty PersonasConfig."""
    deployments = tmp_path / "deployments" / "bare"
    deployments.mkdir(parents=True)
    monkeypatch.setattr(deployment_config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DEPLOYMENT_ID", "bare")
    reload_deployment_config()
    config = get_deployment_config()
    assert config.personas.modes == []


def test_get_persona_applies_name_override(monkeypatch):
    """get_persona returns the deployment-overridden name when personas.yaml has an entry."""
    from gestaltworkframe.core.personas import get_persona
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    persona = get_persona("pipeline")
    assert persona.name == "Engagement Advisor"
    # system_prompt should still contain the framework PIPELINE text (no override)
    assert "Service Inquiry" in persona.system_prompt or "MODE:" in persona.system_prompt


def test_get_persona_fallback_when_no_override(monkeypatch):
    """get_persona returns the hardcoded default when deployment has no personas.yaml."""
    from gestaltworkframe.core.personas import get_persona, PIPELINE_PERSONA
    from gestaltworkframe.core import deployment_config as dc_mod
    tmp = __import__("tempfile").mkdtemp()
    import pathlib
    bare = pathlib.Path(tmp) / "deployments" / "bare"
    bare.mkdir(parents=True)
    monkeypatch.setattr(dc_mod, "REPO_ROOT", pathlib.Path(tmp))
    monkeypatch.setenv("DEPLOYMENT_ID", "bare")
    reload_deployment_config()
    persona = get_persona("pipeline")
    assert persona.name == PIPELINE_PERSONA.name


# ---------------------------------------------------------------------------
# A3: LibraryConfig from library.yaml
# ---------------------------------------------------------------------------

def test_library_config_loads_from_yaml(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_ID", "test-brand")
    reload_deployment_config()
    config = get_deployment_config()
    lib = config.library
    assert lib.library_id == "northstar-sample"
    assert lib.display_name == "Northstar Knowledge Library"
    assert lib.public_url == "https://northstar.example/library"
    assert lib.publish_base_branch == "main"
    assert lib.publish_target_dir == "discovery/approved"


def test_library_config_fallback_when_missing(tmp_path, monkeypatch):
    """Deployment without library.yaml loads with default LibraryConfig."""
    deployments = tmp_path / "deployments" / "bare"
    deployments.mkdir(parents=True)
    monkeypatch.setattr(deployment_config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DEPLOYMENT_ID", "bare")
    reload_deployment_config()
    config = get_deployment_config()
    lib = config.library
    assert lib.library_id == ""
    assert lib.publish_base_branch == "main"
    assert lib.publish_target_dir == "discovery/approved"
