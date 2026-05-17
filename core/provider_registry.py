import os
from urllib.parse import urlparse, urlunparse
from typing import Literal
from pydantic import BaseModel, Field
from core.model_profile import GenerationParams, ModelProfile, ProfileStore, get_default_store
from core.providers import ClaudeProvider, LocalProvider, LLMProvider, OllamaProvider, OpenAICompatibleProvider
from core.router import ProviderRoute

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _local_profile_base_url(profile_url: str) -> str:
    override = _env_text("LOCAL_LLM_BASE_URL")
    if not override:
        return profile_url or "http://localhost:8080/v1"
    profile = urlparse(profile_url or "http://localhost:8080/v1")
    env = urlparse(override)
    if profile.hostname in {"localhost", "127.0.0.1"} and env.hostname:
        netloc = env.hostname
        if profile.port:
            netloc = f"{netloc}:{profile.port}"
        return urlunparse((env.scheme or profile.scheme, netloc, profile.path or env.path or "/v1", "", "", ""))
    return override


def _env_local_provider() -> Literal["llama_cpp", "ollama"]:
    provider = _env_text("LOCAL_LLM_PROVIDER", "llama_cpp").lower()
    return "ollama" if provider == "ollama" else "llama_cpp"


class LocalProviderProfile(BaseModel):
    name: str = "env-local"
    type: Literal["llama_cpp", "ollama"] = "llama_cpp"
    role: Literal["primary"] = "primary"
    cost_tier: Literal["local"] = "local"
    deployment_status: Literal["active", "candidate", "disabled"] = "active"
    runtime_group: str = "env"
    enabled_by_default: bool = True
    allowed_response_policies: list[str] = Field(default_factory=lambda: ["local_only"])
    base_url: str = "http://localhost:8080/v1"
    ollama_base_url: str = "http://localhost:11434"
    model: str = "local-model"
    timeout: float = 30.0
    params: GenerationParams = Field(default_factory=GenerationParams)


class SecondaryProviderProfile(BaseModel):
    name: str = "env-secondary"
    enabled: bool = False
    role: Literal["secondary", "escalation"] = "secondary"
    cost_tier: Literal["low_cost", "premium"] = "low_cost"
    deployment_status: Literal["active", "candidate", "disabled"] = "active"
    runtime_group: str = "cloud"
    enabled_by_default: bool = True
    allowed_response_policies: list[str] = Field(default_factory=lambda: ["local_then_low_cost"])
    api_key: str = Field(default="", repr=False)
    model: str = "claude-haiku-4-5-20251001"
    params: GenerationParams = Field(default_factory=lambda: GenerationParams(max_tokens=4096))


