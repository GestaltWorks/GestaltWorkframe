"""Resolve a lane to an ordered shortlist of models, at runtime.

Filter -> floor -> rank, per `docs/standards/model-routing-policy.md`:

    fetch the catalog -> drop anything missing a hard requirement ->
    drop expired/deprecated -> apply the quality floor -> rank by `prefer` ->
    take the top

Two rules this module exists to enforce, because both were violated by the
static profile table it replaces:

* Availability is observed, not published. A model that benchmarks well and
  prices well is worthless while it is timing out, so a caller-supplied bench
  set removes recently-failing models from the candidate set.
* Fail sideways, not upward. The result is a *shortlist* that all cleared the
  same floors, so a retry stays in the lane instead of jumping to the most
  expensive route on the first error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from gestaltworkframe.core.model_catalog import CatalogModel
from gestaltworkframe.core.model_lanes import Lane

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    """A model that cleared the lane, with the numbers behind the decision."""

    model: CatalogModel
    lane: str
    cost_per_turn_usd: float
    quality_score: float

    @property
    def id(self) -> str:
        return self.model.id


@dataclass(frozen=True)
class Rejection:
    model_id: str
    reason: str


@dataclass(frozen=True)
class Resolution:
    """The shortlist plus why everything else lost: routing must be auditable."""

    lane: str
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def explain(self) -> str:
        if not self.candidates:
            return f"lane {self.lane}: no model cleared the requirements ({len(self.rejections)} rejected)"
        head = self.candidates[0]
        return (
            f"lane {self.lane}: {head.id} "
            f"(${head.cost_per_turn_usd:.5f}/turn, quality {head.quality_score:.1f}); "
            f"{len(self.candidates) - 1} alternate(s) held for retry"
        )


def _is_expired(model: CatalogModel, now: datetime) -> bool:
    if not model.expiration_date:
        return False
    raw = model.expiration_date.strip().replace("Z", "+00:00")
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


def _quality_score(model: CatalogModel, lane: Lane) -> float:
    """Rank quality on the indices the lane actually cares about."""
    wanted: list[float] = []
    if lane.min_intelligence is not None and model.intelligence_index is not None:
        wanted.append(model.intelligence_index)
    if lane.min_coding is not None and model.coding_index is not None:
        wanted.append(model.coding_index)
    if lane.min_agentic is not None and model.agentic_index is not None:
        wanted.append(model.agentic_index)
    if wanted:
        return sum(wanted) / len(wanted)
    # No floor declared: fall back to the general index so ordering is stable.
    return model.intelligence_index or 0.0


def resolve_lane(
    lane: Lane,
    catalog: Iterable[CatalogModel],
    *,
    benched: Sequence[str] | set[str] = (),
    now: datetime | None = None,
) -> Resolution:
    """Return the ordered shortlist for `lane`, and why each loser lost."""

    moment = now or datetime.now(timezone.utc)
    benched_ids = set(benched)
    price_ceiling = lane.price_ceiling_per_token()

    candidates: list[Candidate] = []
    rejections: list[Rejection] = []

    for model in catalog:
        # --- data handling first: a route is only cheap if it may see the payload
        if model.is_free_tier and not lane.allow_free_tier:
            rejections.append(Rejection(model.id, "free tier excluded (trains on prompts)"))
            continue
        if model.is_preview and not lane.allow_preview:
            rejections.append(Rejection(model.id, "preview/experimental build excluded"))
            continue

        # --- observed availability
        if model.id in benched_ids:
            rejections.append(Rejection(model.id, "benched: recent failures"))
            continue

        # --- hard requirements
        missing = [p for p in lane.must if not model.supports(p)]
        if missing:
            rejections.append(Rejection(model.id, f"missing required parameter(s): {', '.join(missing)}"))
            continue
        if lane.min_context_tokens and model.context_length < lane.min_context_tokens:
            rejections.append(
                Rejection(model.id, f"context {model.context_length} < {lane.min_context_tokens}")
            )
            continue
        if _is_expired(model, moment):
            rejections.append(Rejection(model.id, f"expired {model.expiration_date}"))
            continue
        if price_ceiling is not None and model.prompt_price > price_ceiling:
            rejections.append(Rejection(model.id, "prompt price above lane ceiling"))
            continue

        # --- quality floors
        floor_failed = False
        for floor, value, label in (
            (lane.min_intelligence, model.intelligence_index, "intelligence"),
            (lane.min_coding, model.coding_index, "coding"),
            (lane.min_agentic, model.agentic_index, "agentic"),
        ):
            if floor is None:
                continue
            if value is None:
                rejections.append(Rejection(model.id, f"no published {label} index for a lane that floors it"))
                floor_failed = True
                break
            if value < floor:
                rejections.append(Rejection(model.id, f"{label} {value} < {floor}"))
                floor_failed = True
                break
        if floor_failed:
            continue

        candidates.append(
            Candidate(
                model=model,
                lane=lane.name,
                cost_per_turn_usd=model.cost_per_turn(
                    lane.expected_input_tokens,
                    lane.expected_output_tokens,
                    lane.expected_cached_input_tokens,
                ),
                quality_score=_quality_score(model, lane),
            )
        )

    if lane.prefer == "cost":
        candidates.sort(key=lambda c: (c.cost_per_turn_usd, -c.quality_score, c.id))
    else:
        candidates.sort(key=lambda c: (-c.quality_score, c.cost_per_turn_usd, c.id))

    shortlist = tuple(candidates[: max(1, lane.shortlist_size)])
    resolution = Resolution(lane=lane.name, candidates=shortlist, rejections=tuple(rejections))
    logger.info("model resolution: %s", resolution.explain())
    return resolution
