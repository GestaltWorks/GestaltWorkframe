import json
import pytest
from pathlib import Path
from unittest.mock import patch
from gestaltworkframe.core.model_profile import ProfileStore
from gestaltworkframe.core.provider_registry import LocalProviderProfile, ProviderRegistry, SecondaryProviderProfile, _local_profile_base_url
from gestaltworkframe.core.providers import ClaudeProvider, LocalProvider, OpenAICompatibleProvider


def _registry(env: dict) -> ProviderRegistry:
    with patch.dict("os.environ", env, clear=False):
        return ProviderRegistry.from_env()


def test_default_profile_builds_local_provider():
    reg = ProviderRegistry()
    provider = reg.build_primary()
    assert isinstance(provider, LocalProvider)


def test_local_provider_url_and_model_passed_through():
    reg = ProviderRegistry(
        primary=LocalProviderProfile(base_url="http://192.0.2.2:8080/v1", model="llama-3.1-8b")
    )
    provider = reg.build_primary()
    assert isinstance(provider, LocalProvider)
    assert provider.model == "llama-3.1-8b"
    assert "192.0.2.2" in provider.base_url
    assert provider.cost_tier == "local"
    assert provider.provider_role == "primary"


def test_secondary_returns_none_when_disabled():
    reg = ProviderRegistry(
        secondary=SecondaryProviderProfile(enabled=False, api_key="sk-test", model="claude-haiku-4-5-20251001")
    )
    assert reg.build_secondary() is None


def test_secondary_returns_none_when_no_api_key():
    reg = ProviderRegistry(
        secondary=SecondaryProviderProfile(enabled=True, api_key="", model="claude-haiku-4-5-20251001")
    )
    assert reg.build_secondary() is None


def test_secondary_builds_claude_provider_when_enabled():
    reg = ProviderRegistry(
        secondary=SecondaryProviderProfile(enabled=True, api_key="sk-ant-test", model="claude-haiku-4-5-20251001")
    )
    provider = reg.build_secondary()
    assert isinstance(provider, ClaudeProvider)
    assert provider.model == "claude-haiku-4-5-20251001"
    assert provider.cost_tier == "low_cost"
    assert provider.provider_role == "secondary"


def test_from_env_reads_local_settings():
    reg = _registry({
        "LOCAL_LLM_BASE_URL": "http://localhost:9090/v1",
        "LOCAL_LLM_MODEL": "my-model",
        "ENABLE_CLAUDE_FALLBACK": "0",
    })
    assert reg.primary_profile.model == "my-model"
    assert reg.primary_profile.base_url == "http://localhost:9090/v1"
    assert reg.secondary_profile.enabled is False
    assert isinstance(reg.build_primary(), LocalProvider)


def test_profile_base_url_override_preserves_profile_port_for_gpu_host():
    with patch.dict("os.environ", {"LOCAL_LLM_BASE_URL": "http://192.0.2.2:8080/v1"}, clear=False):
        assert _local_profile_base_url("http://localhost:8082/v1") == "http://192.0.2.2:8082/v1"


def test_from_env_enables_secondary_when_flag_set():
    reg = _registry({
        "ENABLE_CLAUDE_FALLBACK": "true",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "CLAUDE_MODEL": "claude-haiku-4-5-20251001",
    })
    assert reg.secondary_profile.enabled is True
    provider = reg.build_secondary()
    assert isinstance(provider, ClaudeProvider)


def test_build_routes_exposes_profile_pool_without_cloud_key(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "local-a": {"provider": "llama_cpp", "model": "local-a", "base_url": "http://localhost:8080/v1"},
            "cloud-a": {"provider": "claude", "model": "claude-haiku-4-5-20251001", "role": "secondary", "cost_tier": "low_cost"},
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {"LLM_PROFILES_PATH": str(profiles_path)}, clear=True):
        reg = ProviderRegistry(store=ProfileStore(path=profiles_path))
        routes = reg.build_routes()

    assert [route.name for route in routes] == ["local-a", "cloud-a"]
    assert routes[0].configured is True
    assert routes[0].provider is not None
    assert routes[1].configured is False
    assert routes[1].provider is None
    assert routes[1].blocked_reason == "missing_api_key"


def test_build_routes_exposes_openai_compatible_route_without_key(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "gemini": {
                "provider": "openai_compatible",
                "model": "gemini-2.5-flash",
                "base_url_env": "GEMINI_TEST_BASE_URL",
                "api_key_env": "GEMINI_TEST_API_KEY",
                "role": "secondary",
                "cost_tier": "low_cost",
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {}, clear=True):
        route = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()[0]

    assert route.name == "gemini"
    assert route.configured is False
    assert route.provider is None
    assert route.blocked_reason == "missing_api_key"
    assert route.enabled_by_default is True


def test_build_routes_builds_openai_compatible_cloud_route_when_configured(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "gemini": {
                "provider": "openai_compatible",
                "model": "gemini-2.5-flash",
                "model_env": "GEMINI_TEST_MODEL",
                "base_url_env": "GEMINI_TEST_BASE_URL",
                "api_key_env": "GEMINI_TEST_API_KEY",
                "role": "secondary",
                "cost_tier": "low_cost",
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {
        "GEMINI_TEST_API_KEY": "sk-test",
        "GEMINI_TEST_BASE_URL": "https://gemini.example/v1",
        "GEMINI_TEST_MODEL": "gemini-custom",
    }, clear=True):
        route = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()[0]

    assert route.configured is True
    assert isinstance(route.provider, OpenAICompatibleProvider)
    assert route.model == "gemini-custom"
    assert route.cost_tier == "low_cost"


def test_build_routes_preserves_profile_deployment_semantics(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "local-candidate": {
                "provider": "llama_cpp",
                "model": "local-candidate",
                "base_url": "http://localhost:8080/v1",
                "deployment_status": "candidate",
                "runtime_group": "personal_gpu",
                "enabled_by_default": False,
                "avoid_for": ["high_stakes_reasoning"],
            },
        }
    }), encoding="utf-8")

    reg = ProviderRegistry(store=ProfileStore(path=profiles_path))
    route = reg.build_routes()[0]

    assert route.deployment_status == "candidate"
    assert route.runtime_group == "personal_gpu"
    assert route.enabled_by_default is False
    assert route.avoid_for == ["high_stakes_reasoning"]


