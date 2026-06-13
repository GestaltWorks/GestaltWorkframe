import logging
import os
from urllib.parse import urlparse, urlunparse
from typing import Any, Literal
from pydantic import BaseModel, Field
from gestaltworkframe.core.model_profile import GenerationParams, ModelProfile, ProfileStore, get_default_store
from gestaltworkframe.core.providers import ClaudeProvider, LocalProvider, LLMProvider, OpenAICompatibleProvider
from gestaltworkframe.core.router import ProviderRoute
from gestalt_llm_contract import env as llm_env

logger = logging.getLogger(__name__)

# Single source of truth for the shared LLM env contract (see gestalt-llm-contract).
_OPENROUTER_DEFAULT_BASE_URL = llm_env.DEFAULT_OPENROUTER_BASE_URL


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _provider_budget_id(profile: "ModelProfile") -> str:
    if profile.provider == "openrouter":
        return "openrouter"
    if profile.provider == "claude":
        return "anthropic"
    if getattr(profile, "api_key_env", "") == "GEMINI_CLOUD_API_KEY":
        return "google"
    return "default"


def _local_profile_base_url(profile_url: str) -> str:
    override = _env_text(llm_env.LOCAL_LLM_BASE_URL)
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


class LocalProviderProfile(BaseModel):
    name: str = "env-local"
    type: Literal["llama_cpp"] = "llama_cpp"
    role: Literal["primary"] = "primary"
    cost_tier: Literal["local"] = "local"
    deployment_status: Literal["active", "candidate", "disabled"] = "active"
    runtime_group: str = "env"
    enabled_by_default: bool = True
    allowed_response_policies: list[str] = Field(default_factory=lambda: ["local_only"])
    base_url: str = "http://localhost:8080/v1"
    model: str = "local-model"
    timeout: float = 30.0
    params: GenerationParams = Field(default_factory=GenerationParams)
    capabilities: list[str] = Field(default_factory=lambda: ["chat", "rag_answering"])
    tool_calling_quality: Literal["none", "weak", "ok", "strong"] = "none"


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
    capabilities: list[str] = Field(default_factory=lambda: ["chat", "tools", "rag_answering"])
    tool_calling_quality: Literal["none", "weak", "ok", "strong"] = "strong"


