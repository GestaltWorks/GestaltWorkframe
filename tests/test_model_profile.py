import importlib.util
import json
import pytest
from pathlib import Path
from unittest.mock import patch
from core.model_profile import GenerationParams, ModelProfile, ProfileStore
from core.provider_registry import ProviderRegistry, SecondaryProviderProfile
from core.providers import ClaudeProvider, LocalProvider, OllamaProvider

ROOT = Path(__file__).parents[1]

_FIXTURE = {
    "profiles": {
        "test-local": {
            "provider": "llama_cpp",
            "model": "test-model",
            "base_url": "http://localhost:9090/v1",
            "role": "primary",
            "cost_tier": "local",
            "allowed_response_policies": ["local_only"],
            "params": {"temperature": 0.5, "max_tokens": 1024, "top_p": 0.8},
            "description": "Test local profile",
        },
        "test-ollama": {
            "provider": "ollama",
            "model": "test-ollama-model",
            "base_url": "http://localhost:11434",
            "role": "primary",
            "cost_tier": "local",
            "allowed_response_policies": ["local_only"],
            "params": {"temperature": 0.2, "max_tokens": 512, "top_p": 0.9},
            "description": "Test Ollama profile",
        },
        "test-claude": {
            "provider": "claude",
            "model": "claude-haiku-4-5-20251001",
            "base_url": "",
            "role": "secondary",
            "cost_tier": "low_cost",
            "allowed_response_policies": ["local_then_low_cost"],
            "params": {"temperature": 0.7, "max_tokens": 2048, "top_p": 1.0},
            "recommended_for": ["classification", "low_cost_fallback"],
            "routing_priority": 20,
            "description": "Test Claude profile",
        },
        "test-sonnet": {
            "provider": "claude",
            "model": "claude-sonnet-4-6",
            "base_url": "",
            "role": "escalation",
            "cost_tier": "premium",
            "allowed_response_policies": ["local_then_claude_if_high_value"],
            "params": {"temperature": 0.7, "max_tokens": 8192, "top_p": 1.0},
            "recommended_for": ["code_review", "coding"],
            "routing_priority": 95,
            "context_window_tokens": 1000000,
            "evidence": [{"source": "test", "url": "https://example.test", "note": "fixture"}],
            "description": "Test Sonnet profile",
        },
        "test-opus": {
            "provider": "claude",
            "model": "claude-opus-4-7",
            "base_url": "",
            "role": "escalation",
            "cost_tier": "premium",
            "allowed_response_policies": ["local_then_claude_if_high_value"],
            "params": {"temperature": 0.5, "max_tokens": 8192, "top_p": 1.0},
            "recommended_for": ["critical_code_review", "deep_reasoning"],
            "routing_priority": 100,
            "context_window_tokens": 1000000,
            "description": "Test Opus profile",
        },
    }
}


@pytest.fixture()
def store(tmp_path: Path) -> ProfileStore:
    p = tmp_path / "profiles.json"
    p.write_text(json.dumps(_FIXTURE), encoding="utf-8")
    return ProfileStore(path=p)


def test_store_loads_all_profiles(store: ProfileStore):
    assert set(store.names()) == {"test-local", "test-ollama", "test-claude", "test-sonnet", "test-opus"}


def test_store_exposes_profiles_for_provider_pool(store: ProfileStore):
    assert {profile.name for profile in store.profiles()} == {"test-local", "test-ollama", "test-claude", "test-sonnet", "test-opus"}


def test_store_returns_none_for_unknown_profile(store: ProfileStore):
    assert store.get("does-not-exist") is None


def test_store_parses_params(store: ProfileStore):
    profile = store.get("test-local")
    assert profile is not None
    assert profile.params.temperature == 0.5
    assert profile.params.max_tokens == 1024
    assert profile.params.top_p == 0.8
    assert profile.role == "primary"
    assert profile.cost_tier == "local"
    assert profile.allowed_response_policies == ["local_only"]


def test_candidate_profiles_default_enabled_unless_explicitly_stopped():
    candidate = ModelProfile(name="candidate", provider="claude", model="claude-test", deployment_status="candidate")
    disabled = ModelProfile(name="disabled", provider="claude", model="claude-test", deployment_status="disabled")
    explicit_off = ModelProfile(
        name="explicit-off",
        provider="claude",
        model="claude-test",
        deployment_status="candidate",
        enabled_by_default=False,
    )

    assert candidate.route_enabled_by_default() is True
    assert disabled.route_enabled_by_default() is False
    assert explicit_off.route_enabled_by_default() is False


def test_store_recommends_profiles_by_task_and_priority(store: ProfileStore):
    recommended = store.recommended("code_review", provider="claude")

    assert [profile.name for profile in recommended] == ["test-sonnet"]
    assert recommended[0].model == "claude-sonnet-4-6"
    assert recommended[0].context_window_tokens == 1000000
    assert recommended[0].evidence[0].source == "test"


def test_store_recommends_opus_for_critical_review(store: ProfileStore):
    recommended = store.recommended("critical_code_review", provider="claude")

    assert [profile.name for profile in recommended] == ["test-opus"]
    assert recommended[0].model == "claude-opus-4-7"
    assert recommended[0].routing_priority == 100


def test_store_handles_missing_file():
    s = ProfileStore(path=Path("/nonexistent/profiles.json"))
    assert s.names() == []
    assert s.get("anything") is None


def test_store_handles_malformed_json(tmp_path: Path):
    p = tmp_path / "profiles.json"
    p.write_text("not json", encoding="utf-8")
    s = ProfileStore(path=p)
    assert s.names() == []


