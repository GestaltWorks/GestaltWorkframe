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
    lane = Lane(name="t", must=["tools"])
    cheap_no_tools = _model("vendor/cheap", prompt=0.01, completion=0.02, params=frozenset())
    pricier_with_tools = _model("vendor/capable", prompt=2.0, completion=8.0)

    result = resolve_lane(lane, [cheap_no_tools, pricier_with_tools])

    assert [c.id for c in result.candidates] == ["vendor/capable"]
    assert any("missing required parameter" in r.reason for r in result.rejections)


def test_free_tier_is_excluded_before_price_is_considered():
    """Zero is a degenerate optimum; `:free` trains on submitted prompts."""
    lane = Lane(name="t", must=["tools"])
    free = _model("vendor/model:free", prompt=0.0, completion=0.0)
    paid = _model("vendor/model", prompt=1.0, completion=4.0)

    result = resolve_lane(lane, [free, paid])

    assert [c.id for c in result.candidates] == ["vendor/model"]
    assert any("free tier excluded" in r.reason for r in result.rejections)


def test_the_free_tier_exclusion_cannot_be_lifted_by_configuration():
    """No lane field, env var or config key may re-admit a `:free` endpoint.

    It is a DATA-HANDLING rule, not a cost preference: free tiers generally
    train on submitted prompts, and no setting makes a training corpus forget.
    A lane used to carry `allow_free_tier: bool = False` with the docstring
    "Opt-in only", and lanes load from a per-deployment lanes.yaml, so that
    field WAS an operator setting.
    """
    assert "allow_free_tier" not in Lane.model_fields

    # Even if a deployment writes the key, it is dropped rather than honoured.
    lane = Lane(name="t", must=["tools"], allow_free_tier=True)  # type: ignore[call-arg]
    result = resolve_lane(lane, [_model("vendor/model:free", prompt=0.0, completion=0.0)])
    assert result.candidates == ()
    assert any("free tier excluded" in r.reason for r in result.rejections)


def test_batch_tier_and_router_pseudo_models_are_excluded():
    """Both win a cost ranking outright, for two different reasons.

    A `:batch` SKU publishes its interactive sibling's indices, so no quality
    floor can catch it: it clears every floor, undercuts the real endpoint by
    about half, and then fails a synchronous turn by arriving hours later.
    A router pseudo-model is not a model at all; it delegates the lane's own
    decision and publishes a sentinel price.
    """
    lane = Lane(name="t", must=["tools"])
    batch = _model("vendor/model:batch", prompt=0.5, completion=2.5)
    pseudo = _model("openrouter/auto", prompt=0.0, completion=0.0)
    sentinel = _model("vendor/negative", prompt=-1.0, completion=-1.0)
    normal = _model("vendor/model", prompt=1.0, completion=5.0)

    result = resolve_lane(lane, [batch, pseudo, sentinel, normal])

    assert [c.id for c in result.candidates] == ["vendor/model"]
    reasons = {r.model_id: r.reason for r in result.rejections}
    assert "batch tier excluded" in reasons["vendor/model:batch"]
    assert "router pseudo-model excluded" in reasons["openrouter/auto"]
    assert "non-positive cost per turn" in reasons["vendor/negative"]


def test_preview_builds_are_excluded_from_user_facing_lanes():
    lane = Lane(name="t", must=["tools"])
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
        name="t", must=["tools"],
        expected_input_tokens=1_000, expected_output_tokens=2_000,
    )
    cheap_prompt_pricey_output = _model("vendor/trap", prompt=0.5, completion=30.0)
    honest = _model("vendor/honest", prompt=1.0, completion=3.0)

    result = resolve_lane(lane, [cheap_prompt_pricey_output, honest])

    assert result.best is not None
    assert result.best.id == "vendor/honest", "ranking on input price alone picks the trap"


def test_cache_reads_are_priced_when_a_prefix_is_replayed():
    lane = Lane(
        name="t", must=["tools"],
        expected_input_tokens=10_000, expected_output_tokens=100,
        expected_cached_input_tokens=9_000,
    )
    cached = _model("vendor/cached", prompt=3.0, completion=15.0, cache_read=0.3)
    uncached = _model("vendor/uncached", prompt=1.0, completion=5.0)

    result = resolve_lane(lane, [cached, uncached])

    assert result.best is not None
    assert result.best.id == "vendor/cached", "a cached premium model can beat an uncached cheap one"


