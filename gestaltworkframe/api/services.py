"""Application services container and dependency-injection helpers.

`AppServices` holds every long-lived collaborator the FastAPI app depends on:
provider router, cloud-budget gate, orchestrator, chat-turn engine, in-process
chat metrics. Built once at lifespan startup, retrieved per request via
`get_app_services`, torn down via `AppServices.close`.

`ChatMetrics` lives here because it shares the same lifecycle: instantiated as
part of `AppServices`, snapshotted by the admin health route, mutated by the
chat path. Keeping it in this module avoids a circular import between
`api/main.py` (which constructs services at startup) and `api/chat.py` (which
records metrics on each turn).

`require_admin_token` is the admin-route auth dependency. It also lives here so
admin and discovery sub-modules can import it without going through the
top-level app module.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, Header, HTTPException, Request

from gestaltworkframe.core.chat_orchestrator import ChatTurnOrchestrator
from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
from gestaltworkframe.core.orchestrator import Orchestrator
from gestaltworkframe.core.policy import CloudSpendPolicy
from gestaltworkframe.core.key_store import ApiKeyStore
from gestaltworkframe.core.provider_balance import OpenRouterBalanceChecker
from gestaltworkframe.core.provider_registry import ProviderRegistry
from gestaltworkframe.core.retrieval import KnowledgeRetriever
from gestaltworkframe.core.router import LLMRouter
from gestaltworkframe.core.runtime import GenerationConcurrencyPolicy, RuntimeControlPolicy, RuntimeManager

logger = logging.getLogger(__name__)


@dataclass
class ChatMetrics:
    """In-memory chat-turn counters.

    Reset on every API process restart. Persisted snapshots and history are
    out of scope for this container; if metrics need to survive deploys they
    should be exported to a separate store (Prometheus, SQLite, etc.). The
    `record` and `snapshot` methods are async because they take the metrics
    lock to keep counter updates consistent under concurrent turns.
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_turns: int = 0
    total_duration_ms: int = 0
    total_output_tokens: int = 0
    total_output_chars: int = 0
    last_turn_at: datetime | None = None
    by_status: Counter[str] = field(default_factory=Counter)
    by_mode: Counter[str] = field(default_factory=Counter)
    by_intent: Counter[str] = field(default_factory=Counter)
    by_route: Counter[str] = field(default_factory=Counter)
    by_route_family: Counter[str] = field(default_factory=Counter)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record(self, payload: dict[str, Any]) -> None:
        status_value = str(payload.get("status") or "unknown")
        mode = str(payload.get("mode") or "unknown")
        intent = str(payload.get("intent") or "unknown")
        route = str(payload.get("selected_route") or "none")
        family = str(payload.get("selected_route_family") or "none")
        duration_ms = int(payload.get("duration_ms") or 0)
        output_tokens = int(payload.get("output_tokens_estimate") or 0)
        output_chars = int(payload.get("output_chars") or 0)

        async with self.lock:
            self.total_turns += 1
            self.total_duration_ms += max(duration_ms, 0)
            self.total_output_tokens += max(output_tokens, 0)
            self.total_output_chars += max(output_chars, 0)
            self.last_turn_at = datetime.now(timezone.utc)
            self.by_status[status_value] += 1
            self.by_mode[mode] += 1
            self.by_intent[intent] += 1
            self.by_route[route] += 1
            self.by_route_family[family] += 1

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            completed = self.by_status.get("completed", 0)
            failed = self.by_status.get("failed", 0)
            avg_duration_ms = round(self.total_duration_ms / self.total_turns) if self.total_turns else 0
            avg_output_tokens = round(self.total_output_tokens / self.total_turns) if self.total_turns else 0
            return {
                "started_at": self.started_at.isoformat(),
                "last_turn_at": self.last_turn_at.isoformat() if self.last_turn_at else None,
                "total_turns": self.total_turns,
                "completed_turns": completed,
                "failed_turns": failed,
                "failure_rate": round(failed / self.total_turns, 4) if self.total_turns else 0,
                "avg_duration_ms": avg_duration_ms,
                "avg_output_tokens_estimate": avg_output_tokens,
                "total_output_tokens_estimate": self.total_output_tokens,
                "total_output_chars": self.total_output_chars,
                "by_status": dict(self.by_status),
                "by_mode": dict(self.by_mode),
                "by_intent": dict(self.by_intent),
                "by_route": dict(self.by_route),
                "by_route_family": dict(self.by_route_family),
            }


@dataclass
class AppServices:
    local_provider: Any
    secondary_provider: Any | None
    cloud_budget: CloudBudgetGate | MultiProviderBudgetGate
    llm_router: LLMRouter
    orchestrator: Orchestrator
    chat_turns: ChatTurnOrchestrator
    balance_checker: OpenRouterBalanceChecker | None = None
    key_store: ApiKeyStore | None = None
    chat_metrics: ChatMetrics = field(default_factory=ChatMetrics)

    async def close(self) -> None:
        # The router owns every provider via its ProviderRoute list and closes
        # them collectively. The historical per-provider fallback branch is
        # unreachable: LLMRouter always exposes `close`.
        await self.llm_router.close()


async def build_app_services() -> AppServices:
    # key_store already created above
    await key_store.init()
    registry = ProviderRegistry.from_env()
    registry.key_store = key_store
    registry.admin_token = os.getenv("ADMIN_TOKEN", "")
    routes = await registry.build_routes()
    local_provider = next((route.provider for route in routes if route.provider and not route.is_cloud), None)
    if local_provider is None:
        local_provider = registry.build_primary()
    secondary_provider = next((route.provider for route in routes if route.provider and route.is_cloud), None)
    _global_gate = CloudBudgetGate(CloudBudgetConfig.from_env())
    cloud_budget = MultiProviderBudgetGate.from_env(_global_gate)
    _or_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    balance_checker = OpenRouterBalanceChecker(_or_key) if _or_key else None
    # key_store already created above
    runtime_manager = RuntimeManager(RuntimeControlPolicy.from_env())
    llm_router = LLMRouter(
        primary=local_provider,
        secondary=secondary_provider,
        cloud_budget=cloud_budget,
        routes=routes,
        routing_strategy=os.getenv("ROUTING_STRATEGY", "best_value"),
        runtime_manager=runtime_manager,
        generation_concurrency_policy=GenerationConcurrencyPolicy.from_env(),
    )
    orchestrator = Orchestrator(CloudSpendPolicy.from_env())
    chat_turns = ChatTurnOrchestrator(orchestrator, llm_router, KnowledgeRetriever())
    return AppServices(local_provider, secondary_provider, cloud_budget, llm_router, orchestrator, chat_turns, balance_checker, key_store, ChatMetrics())


def get_app_services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Application services are not initialized")
    return services


def require_admin_token(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    configured = os.getenv("ADMIN_POLICY_TOKEN", "").strip()
    token = (x_admin_token or "").strip()
    if configured:
        if token == configured:
            return
        raise HTTPException(status_code=401, detail="Invalid admin token")

    if _is_loopback_client(request) and token == "local-dev-admin":
        return
    raise HTTPException(status_code=503, detail="Admin policy token is not configured")


def _is_loopback_client(request: Request) -> bool:
    host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def enabled_cost_tiers(services: AppServices) -> set[str]:
    """Return the set of cost tiers an admin policy currently enables.

    Public to the admin module; shaped as a free function rather than a method
    on AppServices because it reads policy state, not service state.
    """

    tiers = set()
    policy = services.orchestrator.cloud_policy
    if policy.low_cost_enabled:
        tiers.add("low_cost")
    if policy.claude_enabled:
        tiers.add("premium")
    return tiers