def test_profile_store_logs_bad_profile_json(caplog, tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text("{bad json", encoding="utf-8")

    store = ProfileStore(path=profiles_path)

    assert store.profiles() == []
    assert "Failed to load model profiles" in caplog.text


# ---------------------------------------------------------------------------
# provider_budget_id assignment tests
# ---------------------------------------------------------------------------

def test_openrouter_route_gets_openrouter_budget_id(tmp_path):
    profiles_path = tmp_path / "profiles.json"
    import json
    profiles_path.write_text(json.dumps({
        "profiles": {
            "or-free": {
                "provider": "openrouter",
                "model": "openrouter/auto",
                "api_key_env": "OR_KEY",
                "role": "primary",
                "cost_tier": "free",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_only"],
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {"OR_KEY": "sk-or-test"}, clear=False):
        routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert len(routes) == 1
    assert routes[0].provider_budget_id == "openrouter"


def test_claude_route_gets_anthropic_budget_id(tmp_path):
    profiles_path = tmp_path / "profiles.json"
    import json
    profiles_path.write_text(json.dumps({
        "profiles": {
            "claude-haiku": {
                "provider": "claude",
                "model": "claude-haiku-4-5-20251001",
                "role": "secondary",
                "cost_tier": "low_cost",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_then_low_cost"],
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False):
        routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert len(routes) == 1
    assert routes[0].provider_budget_id == "anthropic"


def test_gemini_route_gets_google_budget_id(tmp_path):
    profiles_path = tmp_path / "profiles.json"
    import json
    profiles_path.write_text(json.dumps({
        "profiles": {
            "gemini-cloud": {
                "provider": "openai_compatible",
                "model": "gemini-2.5-flash",
                "role": "secondary",
                "cost_tier": "low_cost",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_then_low_cost"],
                "api_key_env": "GEMINI_CLOUD_API_KEY",
                "base_url_env": "GEMINI_CLOUD_BASE_URL",
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {
        "GEMINI_CLOUD_API_KEY": "sk-google-test",
        "GEMINI_CLOUD_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    }, clear=False):
        routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert len(routes) == 1
    assert routes[0].provider_budget_id == "google"


def test_local_route_gets_default_budget_id(tmp_path):
    profiles_path = tmp_path / "profiles.json"
    import json
    profiles_path.write_text(json.dumps({
        "profiles": {
            "llama-local": {
                "provider": "llama_cpp",
                "model": "llama-3.1-8b",
                "role": "primary",
                "cost_tier": "local",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_only"],
                "base_url": "http://localhost:8080/v1",
            },
        }
    }), encoding="utf-8")
    routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert len(routes) == 1
    assert routes[0].provider_budget_id == "default"


# ---------------------------------------------------------------------------
# Phase 4 - preferred_provider_id passthrough tests
# ---------------------------------------------------------------------------

def test_preferred_provider_id_passed_through_for_openrouter_claude(tmp_path):
    """preferred_provider_id on a profile flows through to the ProviderRoute."""
    import json
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "openrouter-claude-sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4",
                "api_key_env": "OR_KEY",
                "role": "secondary",
                "cost_tier": "premium",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_then_low_cost"],
                "preferred_provider_id": "anthropic",
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {"OR_KEY": "sk-or-test"}, clear=False):
        routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert len(routes) == 1
    assert routes[0].preferred_provider_id == "anthropic"
    assert routes[0].provider_budget_id == "openrouter"


def test_preferred_provider_id_empty_by_default(tmp_path):
    """Routes built from profiles without preferred_provider_id get empty string."""
    import json
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({
        "profiles": {
            "or-free": {
                "provider": "openrouter",
                "model": "openrouter/auto",
                "api_key_env": "OR_KEY",
                "role": "primary",
                "cost_tier": "free",
                "deployment_status": "active",
                "enabled_by_default": True,
                "allowed_response_policies": ["local_only"],
            },
        }
    }), encoding="utf-8")
    with patch.dict("os.environ", {"OR_KEY": "sk-test"}, clear=False):
        routes = ProviderRegistry(store=ProfileStore(path=profiles_path)).build_routes()
    assert routes[0].preferred_provider_id == ""


def test_live_profiles_claude_openrouter_has_anthropic_preference():
    """The real profiles.json sets preferred_provider_id=anthropic on claude-via-OpenRouter."""
    from gestaltworkframe.core.model_profile import get_default_store
    store = get_default_store()
    for name in ("openrouter-claude-sonnet-4-6", "openrouter-claude-opus-4-7"):
        profile = store.get(name)
        if profile is not None:
            assert profile.preferred_provider_id == "anthropic", (
                f"{name}: expected preferred_provider_id='anthropic', got {profile.preferred_provider_id!r}"
            )
