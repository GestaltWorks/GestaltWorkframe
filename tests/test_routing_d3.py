"""Phase D3 (value-lean calibration): best-model-for-query routing tests.

The original D3 directive was "prefer best model for the query, then bounce
if that model isn't available." The value-lean calibration refines it: best
fit still wins, but adequate local/low-cost routes take ties so routine
public traffic stays local while genuinely hard turns escalate. Coordinated
changes verify this:

1. best_value applies a modest value lean toward cheaper tiers
   (local +120, low_cost +40, premium +0). Task fit still dominates
   (+1000 recommended_for, -2000 avoid_for), so a premium-only task match
   outranks the lean and escalates a hard turn.
2. Response policy defaults to LOCAL_THEN_CLAUDE_IF_HIGH_VALUE whenever
   claude is enabled (was previously gated to handoff/urgent only),
   so Sonnet is on the menu by default.
3. Routing frame tags build-intent queries as `complex_implementation`
   so Sonnet/Opus get a clean +1000 task-fit boost.
"""

from __future__ import annotations

import pytest

from gestaltworkframe.core.orchestrator import Orchestrator
from gestaltworkframe.core.policy import (
    ChatMode,
    CloudSpendPolicy,
    ResponsePolicy,
    ToneSignal,
    UserIntent,
    UserNeed,
)
from gestaltworkframe.core.router import ROUTE_COST_ADJUSTMENTS
from gestaltworkframe.core.routing_frame import classify_route


# ---- Change 1: best_value leans toward cheaper tiers ----------------------

def test_best_value_applies_value_lean_toward_cheaper_tiers():
    """A modest cost-tier lean (local > low_cost > premium) breaks ties for
    cheaper-but-adequate routes. Task fit (+1000/-2000) still dominates."""
    assert ROUTE_COST_ADJUSTMENTS["best_value"] == {"local": 120, "low_cost": 40, "premium": 0}


def test_other_strategies_still_express_a_lean():
    """Operator-chosen strategies (prefer_local, prefer_cloud_quality) still
    bias within-family ordering. They're explicit deviations from best fit."""
    assert ROUTE_COST_ADJUSTMENTS["prefer_local"]["local"] > 0
    assert ROUTE_COST_ADJUSTMENTS["prefer_cloud_quality"]["premium"] > 0


# ---- Change 2: response policy defaults to Sonnet eligibility -------------

def _orch(claude: bool = True, low_cost: bool = False) -> Orchestrator:
    return Orchestrator(cloud_policy=CloudSpendPolicy(
        claude_enabled=claude,
        low_cost_enabled=low_cost,
        max_calls_per_turn=1,
    ))


def test_default_policy_is_claude_eligible_when_claude_enabled():
    """A routine technical turn now lands on LOCAL_THEN_CLAUDE_IF_HIGH_VALUE,
    not LOCAL_ONLY/LOCAL_THEN_LOW_COST. Sonnet can compete on task fit."""
    orch = _orch(claude=True)
    decision = orch.decide(
        starting_mode="automator",
        message="how do I add a Jinja filter to a Automation workflow",
    )
    assert decision.response_policy == ResponsePolicy.LOCAL_THEN_CLAUDE_IF_HIGH_VALUE


def test_policy_falls_back_to_low_cost_when_only_low_cost_enabled():
    orch = _orch(claude=False, low_cost=True)
    decision = orch.decide(
        starting_mode="automator",
        message="how do I add a Jinja filter to a Automation workflow",
    )
    assert decision.response_policy == ResponsePolicy.LOCAL_THEN_LOW_COST


def test_policy_is_local_only_when_no_cloud_enabled():
    orch = _orch(claude=False, low_cost=False)
    decision = orch.decide(
        starting_mode="automator",
        message="how do I add a Jinja filter to a Automation workflow",
    )
    assert decision.response_policy == ResponsePolicy.LOCAL_ONLY


# ---- Change 3: complex_implementation task tag for build intent -----------

def test_routine_implementation_help_stays_at_implementation_help_task():
    """A simple how-do-I question without build-intent keeps the routine tag."""
    frame, _intent, _tone = classify_route(
        starting_mode="automator",
        message="how do I configure a webhook integration",
    )
    assert frame.need == UserNeed.IMPLEMENTATION_HELP
    assert frame.task == "implementation_help"


def test_build_intent_elevates_to_complex_implementation_task():
    """When the user expresses build intent the task tag elevates so
    Sonnet/Opus get a clean recommended_for match."""
    frame, _intent, _tone = classify_route(
        starting_mode="automator",
        message="i want to build a workflow that onboards new users",
    )
    assert frame.task == "complex_implementation"


def test_jinja_help_still_wins_over_complex_implementation():
    """Even with build intent, Jinja/ctx/tasks specifics keep the jinja_help
    tag. The more specific routine tag wins over complex_implementation, so the
    turn routes to a local/practitioner model rather than premium cloud."""
    frame, _intent, _tone = classify_route(
        starting_mode="automator",
        message="i want to build a jinja filter that lowercases",
    )
    # jinja_help branch takes priority over complex_implementation when both
    # signals are present - the more specific tag wins.
    assert frame.task == "jinja_help"


# ---- Integrated: building intent gets Sonnet on the menu ------------------

def test_build_intent_decision_full_shape():
    """End-to-end: build-intent question in Automator mode produces a
    decision that (a) keeps the mode in Automator, (b) sets
    soft_service_offer for the bridge sentence, (c) requests retrieval,
    (d) gates with LOCAL_THEN_CLAUDE_IF_HIGH_VALUE so Sonnet is eligible.
    """
    orch = _orch(claude=True)
    decision = orch.decide(
        starting_mode="automator",
        message="i want to build a workflow that automates user onboarding",
    )
    assert decision.selected_mode == ChatMode.AUTOMATOR
    assert decision.soft_service_offer is True
    assert decision.retrieval_required is True
    assert decision.response_policy == ResponsePolicy.LOCAL_THEN_CLAUDE_IF_HIGH_VALUE
    assert decision.frame.task == "complex_implementation"
