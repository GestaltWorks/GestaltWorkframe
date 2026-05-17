import json
import pytest
from pathlib import Path
from unittest.mock import patch
from core.model_profile import ProfileStore
from core.provider_registry import LocalProviderProfile, ProviderRegistry, SecondaryProviderProfile, _local_profile_base_url
from core.providers import ClaudeProvider, LocalProvider, OllamaProvider, OpenAICompatibleProvider


def _registry(env: dict) -> ProviderRegistry:
    with patch.dict("os.environ", env, clear=False):
        return ProviderRegistry.from_env()


def test_default_profile_builds_local_provider():
    reg = ProviderRegistry()
    provider = reg.build_primary()
    assert isinstance(provider, LocalProvider)


def test_ollama_type_builds_ollama_provider():
    reg = ProviderRegistry(
        primary=LocalProviderProfile(type="ollama", ollama_base_url="http://localhost:11434", model="llama3")
    )
    provider = reg.build_primary()
    assert isinstance(provider, OllamaProvider)
    assert provider.model == "llama3"


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


def test_from_env_reads_llama_cpp():
    reg = _registry({
        "LOCAL_LLM_PROVIDER": "llama_cpp",
        "LOCAL_LLM_BASE_URL": "http://localhost:9090/v1",
        "LOCAL_LLM_MODEL": "my-model",
        "ENABLE_CLAUDE_FALLBACK": "0",
    })
    assert reg.primary_profile.type == "llama_cpp"
    assert reg.primary_profile.model == "my-model"
    assert reg.secondary_profile.enabled is False


def test_from_env_reads_ollama():
    reg = _registry({
        "LOCAL_LLM_PROVIDER": "ollama",
        "OLLAMA_BASE_URL": "http://localhost:11434",
        "LOCAL_LLM_MODEL": "qwen2.5-coder:7b",
        "ENABLE_CLAUDE_FALLBACK": "0",
    })
    assert reg.primary_profile.type == "ollama"
    provider = reg.build_primary()
    assert isinstance(provider, OllamaProvider)


def test_from_env_invalid_local_provider_falls_back_to_llama_cpp():
    reg = _registry({
        "LOCAL_LLM_PROVIDER": "not-real",
        "LOCAL_LLM_MODEL": "my-model",
        "ENABLE_CLAUDE_FALLBACK": "0",
    })

    assert reg.primary_profile.type == "llama_cpp"
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
