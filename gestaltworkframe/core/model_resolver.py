"""Resolve a lane to an ordered shortlist of models, at runtime.

Two orthogonal axes, per `docs/standards/model-routing-policy.md`:

* The LANE states the REQUIREMENT: hard filters, quality floors, and the
  expected shape of the turn. It never names a model and it carries no
  objective.
* The TIER states the OBJECTIVE among the models that already qualify. It is
  passed per call and is never welded into the lane record.

Resolution order, and nothing after step 2 may re-admit anything step 2 removed:

    1. take the catalog
    2. drop anything failing a hard requirement, a data-handling rule, a
       delivery contract, an expiry, or the availability bench -- before price
    3. apply the quality floors
    4. cost the turn at the lane's declared shape; drop anything that cannot be
       costed or that exceeds a declared ceiling
    5. apply the tier's gate
    6. rank by the tier's comparator, to a TOTAL order ending in the model id
    7. take the top, log the decision

Three rules this module exists to enforce, because all three were violated by
the static profile table it replaces:

* Availability is observed, not published. A model that benchmarks well and
  prices well is worthless while it is timing out, so a caller-supplied bench
  set removes recently-failing models from the candidate set.
* Fail sideways, not upward. The result is a *shortlist* that all cleared the
  same floors, so a retry stays in the lane instead of jumping to the most
  expensive route on the first error.
* The reason string must name the objective actually used. A wrong route gets
  caught by a bad answer; a wrong REPORT just makes a human confidently
  mistaken.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Mapping, Sequence

from gestaltworkframe.core.model_catalog import CatalogModel
from gestaltworkframe.core.model_lanes import Lane

logger = logging.getLogger(__name__)

Tier = Literal["best", "auto", "fast", "cheap"]

DEFAULT_TIER: Tier = "auto"
"""`auto` is the default. `cheap` is a declaration somebody owns, never a
default: as a hidden default it makes the FLOOR THE TARGET, so the system
systematically runs the worst model that technically passes, including on the
lanes whose output is the product."""

TIER_OBJECTIVES: dict[str, str] = {
    "best": "maximum margin, cost breaks ties",
    "auto": "maximum margin per dollar above the margin-share gate",
    "fast": "minimum seconds x cost above the margin-share gate",
    "cheap": "lowest cost above the bar, margin breaks ties",
}

# A pure margin-per-dollar ratio has a degenerate optimum: "barely adequate and
# nearly free" wins by construction. Measured on the live catalog, a model
# clearing the bar by 0.3 points at $0.0090/turn scores 33.3 margin-points per
# dollar and beats claude-opus-5's 29.4. So a candidate must reach a declared
# SHARE of the best available margin before its ratio is allowed to count: the
# floor is the hard requirement, the share is the "and not by a hair" clause.
#
# 0.35, calibrated 2026-08-01 against this repo's lane shapes and the catalog
# capture behind PINNED_FALLBACK, not inherited. Sweep at 0.00 / 0.35 / 0.55 on
# the four default lanes: 0.00 puts every lane on the cheapest thing that
# passes, which is the hidden-cheap default this whole module exists to remove;
# 0.35 lifts `guide` and `build` off the floor while leaving `lookup` on the
# cheap-and-competent option, which is what those lanes are for; 0.55 over-prunes
# `lookup` to a single candidate, at which point the ratio does no work at all.
# Re-derive this when the catalog moves, and write the sweep down next to it.
MARGIN_SHARE_GATE = 0.35


@dataclass(frozen=True)
class Candidate:
    """A model that cleared the lane, with the numbers behind the decision."""

    model: CatalogModel
    lane: str
    cost_per_turn_usd: float
    margin: float
    """Headroom over the lane's bar, per axis, summed. See `_margin`."""

    observed_seconds: float | None = None
    """Measured turn latency, when anything has measured it. Never looked up:
    the catalog publishes `latency_last_30m` and `throughput_last_30m` as null
    on every endpoint, so ranking on them would be ranking on nothing."""

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
    tier: Tier = DEFAULT_TIER
    objective: str = TIER_OBJECTIVES[DEFAULT_TIER]
    cleared_floors: int = 0
    survived_gate: int = 0
    best_available_margin: float = 0.0

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def explain(self) -> str:
        head = f'lane {self.lane} tier={self.tier} objective="{self.objective}"'
        if not self.candidates:
            return f"{head}: no model cleared the requirements ({len(self.rejections)} rejected)"
        top = self.candidates[0]
        return (
            f"{head}: {top.id} "
            f"(${top.cost_per_turn_usd:.5f}/turn, margin {top.margin:.1f} "
            f"of best available {self.best_available_margin:.1f}); "
            f"{self.cleared_floors} cleared the lane, {self.survived_gate} survived the gate; "
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


def _margin(model: CatalogModel, lane: Lane) -> float:
    """Headroom over the bar, per axis, summed.

    Measured against the BAR, not in absolute index points. The floor encodes
    what the turn actually requires; capability below it is worth nothing
    because the model is excluded outright, so only the surplus above it can
    help this turn. Per-axis and summed, never a max or an average: the filter
    enforces every declared gate, so every surplus is real and additive, and
    averaging would dilute a genuine surplus with an axis the lane did not care
    about.

    Summed over the axes the LANE DECLARES, for that same reason. The doctrine
    writes the formula over intelligence and agentic because those are the two
    axes it floors; this repo also floors coding, and an axis with no declared
    floor is one the lane did not care about, so folding its full absolute index
    in would be the dilution the doctrine rejects. A lane declaring no floor at
    all falls back to intelligence headroom over zero, so ordering stays stable.
    """
    axes = (
        (lane.min_intelligence, model.intelligence_index),
        (lane.min_coding, model.coding_index),
        (lane.min_agentic, model.agentic_index),
    )
    declared = [(floor, value) for floor, value in axes if floor is not None]
    if not declared:
        return max(0.0, model.intelligence_index or 0.0)
    return sum(max(0.0, (value or 0.0) - floor) for floor, value in declared)


def _order(candidates: list[Candidate], tier: Tier) -> list[Candidate]:
    """Rank to a TOTAL order, always ending in the model id.

    Without the id as the final key an exact tie falls through to the sort's
    stability, which means the CATALOG'S ARRAY POSITION decides the lane and one
    upstream reordering flips the pick with no code change and no log line. Ties
    between one vendor's models are structural: vendors price whole families the
    same on purpose.

    Every key above the id is MEASURED. There is no stored vendor preference and
    no other name list anywhere in this order: a lane that should lead with a
    stronger model raises its floor and resolves at tier `best`.
    """
    if tier == "best":
        # The cost ceiling here is EMERGENT: nothing can exceed the price of the
        # top-margin model unless it has more margin, in which case it IS the
        # top-margin model. No dollar constant is needed, and none appears.
        key = lambda c: (-c.margin, c.cost_per_turn_usd, c.id)
    elif tier == "cheap":
        key = lambda c: (c.cost_per_turn_usd, -c.margin, c.id)
    elif tier == "fast":
        # Speed is OBSERVED, never looked up. An unmeasured model sorts BEHIND
        # every measured one and is never excluded, because a model that is
        # never picked can never earn its first measurement. When nothing is
        # measured this degenerates to cost with margin breaking ties, and the
        # reason string says so rather than implying a measurement we do not
        # have.
        key = lambda c: (
            c.observed_seconds is None,
            (c.observed_seconds or 0.0) * c.cost_per_turn_usd,
            c.cost_per_turn_usd,
            -c.margin,
            c.id,
        )
    else:  # auto
        key = lambda c: (
            -(c.margin / c.cost_per_turn_usd),
            c.cost_per_turn_usd,
            c.id,
        )
    return sorted(candidates, key=key)


def resolve_lane(
    lane: Lane,
    catalog: Iterable[CatalogModel],
    *,
    tier: Tier = DEFAULT_TIER,
    benched: Sequence[str] | set[str] = (),
    observed_seconds: Mapping[str, float] | None = None,
    now: datetime | None = None,
) -> Resolution:
    """Return the ordered shortlist for `lane` at `tier`, and why each loser lost."""

    if tier not in TIER_OBJECTIVES:
        logger.warning("unknown tier %r; using %s", tier, DEFAULT_TIER)
        tier = DEFAULT_TIER

    moment = now or datetime.now(timezone.utc)
    benched_ids = set(benched)
    latencies = dict(observed_seconds or {})
    price_ceiling = lane.price_ceiling_per_token()

    candidates: list[Candidate] = []
    rejections: list[Rejection] = []

    for model in catalog:
        # --- data handling and delivery contract first: a route is only cheap
        # --- if it may see the payload and can answer inside the turn.
        # --- Unliftable: there is no lane field, env var or config key here.
        if model.is_free_tier:
            rejections.append(Rejection(model.id, "free tier excluded (trains on prompts)"))
            continue
        if model.is_batch_tier:
            rejections.append(Rejection(model.id, "batch tier excluded (asynchronous delivery)"))
            continue
        if model.is_router_pseudo_model:
            rejections.append(
                Rejection(model.id, "router pseudo-model excluded (delegates the lane's decision)")
            )
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

        # --- cost the turn at the lane's declared shape
        cost = model.cost_per_turn(
            lane.expected_input_tokens,
            lane.expected_output_tokens,
            lane.expected_cached_input_tokens,
        )
        if not cost > 0:
            # A zero or negative computed cost is a sentinel, not a bargain, and
            # it is a degenerate optimum for every cost-aware tier. Reject it
            # independently of the id-based router-pseudo-model check above, so
            # a new meta-router under an unfamiliar slug still cannot win.
            rejections.append(Rejection(model.id, f"non-positive cost per turn ({cost})"))
            continue

        candidates.append(
            Candidate(
                model=model,
                lane=lane.name,
                cost_per_turn_usd=cost,
                margin=_margin(model, lane),
                observed_seconds=latencies.get(model.id),
            )
        )

    cleared_floors = len(candidates)
    best_margin = max((c.margin for c in candidates), default=0.0)

    # --- the tier's gate. `best` and `cheap` rank the whole qualifying set;
    # --- `auto` and `fast` rank only what reaches a share of the best margin.
    if tier in {"auto", "fast"} and best_margin > 0:
        threshold = MARGIN_SHARE_GATE * best_margin
        gated = [c for c in candidates if c.margin >= threshold]
        kept = {c.id for c in gated}
        for loser in candidates:
            if loser.id not in kept:
                rejections.append(
                    Rejection(
                        loser.id,
                        f"margin {loser.margin:.1f} below the {MARGIN_SHARE_GATE:.0%} "
                        f"share of best available margin {best_margin:.1f}",
                    )
                )
        candidates = gated

    objective = TIER_OBJECTIVES[tier]
    if tier == "fast" and not any(c.observed_seconds is not None for c in candidates):
        # Say what was actually done. Implying a measurement we do not have is
        # the failure mode where a human ends up confidently mistaken.
        objective = "no latency observed yet; lowest cost above the gate, margin breaks ties"

    ordered = _order(candidates, tier)
    shortlist = tuple(ordered[: max(1, lane.shortlist_size)])
    resolution = Resolution(
        lane=lane.name,
        candidates=shortlist,
        rejections=tuple(rejections),
        tier=tier,
        objective=objective,
        cleared_floors=cleared_floors,
        survived_gate=len(ordered),
        best_available_margin=best_margin,
    )
    logger.info("model resolution: %s", resolution.explain())
    return resolution
