import logging
import os
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class RuntimeControlPolicy:
    warm_route_bonus: int = 75
    unavailable_penalty: int = 750

    @classmethod
    def from_env(cls) -> "RuntimeControlPolicy":
        return cls(
            warm_route_bonus=_env_int("RUNTIME_WARM_ROUTE_BONUS", 75),
            unavailable_penalty=_env_int("RUNTIME_UNAVAILABLE_PENALTY", 750),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "warm_route_bonus": self.warm_route_bonus,
            "unavailable_penalty": self.unavailable_penalty,
        }


@dataclass
class GenerationConcurrencyPolicy:
    normal_parallel_limit: int = 3
    hard_parallel_limit: int = 5
    max_local_generations: int = 2
    max_cloud_generations: int = 3

    @classmethod
    def from_env(cls) -> "GenerationConcurrencyPolicy":
        policy = cls(
            normal_parallel_limit=max(_env_int("GENERATION_NORMAL_PARALLEL_LIMIT", 3), 1),
            hard_parallel_limit=max(_env_int("GENERATION_HARD_PARALLEL_LIMIT", 5), 1),
            max_local_generations=max(_env_int("GENERATION_MAX_LOCAL", 2), 0),
            max_cloud_generations=max(_env_int("GENERATION_MAX_CLOUD", 3), 0),
        )
        policy.hard_parallel_limit = max(policy.hard_parallel_limit, policy.normal_parallel_limit)
        policy.max_local_generations = min(policy.max_local_generations, policy.hard_parallel_limit)
        policy.max_cloud_generations = min(policy.max_cloud_generations, policy.hard_parallel_limit)
        return policy

    def can_start(
        self,
        cost_tier: str,
        active_total: int,
        active_local: int,
        active_cloud: int,
        weighted_required: bool = False,
    ) -> bool:
        total_limit = self.hard_parallel_limit if weighted_required else self.normal_parallel_limit
        if active_total >= min(total_limit, self.hard_parallel_limit):
            return False
        if cost_tier == "local":
            return active_local < self.max_local_generations
        return active_cloud < self.max_cloud_generations

    def snapshot(self) -> dict[str, int]:
        return {
            "normal_parallel_limit": self.normal_parallel_limit,
            "hard_parallel_limit": self.hard_parallel_limit,
            "max_local_generations": self.max_local_generations,
            "max_cloud_generations": self.max_cloud_generations,
        }


class RuntimeManager:
    def __init__(self, policy: RuntimeControlPolicy | None = None) -> None:
        self.policy = policy or RuntimeControlPolicy()

    def status(self, route: Any, health: dict[str, Any] | None = None, route_enabled: bool = True) -> dict[str, Any]:
        if getattr(route, "is_cloud", False):
            return {"runtime_group": getattr(route, "runtime_group", "cloud") or "cloud"}
        return {
            "runtime_group": getattr(route, "runtime_group", "") or "local",
            "runtime_endpoint": getattr(getattr(route, "provider", None), "base_url", ""),
            "runtime_warm": bool(health and health.get("model_available")),
            "runtime_reachable": bool(health and health.get("endpoint_healthy")),
            "runtime_model_available": bool(health and health.get("model_available")),
            "runtime_blocked_reason": self.selection_blocked_reason(route, health, route_enabled),
        }

    def score_adjustment(self, route: Any, health: dict[str, Any] | None, route_enabled: bool = True) -> int:
        if getattr(route, "is_cloud", False) or not health or not health.get("checked"):
            return 0
        if health.get("model_available"):
            return self.policy.warm_route_bonus
        return -self.policy.unavailable_penalty

    def selection_blocked_reason(self, route: Any, health: dict[str, Any] | None, route_enabled: bool = True) -> str:
        if getattr(route, "is_cloud", False) or not route_enabled or not health or not health.get("checked"):
            return ""
        if health.get("model_available"):
            return ""
        if not health.get("endpoint_healthy"):
            return "runtime_unreachable"
        return "runtime_model_not_warm"