def test_tier_best_orders_by_margin_then_cost():
    lane = Lane(name="t", must=["tools"], min_intelligence=50)
    good = _model("vendor/good", intelligence=60, prompt=0.1)
    better = _model("vendor/better", intelligence=90, prompt=9.0)

    result = resolve_lane(lane, [good, better], tier="best")

    assert [c.id for c in result.candidates][0] == "vendor/better"


# ---- availability and fail-sideways --------------------------------------

def test_benched_models_are_removed_from_the_candidate_set():
    """Availability is observed, not published: a timing-out model is worthless."""
    lane = Lane(name="t", must=["tools"])
    flaky = _model("vendor/flaky", prompt=0.1, completion=0.2)
    steady = _model("vendor/steady", prompt=1.0, completion=4.0)

    result = resolve_lane(lane, [flaky, steady], benched={"vendor/flaky"})

    assert [c.id for c in result.candidates] == ["vendor/steady"]
    assert any("benched" in r.reason for r in result.rejections)


def test_resolution_returns_a_shortlist_so_retries_fail_sideways():
    """A single pinned fallback turns a bad ten minutes into a frontier-priced turn."""
    lane = Lane(name="t", must=["tools"], shortlist_size=3)
    catalog = [_model(f"vendor/m{i}", prompt=float(i + 1)) for i in range(5)]

    result = resolve_lane(lane, catalog)

    assert len(result.candidates) == 3
    assert [c.id for c in result.candidates] == ["vendor/m0", "vendor/m1", "vendor/m2"]


def test_resolution_explains_itself_for_the_operator():
    """Automatic selection without a visible decision is unauditable."""
    lane = Lane(name="guide", must=["tools"])
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


def test_default_lanes_require_tools_and_carry_no_objective():
    """A lane states the requirement. The tier states the objective, per call."""
    for lane in DEFAULT_LANES:
        assert "tools" in lane.must, f"lane {lane.name} must filter on tool support"
    assert "prefer" not in Lane.model_fields, "the objective must not live in the lane record"


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


