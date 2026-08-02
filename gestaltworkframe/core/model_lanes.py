"""Task lanes: what a turn *requires*, expressed as a policy record.

A lane never names a model and it never carries an objective. It states
objective, machine-checkable requirements: hard filters, quality floors, and
the expected shape of the turn. `model_resolver` turns that plus a TIER passed
per call into an ordered shortlist against the live catalog.

Per `docs/standards/model-routing-policy.md`:

    guide:  must:[tools], minContext:200k, minAgentic:35, minIntelligence:45,
            shape:{in:6k, out:900, cached:4k}
    lookup: must:[tools], minIntelligence:28, maxPromptPrice:$1/M,
            shape:{in:1.5k, out:300}

    resolve_lane(GUIDE_LANE, catalog, tier="best")
    resolve_lane(LOOKUP_LANE, catalog, tier="cheap")

The lane answers "what does this turn require, and what shape is it". The tier
answers "among the models that already qualify, what should win". Collapsing
them onto one axis is a defect with measured consequences: the objective ends
up selecting the requirement, there is no way to ask for the same lane at a
different objective, and a cost preference sitting inside an eligibility record
reads as a property of the task when it is a choice somebody made.

Lanes are configuration. They live in a deployment bundle
(`deployments/<id>/lanes.yaml`) so a deployment can retune its own standards
without a code change, and fall back to the defaults below.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class Lane(BaseModel):
    """One task lane. Requirements are hard filters; preference orders survivors."""

    name: str
    description: str = ""

    # --- hard requirements (objective, machine-checkable) ------------------
    must: list[str] = Field(default_factory=list)
    """Required entries in the catalog's `supported_parameters`.

    Tool-calling is a filter, never a preference: a cheap model that cannot
    call tools is not cheap, it is broken.
    """

    min_context_tokens: int = 0
    max_prompt_price_per_million: float | None = None

    # --- quality floors (published indices, not vibes) ---------------------
    min_intelligence: float | None = None
    min_coding: float | None = None
    min_agentic: float | None = None

    # --- data-handling filters --------------------------------------------
    #
    # There is deliberately NO `allow_free_tier` field, and there must never be
    # one again. `:free` and `:batch` exclusions are unliftable: the first
    # because free tiers generally train on submitted prompts and no setting
    # makes a training corpus forget, the second because a batch SKU publishes
    # its interactive sibling's indices, so it clears every floor, undercuts the
    # real endpoint by half, and then fails a synchronous turn by arriving
    # hours later. Lanes load from a per-deployment lanes.yaml, so a lane field
    # here IS an operator setting, which is exactly what may not lift these.

    allow_preview: bool = False
    """Preview endpoints get rotated; a live session must not break on one.

    This is the only exclusion a lane may lift, because it is a stability
    opinion rather than a data-handling or delivery-contract rule."""

    # --- turn shape --------------------------------------------------------
    #
    # No `prefer` field. The objective is the TIER and it is passed per call.
    expected_input_tokens: int = 2_000
    expected_output_tokens: int = 500
    expected_cached_input_tokens: int = 0
    """Declared turn shape. A lookup and a long-form build are not the same
    turn, and ranking both on one assumed shape picks the wrong model."""

    prefer_vendors: list[str] = Field(default_factory=list)
    """Ordered vendor prefixes, used as a TIE-BREAK KEY and nothing else.

    Default empty, and it sits BELOW every measured axis and below cost, one
    place above the model id. That placement is the whole of what makes it
    survivable. A stored name preference is stale-prone in proportion to its
    AUTHORITY, not its existence: as the lowest key, name a vendor that no
    longer clears the floors and it is skipped; name one that is no longer best
    and the measured keys have already placed something above it; name nothing
    relevant and the order falls through to the id. It can only separate models
    the measured axes have already declared EQUAL, which is the only place
    taste is the remaining information.

    It used to be the FIRST sort key, ahead of margin and cost, which made it
    the selector rather than the tie-break: "resolve to a shortlist, then order
    it by a stored human preference" is the mechanism
    docs/standards/model-routing-policy.md records as built, run, and deleted.
    """

    shortlist_size: int = 3
    """How many candidates to keep for fail-sideways retries before any
    escalation. A single pinned fallback turns a cheap provider's bad ten
    minutes into a frontier-priced turn."""

    def price_ceiling_per_token(self) -> float | None:
        if self.max_prompt_price_per_million is None:
            return None
        return self.max_prompt_price_per_million / 1_000_000


DEFAULT_LANES: tuple[Lane, ...] = (
    Lane(
        name="lookup",
        description="Short factual turns, retrieval answers, classification.",
        must=["tools"],
        # Set just below the cheapest pinned-fallback model (29.6) on purpose:
        # a floor of 30 excluded every fallback, so a catalog outage left the
        # cheap lane unroutable. A lane's floor has to be reachable by the
        # models we ship for the outage case, not only by the live catalog.
        min_intelligence=28,
        max_prompt_price_per_million=1.0,
        expected_input_tokens=1_500,
        expected_output_tokens=300,
    ),
    Lane(
        name="guide",
        description="Guided conversation and explanation: the default chat turn.",
        must=["tools"],
        min_context_tokens=200_000,
        # Calibrated against the published distribution (2026-07-25, n=107):
        # intelligence p50=30.3 p90=51.4 max=60.7; agentic p50=18.2 p90=44.4
        # max=55.3. Well above median, comfortably below the ceiling, so the
        # lane keeps a real shortlist instead of resolving to one vendor.
        min_intelligence=45,
        min_agentic=35,
        expected_input_tokens=6_000,
        expected_output_tokens=900,
        expected_cached_input_tokens=4_000,
    ),
    Lane(
        name="build",
        description="Code generation and refactors judged on correctness.",
        must=["tools", "structured_outputs"],
        min_context_tokens=200_000,
        min_coding=60,
        expected_input_tokens=12_000,
        expected_output_tokens=2_500,
        expected_cached_input_tokens=8_000,
    ),
    Lane(
        name="review",
        description="Security and release-readiness review; mistakes are expensive.",
        must=["tools", "reasoning"],
        min_context_tokens=200_000,
        # The top of the lane. A floor of 70 intelligence was unreachable: the
        # published index maxes at 60.7, so the lane silently matched nothing.
        # Floors are reviewable numbers, and reviewing one means checking it
        # against the distribution rather than assuming a 0-100 scale.
        min_intelligence=55,
        min_coding=70,
        expected_input_tokens=20_000,
        expected_output_tokens=3_000,
        expected_cached_input_tokens=12_000,
    ),
)


def _lanes_path(deployment_dir: Path | None) -> Path | None:
    if deployment_dir is None:
        return None
    candidate = deployment_dir / "lanes.yaml"
    return candidate if candidate.is_file() else None


# Keys a deployment might reach for to re-enable an unliftable exclusion. They
# are dropped and reported rather than silently ignored, so an operator finds
# out that the knob does not exist instead of believing it worked.
UNLIFTABLE_KEYS = ("allow_free_tier", "allow_batch_tier", "allow_router_models")


def load_lanes(deployment_dir: Path | None = None) -> dict[str, Lane]:
    """Load lanes from a deployment bundle, falling back to the defaults.

    A malformed lane file never takes the app down: it is logged and the
    defaults stand.
    """
    lanes: dict[str, Lane] = {lane.name: lane for lane in DEFAULT_LANES}
    path = _lanes_path(deployment_dir)
    if path is None:
        return lanes
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        declared = payload.get("lanes", payload) if isinstance(payload, dict) else {}
        if not isinstance(declared, dict):
            raise ValueError("lanes.yaml must map lane name -> lane record")
        for name, record in declared.items():
            if not isinstance(record, dict):
                continue
            record = dict(record)
            for key in UNLIFTABLE_KEYS:
                if record.pop(key, None) is not None:
                    logger.warning(
                        "lanes.yaml lane %r sets %s; that exclusion is a data-handling "
                        "rule and cannot be lifted by configuration. Ignoring it.",
                        name,
                        key,
                    )
            lanes[str(name)] = Lane(name=str(name), **record)
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("lanes.yaml at %s unusable (%s); using defaults", path, exc)
    return lanes
