"""Capability-based model resolution.

Each test pins one rule from docs/standards/model-routing-policy.md. Together
they are the guard against the failure this replaces: a static profile table
with hardcoded ids, hand-assigned priorities, and stale prices, which in
production pointed at models the gateway did not serve and routed to `:free`
tiers by default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from gestaltworkframe.core.model_catalog import (
    CatalogModel,
    PINNED_FALLBACK,
    fetch_catalog,
    parse_catalog,
)
from gestaltworkframe.core.model_lanes import DEFAULT_LANES, Lane, load_lanes
from gestaltworkframe.core.model_resolver import resolve_lane

TOOLS = frozenset({"tools", "tool_choice"})


def _model(
    model_id: str,
    *,
    prompt: float = 1.0,
    completion: float = 5.0,
    context: int = 200_000,
    params: frozenset[str] = TOOLS,
    intelligence: float | None = 60.0,
    coding: float | None = 60.0,
    agentic: float | None = 60.0,
    cache_read: float | None = None,
    expires: str = "",
) -> CatalogModel:
    """Prices are given per million for readability."""
    return CatalogModel(
        id=model_id,
        name=model_id,
        context_length=context,
        prompt_price=prompt / 1_000_000,
        completion_price=completion / 1_000_000,
        cache_read_price=None if cache_read is None else cache_read / 1_000_000,
        supported_parameters=params,
        intelligence_index=intelligence,
        coding_index=coding,
        agentic_index=agentic,
        expiration_date=expires,
    )


# ---- hard filters --------------------------------------------------------

def test_tool_calling_is_a_filter_not_a_preference():
    """A cheap model that cannot call tools is not cheap, it is broken."""
    lane = Lane(name="t", must=["tools"], prefer="cost")
    cheap_no_tools = _model("vendor/cheap", prompt=0.01, completion=0.02, params=frozenset())
    pricier_with_tools = _model("vendor/capable", prompt=2.0, completion=8.0)

    result = resolve_lane(lane, [cheap_no_tools, pricier_with_tools])

    assert [c.id for c in result.candidates] == ["vendor/capable"]
    assert any("missing required parameter" in r.reason for r in result.rejections)


def test_free_tier_is_excluded_before_price_is_considered():
    """Zero is a degenerate optimum; `:free` trains on submitted prompts."""
    lane = Lane(name="t", must=["tools"], prefer="cost")
    free = _model("vendor/model:free", prompt=0.0, completion=0.0)
    paid = _model("vendor/model", prompt=1.0, completion=4.0)

    result = resolve_lane(lane, [free, paid])

    assert [c.id for c in result.candidates] == ["vendor/model"]
    assert any("free tier excluded" in r.reason for r in result.rejections)


def test_free_tier_can_be_opted_into_explicitly():
    lane = Lane(name="t", must=["tools"], prefer="cost", allow_free_tier=True)
    result = resolve_lane(lane, [_model("vendor/model:free", prompt=0.0, completion=0.0)])
    assert [c.id for c in result.candidates] == ["vendor/model:free"]


def test_preview_builds_are_excluded_from_user_facing_lanes():
    lane = Lane(name="t", must=["tools"], prefer="cost")
    result = resolve_lane(lane, [_model("vendor/gemini-3-pro-preview", prompt=0.1, completion=0.2)])
    assert result.candidates == ()
    assert any("preview" in r.reason for r in result.rejections)


def test_expired_models_are_dropped():
    lane = Lane(name="t", must=["tools"])
    stale = _model("vendor/retired", expires="2020-01-01T00:00:00Z")
    live = _model("vendor/current")

    result = resolve_lane(lane, [stale, live], now=datetime(2026, 7, 25, tzinfo=timezone.utc))

    assert [c.id for c in result.candidates] == ["vendor/current"]


def test_context_and_price_ceilings_are_enforced():
    lane = Lane(name="t", must=["tools"], min_context_tokens=200_000, max_prompt_price_per_million=1.0)
    small_context = _model("vendor/small", context=8_000)
    too_pricey = _model("vendor/pricey", prompt=5.0)
    fits = _model("vendor/fits", prompt=0.5)

    result = resolve_lane(lane, [small_context, too_pricey, fits])

    assert [c.id for c in result.candidates] == ["vendor/fits"]


# ---- quality floors ------------------------------------------------------

def test_quality_floors_use_published_indices():
    lane = Lane(name="t", must=["tools"], min_intelligence=70)
    weak = _model("vendor/weak", intelligence=40)
    strong = _model("vendor/strong", intelligence=85)

    result = resolve_lane(lane, [weak, strong])

    assert [c.id for c in result.candidates] == ["vendor/strong"]


def test_a_model_with_no_published_index_cannot_clear_a_floor():
    """Floors are reviewable numbers; an unmeasured model is not 'probably fine'."""
    lane = Lane(name="t", must=["tools"], min_agentic=50)
    unmeasured = _model("vendor/unknown", agentic=None)

    result = resolve_lane(lane, [unmeasured])

    assert result.candidates == ()
    assert any("no published agentic index" in r.reason for r in result.rejections)


# ---- ranking -------------------------------------------------------------

def test_ranking_uses_cost_per_turn_not_the_sticker_input_price():
    """Completion is priced several times input; ranking on prompt alone flips picks."""
    lane = Lane(
        name="t", must=["tools"], prefer="cost",
        expected_input_tokens=1_000, expected_output_tokens=2_000,
    )
    cheap_prompt_pricey_output = _model("vendor/trap", prompt=0.5, completion=30.0)
    honest = _model("vendor/honest", prompt=1.0, completion=3.0)

    result = resolve_lane(lane, [cheap_prompt_pricey_output, honest])

    assert result.best is not None
    assert result.best.id == "vendor/honest", "ranking on input price alone picks the trap"


def test_cache_reads_are_priced_when_a_prefix_is_replayed():
    lane = Lane(
        name="t", must=["tools"], prefer="cost",
        expected_input_tokens=10_000, expected_output_tokens=100,
        expected_cached_input_tokens=9_000,
    )
    cached = _model("vendor/cached", prompt=3.0, completion=15.0, cache_read=0.3)
    uncached = _model("vendor/uncached", prompt=1.0, completion=5.0)

    result = resolve_lane(lane, [cached, uncached])

    assert result.best is not None
    assert result.best.id == "vendor/cached", "a cached premium model can beat an uncached cheap one"


def test_quality_preference_orders_by_index_then_cost():
    lane = Lane(name="t", must=["tools"], min_intelligence=50, prefer="quality")
    good = _model("vendor/good", intelligence=60, prompt=0.1)
    better = _model("vendor/better", intelligence=90, prompt=9.0)

    result = resolve_lane(lane, [good, better])

    assert [c.id for c in result.candidates][0] == "vendor/better"


# ---- availability and fail-sideways --------------------------------------

def test_benched_models_are_removed_from_the_candidate_set():
    """Availability is observed, not published: a timing-out model is worthless."""
    lane = Lane(name="t", must=["tools"], prefer="cost")
    flaky = _model("vendor/flaky", prompt=0.1, completion=0.2)
    steady = _model("vendor/steady", prompt=1.0, completion=4.0)

    result = resolve_lane(lane, [flaky, steady], benched={"vendor/flaky"})

    assert [c.id for c in result.candidates] == ["vendor/steady"]
    assert any("benched" in r.reason for r in result.rejections)


def test_resolution_returns_a_shortlist_so_retries_fail_sideways():
    """A single pinned fallback turns a bad ten minutes into a frontier-priced turn."""
    lane = Lane(name="t", must=["tools"], prefer="cost", shortlist_size=3)
    catalog = [_model(f"vendor/m{i}", prompt=float(i + 1)) for i in range(5)]

    result = resolve_lane(lane, catalog)

    assert len(result.candidates) == 3
    assert [c.id for c in result.candidates] == ["vendor/m0", "vendor/m1", "vendor/m2"]


def test_resolution_explains_itself_for_the_operator():
    """Automatic selection without a visible decision is unauditable."""
    lane = Lane(name="guide", must=["tools"], prefer="cost")
    result = resolve_lane(lane, [_model("vendor/pick")])

    assert "vendor/pick" in result.explain()
    assert "guide" in result.explain()


def test_no_candidate_is_reported_rather_than_guessed():
    lane = Lane(name="t", must=["tools"], min_intelligence=99)
    result = resolve_lane(lane, [_model("vendor/ordinary", intelligence=50)])

    assert result.best is None
    assert "no model cleared" in result.explain()


# ---- lanes ---------------------------------------------------------------

def test_default_lanes_never_name_a_model():
    """The whole point: a lane declares requirements, not an id."""
    for lane in DEFAULT_LANES:
        blob = lane.model_dump_json().lower()
        for vendor in ("anthropic/", "openai/", "google/", "claude-", "gpt-", "gemini-"):
            assert vendor not in blob, f"lane {lane.name} names a model ({vendor})"


def test_default_lanes_require_tools_and_exclude_free_tiers():
    for lane in DEFAULT_LANES:
        assert "tools" in lane.must, f"lane {lane.name} must filter on tool support"
        assert lane.allow_free_tier is False, f"lane {lane.name} must not default to free tiers"


def test_lanes_load_from_a_deployment_bundle(tmp_path: Path):
    bundle = tmp_path / "egi"
    bundle.mkdir()
    (bundle / "lanes.yaml").write_text(
        "lanes:\n"
        "  guide:\n"
        "    must: [tools]\n"
        "    min_intelligence: 80\n"
        "    prefer: quality\n",
        encoding="utf-8",
    )

    lanes = load_lanes(bundle)

    assert lanes["guide"].min_intelligence == 80
    assert lanes["lookup"].name == "lookup", "unlisted defaults survive"


def test_malformed_lane_config_falls_back_to_defaults(tmp_path: Path):
    bundle = tmp_path / "broken"
    bundle.mkdir()
    (bundle / "lanes.yaml").write_text("lanes: [not, a, mapping]\n", encoding="utf-8")

    lanes = load_lanes(bundle)

    assert set(lanes) == {lane.name for lane in DEFAULT_LANES}


# ---- catalog -------------------------------------------------------------

def test_catalog_parses_the_openrouter_shape():
    raw = {
        "data": [
            {
                "id": "vendor/model",
                "name": "Vendor Model",
                "context_length": 128000,
                "pricing": {"prompt": "0.000001", "completion": "0.000005", "input_cache_read": "0.0000001"},
                "supported_parameters": ["tools", "reasoning"],
                "benchmarks": {"artificial_analysis": {"intelligence_index": 61.5, "agentic_index": 44.0}},
            }
        ]
    }

    models = parse_catalog(raw)

    assert len(models) == 1
    model = models[0]
    assert model.id == "vendor/model"
    assert model.supports("tools") and not model.supports("structured_outputs")
    assert model.intelligence_index == 61.5
    assert model.cache_read_price == 0.0000001


@pytest.mark.asyncio
async def test_catalog_fetch_failure_degrades_to_the_pinned_fallback(tmp_path: Path):
    """A catalog outage must degrade the decision, never break the app."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_catalog(client=client, cache_path=tmp_path / "cache.json")

    assert [m.id for m in models] == [m.id for m in PINNED_FALLBACK]


@pytest.mark.asyncio
async def test_catalog_is_cached_and_reused(tmp_path: Path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": [{"id": "vendor/model", "pricing": {"prompt": "0.000001"}}]})

    cache = tmp_path / "cache.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await fetch_catalog(client=client, cache_path=cache)
        second = await fetch_catalog(client=client, cache_path=cache)

    assert calls["n"] == 1, "the second call must be served from cache"
    assert [m.id for m in first] == [m.id for m in second] == ["vendor/model"]


def test_every_default_lane_is_satisfiable_by_the_pinned_fallback():
    """A floor above the index ceiling matches nothing, silently.

    The `review` lane originally floored intelligence at 70 while the
    published index tops out around 60, so it resolved to nothing against the
    real catalog and would have escalated on every turn. Floors must be
    reachable by the models we ship as the last-resort fallback, or the lane
    is unroutable by construction.
    """
    for lane in DEFAULT_LANES:
        relaxed = lane.model_copy(update={"min_context_tokens": 0})
        result = resolve_lane(relaxed, PINNED_FALLBACK)
        assert result.best is not None, (
            f"lane {lane.name} cannot be satisfied even by the pinned fallback; "
            "its floors are above what any shipped model publishes"
        )