class ProviderRegistry:
    def __init__(
        self,
        primary: LocalProviderProfile | None = None,
        secondary: SecondaryProviderProfile | None = None,
        store: ProfileStore | None = None,
        key_store: Any | None = None,
        admin_token: str = "",
    ) -> None:
        self.primary_profile = primary or LocalProviderProfile()
        self.secondary_profile = secondary or SecondaryProviderProfile()
        self.store = store or get_default_store()
        self.key_store = key_store
        self.admin_token = admin_token

    def _get_api_key(self, provider_id: str, env_var: str) -> str:
        """Get API key from env var or key store."""
        # First check environment variable
        key = _env_text(env_var)
        if key:
            return key
        # Then check key store if available
        if self.key_store and self.admin_token:
            try:
                stored_key = self.key_store.get_key_sync(provider_id, self.admin_token)
                if stored_key:
                    return stored_key
            except Exception as exc:
                logger.debug("key store lookup failed for %s, falling back to env: %s", provider_id, exc)
        return ""

    @classmethod
    def from_env(cls) -> "ProviderRegistry":
        store = get_default_store()

        primary_profile_name = _env_text("LOCAL_LLM_PROFILE")
        if primary_profile_name and (mp := store.get(primary_profile_name)):
            primary = LocalProviderProfile(
                name=mp.name,
                role="primary",
                cost_tier="local",
                deployment_status=mp.deployment_status,
                runtime_group=mp.runtime_group or "personal_gpu",
                enabled_by_default=mp.route_enabled_by_default(),
                allowed_response_policies=mp.allowed_response_policies,
                base_url=mp.base_url or "http://localhost:8080/v1",
                model=mp.model,
                params=mp.params,
                capabilities=mp.capabilities,
                tool_calling_quality=mp.tool_calling_quality,
            )
        else:
            primary = LocalProviderProfile(
                name="env-local",
                base_url=_env_text(llm_env.LOCAL_LLM_BASE_URL, llm_env.DEFAULT_LOCAL_BASE_URL),
                model=_env_text(llm_env.LOCAL_LLM_MODEL, "local-model"),
            )

        secondary_profile_name = _env_text("CLAUDE_PROFILE")
        if secondary_profile_name and (sp := store.get(secondary_profile_name)):
            secondary = SecondaryProviderProfile(
                name=sp.name,
                enabled=_env_bool(llm_env.ENABLE_CLAUDE_FALLBACK),
                role="escalation" if sp.cost_tier == "premium" else "secondary",
                cost_tier=sp.cost_tier if sp.cost_tier in {"low_cost", "premium"} else "low_cost",
                deployment_status=sp.deployment_status,
                runtime_group=sp.runtime_group or "cloud",
                enabled_by_default=sp.route_enabled_by_default(),
                allowed_response_policies=sp.allowed_response_policies,
                api_key=_env_text(llm_env.ANTHROPIC_API_KEY),
                model=sp.model,
                params=sp.params,
                capabilities=sp.capabilities,
                tool_calling_quality=sp.tool_calling_quality,
            )
        else:
            secondary = SecondaryProviderProfile(
                name="env-secondary",
                enabled=_env_bool(llm_env.ENABLE_CLAUDE_FALLBACK),
                api_key=_env_text(llm_env.ANTHROPIC_API_KEY),
                model=_env_text("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            )

        return cls(primary=primary, secondary=secondary, store=store)

    def build_routes(self) -> list[ProviderRoute]:
        routes = [
            self._route_from_local_profile(profile)
            for profile in self.store.profiles()
            if profile.provider == "llama_cpp"
        ]
        routes.extend(
            self._route_from_openai_compatible_profile(profile)
            for profile in self.store.profiles()
            if profile.provider == "openai_compatible"
        )
        routes.extend(
            self._route_from_openrouter_profile(profile)
            for profile in self.store.profiles()
            if profile.provider == "openrouter"
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
        provider.capabilities = profile.capabilities  # type: ignore[attr-defined]
        provider.tool_calling_quality = profile.tool_calling_quality  # type: ignore[attr-defined]

    def _route_from_local_profile(self, profile: ModelProfile) -> ProviderRoute:
        provider = LocalProvider(
            base_url=_local_profile_base_url(profile.base_url),
            model=profile.model,
            params=profile.params,
        )
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True)

    def _route_from_claude_profile(self, profile: ModelProfile) -> ProviderRoute:
        api_key = self._get_api_key("anthropic", llm_env.ANTHROPIC_API_KEY)
        if not api_key:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_api_key")
        provider = ClaudeProvider(api_key=api_key, model=profile.model, params=profile.params)
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True)

    def _route_from_openai_compatible_profile(self, profile: ModelProfile) -> ProviderRoute:
        # Map provider name to key store ID
        provider_key_id = profile.provider_id if hasattr(profile, 'provider_id') else "openai"
        if profile.api_key_env and "ANTHROPIC" in profile.api_key_env:
            provider_key_id = "anthropic"
        elif profile.api_key_env and "GOOGLE" in profile.api_key_env:
            provider_key_id = "google"
        elif profile.api_key_env and "OPENAI" in profile.api_key_env:
            provider_key_id = "openai"

        api_key = self._get_api_key(provider_key_id, profile.api_key_env) if profile.api_key_env else ""
        base_url = _env_text(profile.base_url_env, profile.base_url) if profile.base_url_env else profile.base_url
        model = _env_text(profile.model_env, profile.model) if profile.model_env else profile.model
        if not api_key:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_api_key", model=model)
        if not base_url:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_base_url", model=model)
        provider = OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model, params=profile.params)
        self._attach_model_profile(provider, profile)
        return self._route_from_model_profile(profile, provider, configured=True, model=model)

    def _route_from_openrouter_profile(self, profile: ModelProfile) -> ProviderRoute:
        # api_key_env overrides OPENROUTER_API_KEY when set on the profile.
        key_env = profile.api_key_env or llm_env.OPENROUTER_API_KEY
        api_key = _env_text(key_env)
        # base_url defaults to the canonical OpenRouter endpoint unless overridden.
        base_url_env_val = _env_text(profile.base_url_env) if profile.base_url_env else ""
        base_url = base_url_env_val or profile.base_url or _env_text(llm_env.OPENROUTER_BASE_URL, _OPENROUTER_DEFAULT_BASE_URL)
        model = _env_text(profile.model_env, profile.model) if profile.model_env else profile.model
        if not api_key:
            return self._route_from_model_profile(profile, None, configured=False, blocked_reason="missing_api_key", model=model)
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
            capabilities=profile.capabilities,
            tool_calling_quality=profile.tool_calling_quality,
        )

    def _route_from_model_profile(
        self,
        profile: ModelProfile,
        provider: LLMProvider | None,
        configured: bool,
        blocked_reason: str = "",
        model: str | None = None,
    ) -> ProviderRoute:
        budget_id = _provider_budget_id(profile)
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
            runtime_group=profile.runtime_group or ("cloud" if profile.cost_tier in {"low_cost", "premium"} else "openrouter" if profile.provider == "openrouter" else "personal_gpu"),
            enabled_by_default=profile.route_enabled_by_default(),
            capabilities=profile.capabilities,
            tool_calling_quality=profile.tool_calling_quality,
            input_price_usd_per_million=profile.input_price_usd_per_million,
            output_price_usd_per_million=profile.output_price_usd_per_million,
            provider_budget_id=budget_id,
            preferred_provider_id=profile.preferred_provider_id,
        )

    def _attach_model_profile(self, provider: LLMProvider, profile: ModelProfile) -> None:
        provider.profile_name = profile.name  # type: ignore[attr-defined]
        provider.provider_role = profile.role  # type: ignore[attr-defined]
        provider.cost_tier = profile.cost_tier  # type: ignore[attr-defined]
        provider.allowed_response_policies = profile.allowed_response_policies  # type: ignore[attr-defined]
        provider.capabilities = profile.capabilities  # type: ignore[attr-defined]
        provider.tool_calling_quality = profile.tool_calling_quality  # type: ignore[attr-defined]