def _cache_file(path: Path, *, age_seconds: float, model_id: str = "vendor/stale") -> Path:
    import json
    import time

    path.write_text(
        json.dumps(
            {
                "fetched_at": time.time() - age_seconds,
                "catalog": {
                    "data": [
                        {
                            "id": model_id,
                            "context_length": 1_000_000,
                            "pricing": {"prompt": "0.000001", "completion": "0.000005"},
                            "supported_parameters": ["tools", "tool_choice"],
                            "benchmarks": {"artificial_analysis": {"intelligence_index": 50.0}},
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_staleness_is_bounded_by_a_ttl_and_a_separate_hard_max_age(tmp_path: Path):
    """Two bounds, two questions, previously both answered by no check at all.

    The TTL says when a stored catalog stops being PREFERRED. The hard max age
    says when it stops being USABLE. The fetch-failure path used to pass a TTL
    of ten years, which is a shape test wearing an age test's clothes: a shape
    test cannot tell a price from an hour ago from a price from last month, so a
    promotional price that lapsed weeks ago could keep winning a cost-ranked
    lane while the reason string still said "cheapest of N".
    """
    from gestaltworkframe.core.model_catalog import (
        DEFAULT_TTL_SECONDS,
        HARD_MAX_AGE_SECONDS,
        load_cached_catalog_sync,
    )

    assert HARD_MAX_AGE_SECONDS > DEFAULT_TTL_SECONDS

    cache = tmp_path / "catalog.json"

    # Fresh: used.
    _cache_file(cache, age_seconds=60)
    assert [m.id for m in load_cached_catalog_sync(cache_path=cache)] == ["vendor/stale"]

    # Past the TTL, inside the hard cap: still used, and its age is logged.
    _cache_file(cache, age_seconds=DEFAULT_TTL_SECONDS + 3600)
    assert [m.id for m in load_cached_catalog_sync(cache_path=cache)] == ["vendor/stale"]

    # Past the hard cap: refused, and the pinned fallback is used instead.
    _cache_file(cache, age_seconds=HARD_MAX_AGE_SECONDS + 3600)
    assert [m.id for m in load_cached_catalog_sync(cache_path=cache)] == [
        m.id for m in PINNED_FALLBACK
    ]


def test_a_missing_or_unparseable_timestamp_counts_as_infinitely_old(tmp_path: Path):
    """A stale record must not be able to look exactly like a fresh one."""
    import json

    from gestaltworkframe.core.model_catalog import cache_age_seconds, load_cached_catalog_sync

    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps({"catalog": {"data": [{"id": "vendor/x"}]}}), encoding="utf-8")

    assert cache_age_seconds(cache) == float("inf")
    assert [m.id for m in load_cached_catalog_sync(cache_path=cache)] == [
        m.id for m in PINNED_FALLBACK
    ]


def test_the_pinned_fallback_stays_a_small_dated_verbatim_capture(tmp_path: Path):
    """It is a live selector, not inert insurance, so it must not grow.

    A pinned rung is returned whenever the catalog is unreachable and is handed
    out as an escalation target, so a "best models" table here becomes a
    permanent shadow routing policy. Three entries, real published indices,
    dated capture annotation.
    """
    from gestaltworkframe.core import model_catalog

    assert len(PINNED_FALLBACK) == 3
    source = Path(model_catalog.__file__).read_text(encoding="utf-8")
    assert "2026-07-25" in source, "the dated capture annotation must travel with the values"
    for model in PINNED_FALLBACK:
        assert model.intelligence_index is not None
        assert model.agentic_index is not None
        assert model.prompt_price > 0 and model.completion_price > 0


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


# ---- no vendor preference, at all ----------------------------------------

def test_a_lane_cannot_express_a_vendor_preference():
    """The field is gone, not demoted.

    It survived one pass as a bottom-of-the-order tie-break key on a single
    rationale: customer-facing lanes must lead with Anthropic. The operator has
    since decided that OPENROUTER leads, and OpenRouter is a transport rather
    than a vendor to prefer, so a list of vendor name prefixes has nothing left
    to say. A lane that should lead with a stronger model raises its floor and
    resolves at tier `best`.
    """
    assert "prefer_vendors" not in Lane.model_fields
    assert not hasattr(Lane(name="t"), "prefer_vendors")


def test_a_lanes_yaml_still_naming_a_vendor_preference_is_reported_not_obeyed(
    tmp_path: Path, caplog
):
    """Pydantic ignores unknown fields, so silence would look like agreement."""
    bundle = tmp_path / "brand"
    bundle.mkdir()
    (bundle / "lanes.yaml").write_text(
        "lanes:\n"
        "  guide:\n"
        "    must: [tools]\n"
        "    min_intelligence: 45\n"
        "    prefer_vendors: ['anthropic/']\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        lanes = load_lanes(bundle)

    assert not hasattr(lanes["guide"], "prefer_vendors")
    assert lanes["guide"].min_intelligence == 45, "the rest of the record still loads"
    warnings = [record.getMessage() for record in caplog.records]
    assert any("prefer_vendors" in message for message in warnings)
    assert any("tier `best`" in message for message in warnings)


def test_the_answer_to_leading_with_a_stronger_model_is_the_floor_and_the_tier():
    """The doctrine's replacement for a vendor list, pinned as behaviour.

    Same catalog, same tier. Raising the floor to where the capability starts is
    what moves the winner, and it is a reviewable number rather than a name.
    """
    adequate_and_cheap = _model("vendor/adequate", prompt=0.1, completion=0.2, intelligence=52)
    strong_and_dear = _model("anthropic/claude-x", prompt=3.0, completion=15.0, intelligence=60)
    catalog = [adequate_and_cheap, strong_and_dear]

    low_bar = resolve_lane(Lane(name="t", must=["tools"], min_intelligence=45), catalog, tier="auto")
    raised_bar = resolve_lane(Lane(name="t", must=["tools"], min_intelligence=55), catalog, tier="auto")

    assert low_bar.best is not None and low_bar.best.id == "vendor/adequate"
    assert raised_bar.best is not None and raised_bar.best.id == "anthropic/claude-x"
    assert "vendor/adequate" not in {c.id for c in raised_bar.candidates}, (
        "the floor excludes outright; it is not a preference something can outrank"
    )


def test_ordering_is_measured_all_the_way_down_to_the_model_id():
    lane = Lane(name="t", must=["tools"])
    cheap = _model("vendor/cheap", prompt=0.1, completion=0.2)
    pricey = _model("anthropic/claude-x", prompt=9.0, completion=40.0)

    result = resolve_lane(lane, [pricey, cheap], tier="cheap")

    assert [c.id for c in result.candidates][0] == "vendor/cheap"


def test_every_comparator_is_a_total_order_ending_in_the_model_id():
    """Otherwise the CATALOG'S ARRAY POSITION decides the lane.

    Exact ties are structural, not hypothetical: vendors price whole families
    the same on purpose. Without the id as the final key, one upstream
    reordering flips the pick with no code change and no log line.
    """
    lane = Lane(name="t", must=["tools"])
    twins = [
        _model("vendor/zzz", prompt=1.0, completion=5.0),
        _model("vendor/aaa", prompt=1.0, completion=5.0),
    ]

    for tier in ("best", "auto", "fast", "cheap"):
        forward = resolve_lane(lane, twins, tier=tier)
        backward = resolve_lane(lane, list(reversed(twins)), tier=tier)
        assert [c.id for c in forward.candidates] == [c.id for c in backward.candidates] == [
            "vendor/aaa",
            "vendor/zzz",
        ], tier


# ---- the tier axis -------------------------------------------------------

def test_the_same_lane_resolves_differently_at_each_tier():
    """Lane and tier are orthogonal: one requirement, four objectives."""
    lane = Lane(
        name="t",
        must=["tools"],
        min_intelligence=40,
        expected_input_tokens=1_000,
        expected_output_tokens=1_000,
    )
    # Cheapest thing that clears the bar, by a hair.
    squeaker = _model("vendor/squeaker", prompt=0.01, completion=0.02, intelligence=40.3)
    # Solid mid-range.
    middle = _model("vendor/middle", prompt=1.0, completion=5.0, intelligence=55)
    # Top of the catalog, priced like it.
    frontier = _model("vendor/frontier", prompt=5.0, completion=25.0, intelligence=75)
    catalog = [squeaker, middle, frontier]

    best = resolve_lane(lane, catalog, tier="best")
    cheap = resolve_lane(lane, catalog, tier="cheap")
    auto = resolve_lane(lane, catalog, tier="auto")

    assert best.best is not None and best.best.id == "vendor/frontier"
    assert cheap.best is not None and cheap.best.id == "vendor/squeaker"
    # The gate is the point: an ungated margin-per-dollar ratio would take the
    # squeaker, which is "barely adequate and nearly free" -- the degenerate
    # optimum. It must not survive the gate at all.
    assert auto.best is not None and auto.best.id != "vendor/squeaker"
    assert any(
        r.model_id == "vendor/squeaker" and "share of best available margin" in r.reason
        for r in auto.rejections
    )


def test_auto_takes_margin_per_dollar_among_what_survives_the_gate():
    lane = Lane(
        name="t",
        must=["tools"],
        min_intelligence=40,
        expected_input_tokens=1_000,
        expected_output_tokens=1_000,
    )
    # Both well clear of the gate; the cheaper one wins on value per dollar.
    value = _model("vendor/value", prompt=1.0, completion=5.0, intelligence=70)
    frontier = _model("vendor/frontier", prompt=5.0, completion=25.0, intelligence=75)

    auto = resolve_lane(lane, [value, frontier], tier="auto")
    best = resolve_lane(lane, [value, frontier], tier="best")

    assert auto.best is not None and auto.best.id == "vendor/value"
    assert best.best is not None and best.best.id == "vendor/frontier"


def test_fast_never_pays_a_lot_to_save_a_little_time():
    """Two measured models: one 3x faster and 20x dearer. `fast` takes the slower."""
    lane = Lane(
        name="t",
        must=["tools"],
        min_intelligence=40,
        expected_input_tokens=1_000,
        expected_output_tokens=1_000,
    )
    quick_and_dear = _model("vendor/quick", prompt=20.0, completion=100.0, intelligence=60)
    slow_and_cheap = _model("vendor/slow", prompt=1.0, completion=5.0, intelligence=60)

    result = resolve_lane(
        lane,
        [quick_and_dear, slow_and_cheap],
        tier="fast",
        observed_seconds={"vendor/quick": 2.0, "vendor/slow": 6.0},
    )

    assert result.best is not None and result.best.id == "vendor/slow"


def test_an_unmeasured_model_sorts_behind_a_measured_one_but_is_never_excluded():
    """A model that is never picked can never earn its first measurement."""
    lane = Lane(name="t", must=["tools"], min_intelligence=40)
    measured = _model("vendor/measured", prompt=9.0, completion=40.0, intelligence=60)
    unmeasured = _model("vendor/unmeasured", prompt=0.1, completion=0.2, intelligence=60)

    result = resolve_lane(
        lane, [unmeasured, measured], tier="fast", observed_seconds={"vendor/measured": 1.0}
    )

    assert [c.id for c in result.candidates] == ["vendor/measured", "vendor/unmeasured"]


def test_fast_says_so_when_it_has_no_latency_to_rank_on():
    """Never imply a measurement that does not exist."""
    lane = Lane(name="t", must=["tools"], min_intelligence=40)
    result = resolve_lane(lane, [_model("vendor/a", intelligence=60)], tier="fast")
    assert "no latency observed" in result.explain()


def test_cheap_is_never_a_hidden_default():
    """`auto` is the default. `cheap` is a claim somebody owns."""
    from gestaltworkframe.core.model_resolver import DEFAULT_TIER

    assert DEFAULT_TIER == "auto"
    lane = Lane(name="t", must=["tools"], min_intelligence=40)
    assert resolve_lane(lane, [_model("vendor/a", intelligence=60)]).tier == "auto"


# ---- margin --------------------------------------------------------------

def test_margin_is_headroom_over_the_bar_not_the_mean_of_raw_indices():
    """The two metrics disagree, and doctrine says which one wins.

    Capability below the bar is worth nothing, because the model is excluded
    outright; only the surplus above it can help this turn. Here the two
    metrics order the same catalog differently:

        lane floors: intelligence 50, agentic 40
        alpha: 90 / 42 -> mean 66.0, headroom 40 + 2 = 42
        beta:  55 / 75 -> mean 65.0, headroom  5 + 35 = 40
        gamma: 60 / 70 -> mean 65.0, headroom 10 + 30 = 40

    Mean-of-indices puts alpha first too, but the OLD code averaged only the
    floored axes and would have made beta and gamma tie with alpha's 66 close
    behind; the point of the assertion is that the winner's reported number is
    the headroom (42), not any average of raw indices.
    """
    lane = Lane(name="t", must=["tools"], min_intelligence=50, min_agentic=40)
    # mean(90, 42) = 66.0 ; headroom = 40 + 2 = 42
    alpha = _model("vendor/alpha", intelligence=90, agentic=42, coding=None)
    # mean(55, 75) = 65.0 ; headroom = 5 + 35 = 40
    beta = _model("vendor/beta", intelligence=55, agentic=75, coding=None)
    # mean(60, 70) = 65.0 ; headroom = 10 + 30 = 40  -- and cheaper than alpha
    gamma = _model("vendor/gamma", intelligence=60, agentic=70, coding=None)

    assert (90 + 42) / 2 > (55 + 75) / 2, "alpha has the higher mean"
    result = resolve_lane(lane, [beta, gamma, alpha], tier="best")

    assert result.best is not None and result.best.id == "vendor/alpha"
    assert result.best.margin == 42.0


def test_margin_ignores_an_axis_the_lane_declared_no_floor_on():
    """Averaging or summing an undeclared axis dilutes a genuine surplus."""
    lane = Lane(name="t", must=["tools"], min_intelligence=50)
    # Identical intelligence headroom; wildly different coding index.
    plain = _model("vendor/plain", intelligence=60, coding=10, agentic=10)
    coder = _model("vendor/coder", intelligence=60, coding=95, agentic=95)

    result = resolve_lane(lane, [plain, coder], tier="best")

    assert {c.margin for c in result.candidates} == {10.0}


# ---- the decision log ----------------------------------------------------

def test_the_reason_string_names_the_tier_and_objective_actually_used():
    """A wrong route gets caught by a bad answer; a wrong REPORT does not."""
    lane = Lane(name="guide", must=["tools"], min_intelligence=40)
    catalog = [
        _model("vendor/cheap", prompt=0.1, completion=0.5, intelligence=45),
        _model("vendor/strong", prompt=5.0, completion=25.0, intelligence=75),
    ]

    best = resolve_lane(lane, catalog, tier="best").explain()
    cheap = resolve_lane(lane, catalog, tier="cheap").explain()

    assert "tier=best" in best and "maximum margin" in best
    assert "tier=cheap" in cheap and "lowest cost above the bar" in cheap
    assert best != cheap
    assert "vendor/strong" in best and "vendor/cheap" in cheap