def test_registry_uses_profile_params_for_local(store: ProfileStore, tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")

    env = {
        "LOCAL_LLM_PROFILE": "test-local",
        "ENABLE_CLAUDE_FALLBACK": "0",
        "LLM_PROFILES_PATH": str(profiles_path),
    }
    with patch.dict("os.environ", env, clear=False):
        from core import model_profile
        model_profile._default_store = ProfileStore(path=profiles_path)
        reg = ProviderRegistry.from_env()

    assert reg.primary_profile.model == "test-model"
    assert reg.primary_profile.params.temperature == 0.5
    assert reg.primary_profile.params.max_tokens == 1024
    provider = reg.build_primary()
    assert isinstance(provider, LocalProvider)
    assert provider.params.temperature == 0.5


def test_registry_uses_profile_params_for_ollama(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")

    env = {
        "LOCAL_LLM_PROFILE": "test-ollama",
        "ENABLE_CLAUDE_FALLBACK": "0",
        "LLM_PROFILES_PATH": str(profiles_path),
    }
    with patch.dict("os.environ", env, clear=False):
        from core import model_profile
        model_profile._default_store = ProfileStore(path=profiles_path)
        reg = ProviderRegistry.from_env()

    provider = reg.build_primary()
    assert isinstance(provider, OllamaProvider)
    assert provider.params.temperature == 0.2
    assert provider.params.max_tokens == 512


def test_registry_falls_back_to_raw_env_when_no_profile(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")

    env = {
        "LOCAL_LLM_PROFILE": "",
        "LOCAL_LLM_PROVIDER": "llama_cpp",
        "LOCAL_LLM_MODEL": "raw-env-model",
        "ENABLE_CLAUDE_FALLBACK": "0",
    }
    with patch.dict("os.environ", env, clear=False):
        from core import model_profile
        model_profile._default_store = ProfileStore(path=profiles_path)
        reg = ProviderRegistry.from_env()

    assert reg.primary_profile.model == "raw-env-model"


def test_registry_uses_claude_profile(tmp_path: Path):
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")

    env = {
        "LOCAL_LLM_PROFILE": "",
        "CLAUDE_PROFILE": "test-claude",
        "ENABLE_CLAUDE_FALLBACK": "1",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }
    with patch.dict("os.environ", env, clear=False):
        from core import model_profile
        model_profile._default_store = ProfileStore(path=profiles_path)
        reg = ProviderRegistry.from_env()

    assert reg.secondary_profile.model == "claude-haiku-4-5-20251001"
    assert reg.secondary_profile.cost_tier == "low_cost"
    assert reg.secondary_profile.allowed_response_policies == ["local_then_low_cost"]
    assert reg.secondary_profile.params.max_tokens == 2048
    provider = reg.build_secondary()
    assert isinstance(provider, ClaudeProvider)
    assert provider.params.max_tokens == 2048
    assert provider.cost_tier == "low_cost"


def test_repository_claude_profiles_use_current_api_ids():
    data = json.loads((ROOT / "llm" / "profiles.json").read_text(encoding="utf-8"))
    profiles = data["profiles"]

    assert profiles["claude-haiku-4-5"]["model"] == "claude-haiku-4-5-20251001"
    assert profiles["claude-sonnet-4-5"]["model"] == "claude-sonnet-4-5-20250929"
    assert profiles["claude-sonnet-4-6"]["model"] == "claude-sonnet-4-6"
    assert profiles["claude-opus-4-7"]["model"] == "claude-opus-4-7"


def test_repository_local_candidate_profiles_use_llama_cpp_ports():
    data = json.loads((ROOT / "llm" / "profiles.json").read_text(encoding="utf-8"))
    profiles = data["profiles"]

    assert profiles["llama-3.2-3b-q4"]["provider"] == "llama_cpp"
    assert profiles["llama-3.2-3b-q4"]["base_url"] == "http://localhost:8081/v1"
    assert profiles["qwen-2.5-coder-7b-q4"]["provider"] == "llama_cpp"
    assert profiles["qwen-2.5-coder-7b-q4"]["base_url"] == "http://localhost:8082/v1"


def test_repository_gemini_cloud_profile_is_api_backed_not_local():
    data = json.loads((ROOT / "llm" / "profiles.json").read_text(encoding="utf-8"))
    profile = data["profiles"]["gemini-cloud"]

    assert profile["provider"] == "openai_compatible"
    assert profile["runtime_group"] == "cloud"
    assert profile["cost_tier"] == "low_cost"
    assert profile["api_key_env"] == "GEMINI_CLOUD_API_KEY"
    assert profile["base_url_env"] == "GEMINI_CLOUD_BASE_URL"
    # gemini-cloud is a disabled bolt-on; operators enable it by setting GEMINI_CLOUD_* vars
    # and flipping deployment_status in profiles.json.
    assert profile["deployment_status"] == "disabled"
    assert profile["enabled_by_default"] is False


def test_claude_review_default_matches_sonnet_46_profile_and_workflow():
    data = json.loads((ROOT / "llm" / "profiles.json").read_text(encoding="utf-8"))
    profile_model = data["profiles"]["claude-sonnet-4-6"]["model"]

    script_path = ROOT / ".github" / "scripts" / "claude_review.py"
    spec = importlib.util.spec_from_file_location("claude_review", script_path)
    assert spec and spec.loader
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)

    workflow = (ROOT / ".github" / "workflows" / "claude-review.yml").read_text(encoding="utf-8")
    workflow_line = next(line for line in workflow.splitlines() if "CLAUDE_REVIEW_MODEL:" in line)
    workflow_default = workflow_line.split("'")[1]

    assert review.DEFAULT_CLAUDE_REVIEW_MODEL == profile_model
    assert workflow_default == profile_model
