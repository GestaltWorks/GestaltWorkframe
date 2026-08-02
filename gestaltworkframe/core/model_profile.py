import json
import logging
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field, ValidationError


logger = logging.getLogger(__name__)


class GenerationParams(BaseModel):
    temperature: float = 0.7
    max_tokens: int = 2048
    top_p: float = 1.0
    stop: list[str] = Field(default_factory=list)


class ModelEvidence(BaseModel):
    source: str
    url: str
    note: str


class ModelProfile(BaseModel):
    name: str
    provider: Literal["llama_cpp", "claude", "openai_compatible", "openrouter"]
    model: str
    base_url: str = ""
    model_env: str = ""
    base_url_env: str = ""
    api_key_env: str = ""
    role: Literal["primary", "secondary", "escalation"] = "primary"
    # No `"free"` tier, and there must not be one again. It described an
    # aggregator `:free` endpoint, which is excluded outright as a data-handling
    # rule: those endpoints generally train on submitted prompts, and no setting
    # makes a training corpus forget. "Free" in this shop means local inference
    # on our own hardware, which is what `"local"` says. Leaving the value in the
    # Literal let a profile declare a tier that every cloud/local split in the
    # codebase then had to special-case, and two of them got it wrong.
    cost_tier: Literal["local", "low_cost", "premium"] = "local"
    # Per-profile pricing (USD per million tokens). 0.0 = free / unknown.
    input_price_usd_per_million: float = 0.0
    output_price_usd_per_million: float = 0.0
    deployment_status: Literal["active", "candidate", "disabled"] = "active"
    runtime_group: str = ""
    enabled_by_default: bool | None = None
    allowed_response_policies: list[str] = Field(default_factory=lambda: ["local_only"])
    params: GenerationParams = Field(default_factory=GenerationParams)
    recommended_for: list[str] = Field(default_factory=list)
    avoid_for: list[str] = Field(default_factory=list)
    routing_priority: int = 0
    capabilities: list[str] = Field(default_factory=list)
    tool_calling_quality: Literal["none", "weak", "ok", "strong"] = "none"
    # When non-empty, the router applies a preference bonus to routes that
    # serve this model via the named direct provider versus via OpenRouter.
    # Example: "anthropic" on a claude profile signals "prefer direct Anthropic
    # over OpenRouter if that budget still has headroom".
    preferred_provider_id: str = ""
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    evidence: list[ModelEvidence] = Field(default_factory=list)
    description: str = ""

    def route_enabled_by_default(self) -> bool:
        if self.enabled_by_default is not None:
            return self.enabled_by_default
        return self.deployment_status != "disabled"


_DEFAULT_PROFILES_PATH = Path(__file__).parent.parent.parent / "llm" / "profiles.json"


class ProfileStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(os.getenv("LLM_PROFILES_PATH", str(_DEFAULT_PROFILES_PATH)))
        self._profiles: dict[str, ModelProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for name, data in raw.get("profiles", {}).items():
                self._profiles[name] = ModelProfile(name=name, **data)
        except (OSError, json.JSONDecodeError, TypeError, ValidationError):
            logger.exception("Failed to load model profiles from %s", self._path)
            if os.getenv("LLM_PROFILES_STRICT", "").strip().lower() in {"1", "true"}:
                raise

    def get(self, name: str) -> ModelProfile | None:
        return self._profiles.get(name)

    def names(self) -> list[str]:
        return list(self._profiles.keys())

    def profiles(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    # No `recommended(task, provider)` query. Nothing called it: task fit is
    # decided on the ROUTE, by `LLMRouter._task_matches` against
    # `recommended_for` / `avoid_for`, and the ordering above it comes from the
    # lane. A second task-matching implementation on the profile store, ranked
    # by the hand-typed `routing_priority` integer, was a selector nobody
    # consulted and it ranked on the axis the doctrine took authority away from.


_default_store: ProfileStore | None = None


def get_default_store() -> ProfileStore:
    global _default_store
    if _default_store is None:
        _default_store = ProfileStore()
    return _default_store
