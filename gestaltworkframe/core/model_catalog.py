"""OpenRouter model catalog: the runtime source of truth for model facts.

Per `docs/standards/model-routing-policy.md`, a model id in application code
is a stale constant with a short shelf life. This module supplies the facts a
lane needs to *resolve* a model instead of naming one: price, capabilities,
context window, and published quality indices.

`GET https://openrouter.ai/api/v1/models` is public and unauthenticated. It is
cached on disk (24h by default) and backed by a small pinned fallback so a
catalog fetch failure degrades the routing decision rather than breaking the
application.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

CATALOG_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 10.0


def _cache_path() -> Path:
    override = os.getenv("MODEL_CATALOG_CACHE_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent.parent / "llm" / ".model-catalog-cache.json"


@dataclass(frozen=True)
class CatalogModel:
    """One model as the catalog describes it. No judgement, only facts."""

    id: str
    name: str = ""
    context_length: int = 0
    # USD per token, as OpenRouter reports them.
    prompt_price: float = 0.0
    completion_price: float = 0.0
    cache_read_price: float | None = None
    supported_parameters: frozenset[str] = field(default_factory=frozenset)
    intelligence_index: float | None = None
    coding_index: float | None = None
    agentic_index: float | None = None
    expiration_date: str = ""

    @property
    def is_free_tier(self) -> bool:
        """`:free` variants are generally trained on submitted prompts.

        Excluded as a data-handling filter before price is considered: a cost
        ranking would otherwise always select them, because zero is a
        degenerate optimum.
        """
        return self.id.endswith(":free") or self.id.endswith(":free-tier")

    @property
    def is_preview(self) -> bool:
        """Preview/experimental builds get rotated without notice."""
        lowered = self.id.lower()
        return any(marker in lowered for marker in ("-preview", "-exp", ":alpha", ":beta", "-experimental"))

    def supports(self, parameter: str) -> bool:
        return parameter in self.supported_parameters

    def cost_per_turn(self, expected_in: int, expected_out: int, cached_in: int = 0) -> float:
        """Cost of one representative turn, in USD.

        Ranking on the prompt price alone is the common bug: completion is
        routinely priced several times input, so a model that is cheap to
        prompt can be expensive to answer. When a stable prefix is replayed
        each turn, the cached-read rate applies to that portion.
        """
        fresh_in = max(0, expected_in - cached_in)
        cost = fresh_in * self.prompt_price + expected_out * self.completion_price
        if cached_in:
            rate = self.cache_read_price if self.cache_read_price is not None else self.prompt_price
            cost += cached_in * rate
        return cost


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_model(payload: dict[str, Any]) -> CatalogModel | None:
    """Convert one raw catalog entry. Returns None if it has no usable id."""
    model_id = str(payload.get("id") or "").strip()
    if not model_id:
        return None
    pricing = payload.get("pricing") or {}
    benchmarks = ((payload.get("benchmarks") or {}).get("artificial_analysis")) or {}
    params = payload.get("supported_parameters") or []
    return CatalogModel(
        id=model_id,
        name=str(payload.get("name") or model_id),
        context_length=int(payload.get("context_length") or 0),
        prompt_price=_as_float(pricing.get("prompt")),
        completion_price=_as_float(pricing.get("completion")),
        cache_read_price=_optional_float(pricing.get("input_cache_read")),
        supported_parameters=frozenset(str(p) for p in params),
        intelligence_index=_optional_float(benchmarks.get("intelligence_index")),
        coding_index=_optional_float(benchmarks.get("coding_index")),
        agentic_index=_optional_float(benchmarks.get("agentic_index")),
        expiration_date=str(payload.get("expiration_date") or ""),
    )


def parse_catalog(raw: dict[str, Any] | list[Any]) -> list[CatalogModel]:
    entries = raw.get("data", []) if isinstance(raw, dict) else raw
    models: list[CatalogModel] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            parsed = parse_model(entry)
            if parsed is not None:
                models.append(parsed)
    return models


# Not a "best models" table and must not grow into one.
PINNED_FALLBACK: tuple[CatalogModel, ...] = (
    # A deliberately small pinned fallback so a catalog outage degrades the
    # decision instead of breaking the app. Values are copied verbatim from the
    # live catalog (2026-07-25) rather than estimated — a fallback with missing
    # indices silently fails every lane that declares a floor, which is worse
    # than having no fallback at all. Prices are USD per token.
    CatalogModel(
        id="anthropic/claude-haiku-4.5",
        name="Claude Haiku 4.5 (pinned fallback)",
        context_length=200_000,
        prompt_price=1.0 / 1_000_000,
        completion_price=5.0 / 1_000_000,
        cache_read_price=0.1 / 1_000_000,
        supported_parameters=frozenset({"tools", "tool_choice", "structured_outputs", "reasoning"}),
        intelligence_index=29.6,
        coding_index=43.9,
        agentic_index=16.4,
    ),
    CatalogModel(
        id="anthropic/claude-sonnet-4.6",
        name="Claude Sonnet 4.6 (pinned fallback)",
        context_length=1_000_000,
        prompt_price=3.0 / 1_000_000,
        completion_price=15.0 / 1_000_000,
        cache_read_price=0.3 / 1_000_000,
        supported_parameters=frozenset({"tools", "tool_choice", "structured_outputs", "reasoning"}),
        intelligence_index=47.2,
        coding_index=63.0,
        agentic_index=40.8,
    ),
    CatalogModel(
        id="anthropic/claude-opus-5",
        name="Claude Opus 5 (pinned fallback)",
        context_length=1_000_000,
        prompt_price=5.0 / 1_000_000,
        completion_price=25.0 / 1_000_000,
        cache_read_price=0.5 / 1_000_000,
        supported_parameters=frozenset({"tools", "tool_choice", "structured_outputs", "reasoning"}),
        intelligence_index=60.7,
        coding_index=78.0,
        agentic_index=55.3,
    ),
)

def _read_cache(path: Path, ttl_seconds: int) -> list[CatalogModel] | None:
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = float(payload.get("fetched_at") or 0)
        if time.time() - fetched_at > ttl_seconds:
            return None
        models = parse_catalog(payload.get("catalog") or {})
        return models or None
    except (OSError, ValueError) as exc:
        logger.warning("model catalog cache unreadable at %s: %s", path, exc)
        return None


def _write_cache(path: Path, raw: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"fetched_at": time.time(), "catalog": raw}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("could not write model catalog cache to %s: %s", path, exc)


async def fetch_catalog(
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
    cache_path: Path | None = None,
    force_refresh: bool = False,
) -> list[CatalogModel]:
    """Return the model catalog: cache, then network, then pinned fallback.

    Never raises for a network problem. A routing decision made against a
    stale-but-real catalog is far better than an exception on a user turn.
    """
    path = cache_path or _cache_path()
    if not force_refresh:
        cached = _read_cache(path, ttl_seconds)
        if cached:
            return cached

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        response = await http.get(CATALOG_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        raw = response.json()
        models = parse_catalog(raw)
        if not models:
            raise ValueError("catalog returned no usable models")
        _write_cache(path, raw)
        return models
    except Exception as exc:  # network, HTTP, or shape problems all degrade the same way
        logger.warning("model catalog fetch failed (%s); falling back", exc)
        stale = _read_cache(path, ttl_seconds=10 * 365 * 24 * 3600)
        if stale:
            logger.warning("using stale model catalog cache from %s", path)
            return stale
        logger.warning("using pinned fallback catalog of %d models", len(PINNED_FALLBACK))
        return list(PINNED_FALLBACK)
    finally:
        if owns_client:
            await http.aclose()


def index_by_id(models: Iterable[CatalogModel]) -> dict[str, CatalogModel]:
    return {model.id: model for model in models}