class ProviderRegistry:
    def __init__(
        self,
        primary: LocalProviderProfile | None = None,
        secondary: SecondaryProviderProfile | None = None,
        store: ProfileStore | None = None,
    ) -> None:
        self.primary_profile = primary or LocalProviderProfile()
        self.secondary_profile = secondary or SecondaryProviderProfile()
        self.store = store or get_default_store()

    @classmethod
    def from_env(cls) -> "ProviderRegistry":
        store = get_default_store()

        primary_profile_name = _env_text("LOCAL_LLM_PROFILE")
        if primary_profile_name and (mp := store.get(primary_profile_name)):
            primary = LocalProviderProfile(
                name=mp.name,
                type=mp.provider,  # type: ignore[arg-type]
                role="primary",
                cost_tier="local",
                deployment_status=mp.deployment_status,
                runtime_group=mp.runtime_group or "personal_gpu",
                enabled_by_default=mp.route_enabled_by_default(),
                allowed_response_policies=mp.allowed_response_policies,
                base_url=mp.base_url if mp.provider == "llama_cpp" else "http://localhost:8080/v1",
                ollama_base_url=mp.base_url if mp.provider == "ollama" else "http://localhost:11434",
                model=mp.model,
                params=mp.params,
            )
        else:
            primary = LocalProviderProfile(
                name="env-local",
                type=_env_local_provider(),
                base_url=_env_text("LOCAL_LLM_BASE_URL", "http://localhost:8080/v1"),
                ollama_base_url=_env_text("OLLAMA_BASE_URL", "http://localhost:11434"),
                model=_env_text("LOCAL_LLM_MODEL", "local-model"),
            )

        secondary_profile_name = _env_text("CLAUDE_PROFILE")
        if secondary_profile_name and (sp := store.get(secondary_profile_name)):
            secondary = SecondaryProviderProfile(
                name=sp.name,
                enabled=_env_bool("ENABLE_CLAUDE_FALLBACK"),
                role="escalation" if sp.cost_tier == "premium" else "secondary",
                cost_tier=sp.cost_tier if sp.cost_tier in {"low_cost", "premium"} else "low_cost",
                deployment_status=sp.deployment_status,
                runtime_group=sp.runtime_group or "cloud",
                enabled_by_default=sp.route_enabled_by_default(),
                allowed_response_policies=sp.allowed_response_policies,
                api_key=_env_text("ANTHROPIC_API_KEY"),
                model=sp.model,
                params=sp.params,
            )
        else:
            secondary = SecondaryProviderProfile(
                name="env-secondary",
                enabled=_env_bool("ENABLE_CLAUDE_FALLBACK"),
                api_key=_env_text("ANTHROPIC_API_KEY"),
                model=_env_text("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            )

        return cls(primary=primary, secondary=secondary, store=store)

    def build_routes(self) -> list[ProviderRoute]:
        routes = [
            self._route_from_local_profile(profile)
            for profile in self.store.profiles()
            if profile.provider in {"llama_cpp", "ollama"}
        ]
        routes.extend(
            self._route_from_openai_compatible_profile(profile)
            for profile in self.store.profiles()
            if profile.provider == "openai_compatible"
        )
        routes.extend(
            self._route_from_claude_profile(profile)
            for profile in self.store.profiles()
            if profile.provider == "claude"
        )
        if not routes:
            primary = self.build_primary()
            routes.append(self._route_from_provider("env-local", primary, self.primary_profile))
            secondary = self.build_secondary()
            if secondary:
                routes.append(self._route_from_provider("env-secondary", secondary, self.secondary_profile))
        return routes

    def build_primary(self) -> LLMProvider:
        p = self.primary_profile
        if p.type == "ollama":
            provider = OllamaProvider(base_url=p.ollama_base_url, model=p.model, params=p.params)
        else:
            provider = LocalProvider(base_url=p.base_url, model=p.model, params=p.params)
        self._attach_profile(provider, p)
        return provider

    def build_secondary(self) -> LLMProvider | None:
        s = self.secondary_profile
        if not s.enabled or not s.api_key:
            return None
        provider = ClaudeProvider(api_key=s.api_key, model=s.model, params=s.params)
        self._attach_profile(provider, s)
        return provider

    def _attach_profile(self, provider: LLMProvider, profile: LocalProviderProfile | SecondaryProviderProfile) -> None:
        provider.profile_name = profile.name  # type: ignore[attr-defined]
        provider.provider_role = profile.role  # type: ignore[attr-defined]
        provider.cost_tier = profile.cost_tier  # type: ignore[attr-defined]
        provider.allowed_response_policies = profile.allowed_response_policies  # type: ignore[attr-defined]

    def _route_from_local_profile(self, profile: ModelProfile) -> ProviderRoute:
        if profile.provider == "ollama":
            provider = OllamaProvider(
                base_url=_env_text("OLLAMA_BASE_URL", profile.base_url or "http://localhost:11434"),
                model=profile.model,
                params=profile.params,
            )
        else:
            provider = LocalProvider(
                base_url=_local_profile_base_url(profile.base_url),
                model=profile.model,
                params=profile.params,
            )
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True)

    def _route_from_claude_profile(self, profile: ModelProfile) -> ProviderRoute:
        api_key = _env_text("ANTHROPIC_API_KEY")
        if not api_key:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_api_key")
        provider = ClaudeProvider(api_key=api_key, model=profile.model, params=profile.params)
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True)

    def _route_from_openai_compatible_profile(self, profile: ModelProfile) -> ProviderRoute:
        api_key = _env_text(profile.api_key_env) if profile.api_key_env else ""
        base_url = _env_text(profile.base_url_env, profile.base_url) if profile.base_url_env else profile.base_url
        model = _env_text(profile.model_env, profile.model) if profile.model_env else profile.model
        if not api_key:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_api_key", model=model)
        if not base_url:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_base_url", model=model)
        provider = OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model, params=profile.params)
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True, model=model)

    def _route_from_provider(
        self,
        name: str,
        provider: LLMProvider,
        profile: LocalProviderProfile | SecondaryProviderProfile,
    ) -> ProviderRoute:
        return ProviderRoute(
            name=name,
            provider=provider,
            provider_type=provider.__class__.__name__,
            model=getattr(provider, "model", profile.model),
            role=profile.role,
            cost_tier=profile.cost_tier,
            allowed_response_policies=profile.allowed_response_policies,
            configured=True,
            deployment_status=profile.deployment_status,
            runtime_group=profile.runtime_group,
            enabled_by_default=profile.enabled_by_default,
        )

    def _route_from_model_profile(
        self,
        profile: ModelProfile,
        provider: LLMProvider | None,
        configured: bool,
        blocked_reason: str = "",
        model: str | None = None,
    ) -> ProviderRoute:
        return ProviderRoute(
            name=profile.name,
            provider=provider,
            provider_type=profile.provider,
            model=model or profile.model,
            role=profile.role,
            cost_tier=profile.cost_tier,
            allowed_response_policies=profile.allowed_response_policies,
            recommended_for=profile.recommended_for,
            avoid_for=profile.avoid_for,
            routing_priority=profile.routing_priority,
            configured=configured,
            blocked_reason=blocked_reason,
            deployment_status=profile.deployment_status,
            runtime_group=profile.runtime_group or ("cloud" if profile.cost_tier in {"low_cost", "premium"} else "personal_gpu"),
            enabled_by_default=profile.route_enabled_by_default(),
        )

    def _attach_model_profile(self, provider: LLMProvider, profile: ModelProfile) -> None:
        provider.profile_name = profile.name  # type: ignore[attr-defined]
        provider.provider_role = profile.role  # type: ignore[attr-defined]
        provider.cost_tier = profile.cost_tier  # type: ignore[attr-defined]
        provider.allowed_response_policies = profile.allowed_response_policies  # type: ignore[attr-defined]
