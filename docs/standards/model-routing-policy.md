<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->

# Model Routing Policy

Canonical local-first / best-value routing doctrine for any repo in this shop.
This is the source of truth; repos vendor a copy as `docs/standards/model-routing-policy.md`.

## Principles

1. Never depend entirely on a single provider. Use an aggregator (e.g.
   OpenRouter) as the primary access path; local GPU and direct-SDK providers are
   optional bolt-ons, disabled by default until hardware/keys are available.
2. Match the specific model to the specific task. Capability need, not habit,
   drives selection. Express the need; never hardcode the model name (see
   "Capability-based selection").
3. Treat intelligence and compute as operating expense with unit economics.
   LOCAL routes have zero marginal cost; metered/premium cost is incurred only
   when task fit justifies it. "Free" here means local inference on your own
   hardware. It never means an aggregator `:free` tier, which is excluded
   outright as a data-handling rule (see "Hard filters"): those routes are not
   cheap compute, they are compute paid for with your prompts.
4. The application owns policy, credentials, routing, memory, and final
   acceptance. Models are workers, not decision-makers.
5. Tokens-per-call is a cost lever independent of tier. Shrink the payload
   before the call; this compounds with routing instead of competing with it.

## Default routing order (best value)

1. Deterministic tools first when they can verify or compute the answer.
2. LOCAL models for routine execution: summarizing, drafting routine code,
   refactors with clear tests, markdown/templates, boilerplate, first-pass
   investigation. Local, not an aggregator `:free` tier: see Principle 3.
3. Metered (low-cost) cloud only when local is inadequate.
4. Premium cloud reserved for: architecture with long-term cost, security
   review, hard debugging after local attempts fail, release readiness review,
   and code where mistakes cause data loss, auth/payment bugs, or outages.

That list says which CLASS of compute to reach for. It is not an objective, and
it must never be read as "take the cheapest thing that technically passes". Which
model runs inside the chosen class is decided by the two axes below: the LANE
states the requirement, the TIER states the objective. "Best value" is the name
of one tier (`auto`, margin per dollar), not a standing lean toward the bottom of
the qualifying set.

## Capability-based selection (never hardcode model IDs)

A model name in application code is a stale constant with a short shelf life.
It encodes a judgment ("this is the best one for X") that stops being true the
week a better or cheaper model ships, and it can only be corrected by a code
change and a redeploy. Hardcoding is the "habit" Principle 2 forbids.

**Declare what the task requires; resolve the model at runtime. Then say
separately what you are optimising for.**

### Two orthogonal axes: the lane and the tier

A task lane is a policy record, not a name, and it carries NO objective:

```text
guide:  must:[tools], minContext:200k, minAgentic:45, minIntelligence:55,
        shape:{in:40k, out:12k, cached:0}
lookup: must:[tools], minIntelligence:30, shape:{in:15k, out:600}
```

The tier is passed PER CALL and is never written into the lane record:

```text
resolveLane(GUIDE_LANE, "best")
resolveLane(LOOKUP_LANE, "cheap")
```

The lane answers "what does this turn require, and what shape is it". Hard
requirements are objective and machine-checkable, never a matter of taste. The
tier answers "among the models that already qualify, what should win". Both are
needed and neither can stand in for the other.

Collapsing them onto one axis is a real defect with measured consequences, not a
tidiness argument. Three of them, all observed:

- **The objective ends up selecting the requirement.** co-dm mapped the
  composer's effort control onto lanes (`{fast: "lookup", balanced: "chat",
  best: "guide"}`). Asking for "best" on a one-line canon question priced and
  filtered it as a full session guide, against a 200k context floor and a 12k
  output shape. Asking for "fast" on a session guide routed it through the lookup
  lane, which has no truncation guard, a 600-token output shape and a $1/M prompt
  cap, so a model that stops at 4k could be handed a 12k-token document to write.
- **There is no way to ask for the same lane at a different objective.** When the
  objective lives inside the lane record, an escalation path has nowhere to put
  one. The kit's `resolveLadder` had to forcibly overwrite `prefer` on the
  escalated rung, which silently demoted a `best` lane to cheapest-above-the-new-
  floor at exactly the moment it was escalating. With the objective on its own
  axis the escalated rung inherits the caller's tier and raising the floor does
  the escalating.
- **A cost preference inside an eligibility record reads as a property of the
  task.** It is not; it is a choice somebody made. `prefer: "cost"` on a lane
  reads as thrifty in a diff and is invisible on review, and the lane's own
  comments then argue with it forever.

The corollary: do not smuggle the objective back in through the requirement
record. A `maxCostPerTurnUSD` ceiling on a lane is a tier decision hiding in an
eligibility record, and it constrains EVERY objective including `best`. The
kit's writing lane is the case study, and its own comment was the confession.
The lane began at tier quality and resolved to claude-opus-5. Someone changed it
to prefer cost. The drafts got worse, and the response was to fight the objective
by editing eligibility: ratchet the intelligence floor from 50 to 58, then raise
the cost ceiling from 5c to 25c because at 5c the new floor excluded everything
and the lane lived on fallbacks. Two knobs bent out of shape to compensate for
one wrong objective. If you want a lane cheap, resolve it at tier `cheap`.

### Margin: the quality currency every tier ranks on

```text
margin = max(0, intelligence - minIntelligence) + max(0, agentic - minAgentic)
```

Headroom over the bar, per axis, summed.

- **Measured against the BAR, not in absolute index points.** The floor encodes
  what the turn actually requires. Capability below it is worth nothing, because
  the model is excluded outright; only the surplus above it can help this turn.
  Five extra points of reasoning on a lane that needed 45 is worth having; the
  same five points below the bar buys nothing at all.
- **Per-axis and summed, never a max or an average.** The candidate filter
  enforces BOTH gates, so both surpluses are real and additive. Taking the max
  would let a lead in reasoning pay for a model that barely scrapes tool
  adherence; averaging would dilute a genuine surplus with an axis the lane did
  not care about.
- **An undeclared floor counts as 0.** No bar means the whole of that axis is
  headroom.
- **Margins are only ever compared WITHIN one lane resolution**, so the absolute
  scale never matters and there is nothing to calibrate across lanes.

### The four tiers

Every tier applies the same hard filters and the same floors first. They differ
only in how they order what survives.

- **`best` = maximum margin; cost only breaks ties.**
  The cost ceiling is EMERGENT, which is why no dollar constant appears anywhere
  in the implementation. The requirement was "do not exceed what the current
  Opus-class model costs, but never name it". Max-margin-cheapest-on-ties reduces
  to exactly that: the cap is set by the top-margin model's price, and the only
  thing permitted to exceed it is something with MORE margin, which would then BE
  the top-margin model. Self-updating, no name, no number. Never write a dollar
  constant to approximate this.
- **`auto` = maximum margin per dollar, above the margin-share gate. This is the
  default.** The gate runs first, the ratio ranks only what survives it. See
  below for why the gate is not optional.
- **`fast` = minimises seconds x cost, above the same gate.**
  Never seconds alone. Ranking on seconds alone pays any amount to shave a second
  off a turn; ranking on cost alone is just `auto` with a lower bar. The product
  rule is "do not spend a lot to save a little time, and do not waste a lot of
  time to save a little money". Encode it as a test: two models both measured,
  one 3x faster and 20x dearer, and `fast` must take the slower one.
  Speed is OBSERVED, never looked up. `latency_last_30m` and
  `throughput_last_30m` are published as null on every endpoint of every model
  checked (16 of 16), and the bulk catalog has no speed field at all, so ranking
  on them would be ranking on nothing. Until a probe exists, `fast` orders by
  cost with margin breaking ties, and it says so in its reason string rather than
  implying a measurement it does not have. An unmeasured model sorts BEHIND every
  measured one and is never EXCLUDED, because a model that is never picked can
  never earn its first measurement.
- **`cheap` = lowest cost above the bar, margin breaking ties.**

**`cheap` is a named declaration, not a default.** It is the same arithmetic the
old hidden `prefer: "cost"` performed, and the difference is entirely epistemic.
As a hidden default it made the FLOOR THE TARGET on every lane, so the system
systematically ran the worst model that technically passed, including on the
lanes whose own comments said the output IS the product. As a declared tier it is
a claim somebody owns: this work is commodity.

It is honest only when the LANE'S BAR IS SET AT THE LOWEST REASONABLE PLACE to
begin with. `cheap` sitting on a craft-calibrated floor is the worst of both
worlds: you pay a quality premium in the floor and then refuse to spend anything
above it. The kit's task-classification lane is the honest case. What that turn
genuinely requires is enough context for a task brief plus the capability list,
and enough reasoning to emit schema-valid JSON without wandering. It does not
require tool-calling, long context, or strong prose, so the floors say exactly
that and nothing more. If classifications start coming back malformed, raise
`minIntelligence` on the evidence and record why; do not quietly change tier.

And `cheap` still means cheapest among the CAPABLE. Every hard filter above still
applies, because a cheap model that cannot call tools is not cheap, it is broken.

### The margin-share gate, and why a pure ratio needs one

A pure margin-per-dollar ratio has a degenerate optimum: "barely adequate and
nearly free" wins by construction. Measured on the live catalog, a model clearing
the bar by 0.3 points at $0.0090/turn scores 33.3 margin-points per dollar and
beats claude-opus-5's 29.4. That is not what best value means.

So a candidate must reach a declared SHARE of the best available margin before
its ratio is allowed to count. The gate runs first; the ratio ranks the
survivors. This is the SECOND buffer: the floor is the hard requirement, and the
share is the "and not by a hair" clause.

The share is a DATED CALIBRATION against a specific catalog, not a law. It has
to be re-derived per repo and re-checked when the catalog moves.

- co-dm runs **0.55**. At 0.55, three of four candidates stayed eligible and the
  squeaker was gone; at 0.75 it over-pruned to a single candidate and the ratio
  did no work at all.
- The kit runs **0.35**, calibrated 2026-08-01 against this factory's own catalog
  and lane shapes rather than inherited. The sweep, at shares 0.00 / 0.35 / 0.55:

  ```text
  agentic-web-research     luna $0.027 | luna $0.027   | terra $0.268
  deep-contact-enrichment  luna $0.020 | terra $0.198  | kimi-k3 $0.570
  wide-shallow-discovery   luna $0.010 | terra $0.104  | kimi-k3 $0.300
  interactive-tool-chat    luna $0.004 | luna $0.004   | terra $0.039
  voiced-drafting          opus-5      | opus-5        | opus-5   (stable)
  ```

  0.35 lifts enrichment and discovery off the floor, which are the two lanes
  where research depth compounds into every draft written afterwards, and leaves
  sourcing and chat on the cheap-and-competent option. 0.55 additionally pulls in
  a model at 3x the cost, which index arithmetic alone does not justify.

If a repo's numbers differ from both, that is expected. Write down the sweep that
chose them, with the date, next to the constant.

### Resolution order

1. Fetch the catalog (cached, with an age attached).
2. Drop anything failing a hard requirement, a data-handling rule, a delivery
   contract, an expiry, or the availability bench. Before price, always.
3. Apply the quality floors.
4. Compute cost per turn at the lane's declared shape; drop anything that cannot
   be costed or that exceeds a declared ceiling.
5. Apply the tier's gate.
6. Rank by the tier's comparator, to a TOTAL order.
7. Take the top. Log the decision.

Nothing after step 2 may re-admit anything step 2 removed.

### Hard filters, costing, and degradation

- **Catalog.** OpenRouter's `GET /api/v1/models` is public (no auth) and returns,
  per model: `pricing` (prompt, completion, `input_cache_read`, and
  `pricing.overrides`, which is a long-context SURCHARGE ladder and not a
  discount), `supported_parameters` (`tools`, `tool_choice`,
  `structured_outputs`, `reasoning`, ...), `context_length`,
  `top_provider.max_completion_tokens`, `expiration_date`, and
  `benchmarks.artificial_analysis` (`intelligence_index`, `coding_index`,
  `agentic_index`).
- **Tool-calling is a hard filter, not a preference.** Any agentic loop must drop
  every model whose `supported_parameters` lacks `tools`. A cheap model that
  cannot call tools is not cheap; it is broken.
- **Quality floors use published indices**, not vibes. `agentic_index` is the
  proxy for tool adherence; `intelligence_index` for reasoning. A model with no
  published benchmarks cannot clear a floor: unmeasured is not the same as good.
  Floors are reviewable numbers, so a lane's standard is legible and arguable,
  and they move on evidence. Both of the kit's research lanes sit at agentic 45
  because models clearing 35 burned ENTIRE timed runs by dying on the clock
  (glm-5.2, discovery dead at the 3-minute rung, 2026-07-27; the same class of
  failure as an earlier 234s double-timeout on enrichment). A run that times out
  costs the full budget and produces zero output, which is the most expensive
  outcome there is.
- **Exclude `:free` BEFORE cost ranking. This is a DATA-HANDLING exclusion and no
  operator setting may lift it.** Free tiers are generally trained on submitted
  prompts. A cost ranking would otherwise always select them, because zero is a
  degenerate optimum. No setting makes a training corpus forget.
- **Exclude `:batch` BEFORE cost ranking, for a separate reason: arrival time,
  not price, is the defect.** A batch SKU is the SAME model at IDENTICAL
  published indices for about half the price, delivered asynchronously over a
  completion window measured in hours. No quality floor can catch it, because the
  indices are its interactive sibling's. Measured on the live catalog
  (2026-07-30): 367 models, 28 of them `:batch`, 19 of those carrying full
  Artificial Analysis indices, i.e. 19 models that pass every floor and undercut
  the real endpoint by 50%. The kit's writing lane had `anthropic/claude-opus-5`
  and `anthropic/claude-fable-5:batch` tied at EXACTLY $0.0645/turn
  (`fable-5:batch` is priced 5e-6/25e-6, opus-5's rates, while interactive
  fable-5 is 10e-6/50e-6). This is not theoretical: `data/model-health.json`
  carried a bench record for `openrouter/anthropic/claude-opus-5:batch` with the
  reason "no usable output (exit 1)", a batch id that a lane resolved and a
  worker dispatched, failing the way an async endpoint fails a synchronous
  headless call. Like `:free`, no override may lift it.
- **Exclude router pseudo-models, and reject non-positive costs.**
  `openrouter/auto`, `openrouter/auto-beta` and `openrouter/free` are not
  selectable models at all; they are meta-routers that delegate the decision away
  from the lane, and they publish SENTINEL prices (0 and -1 per token) that win
  any cost ranking outright. A negative price is a degenerate optimum in exactly
  the way zero is. Match the bare slug forms as well as the colon-suffixed ones,
  and independently reject any computed turn cost that is not greater than zero.
  This closed a live split-brain in the kit: a classifier chose its own model
  from string constants defaulting to `openrouter/auto` and `openrouter/auto:free`
  and dispatched them directly, so one application carried two selection
  mechanisms and each was doing something the other forbade.
- **Preview / experimental / alpha builds are excluded from anything user-facing**
  so a live session does not break because a provider rotated a preview endpoint.
  This one IS a stability opinion, so it is the ONLY one of the three an operator
  override may lift. Test the unliftable denials BEFORE the override
  short-circuit: a substring allowance naming a model otherwise re-admits that
  model's own `:batch` twin, which is an override of something nobody asked for.
- **Rank on COST PER TURN, never a sticker rate.** Two things are routinely left
  out and both flip picks:
  - *Output.* Completion is usually priced 3-5x input, so a model that is cheap
    to prompt can be expensive to answer. Rank on
    `input x expected_in + completion x expected_out`, with the expected shape
    declared per lane, because a lookup and a long-form build are not the same
    turn.
  - *Cache reads.* When a large stable prefix is replayed each turn (system
    prompt, tool schemas, style corpus, retrieved context), price it at
    `input_cache_read`. A "cheap" model without cache pricing can lose to a
    cached premium one. Where a model publishes no cache price, charge the
    replayed prefix at the FULL input rate rather than pretending it is free.

  Both are estimates; declare them explicitly as tunable lane parameters rather
  than burying them in a comparison. Ranking on input price alone is the common
  bug: it looks right and quietly picks the wrong model.

  Compose the value number locally rather than importing one. Exactly one
  published cost-adjusted composite turned up anywhere: LiveBench's
  `cost_per_successful_task`, which is cost divided by success rate. Its cost
  CSVs exist only for the newest release snapshot with prior dates gone, so it is
  a frozen sanity check and not a feed. Artificial Analysis publishes the total
  dollars its own eval burned per model, which is worth reading once as a reality
  check on a lane's declared shape, but it sits behind a keyed free tier and is a
  calibration input rather than a per-turn dependency. And a pricing metadata
  file is not a capability source: LiteLLM's
  `model_prices_and_context_window.json` covers roughly 2,974 entries with
  pricing and context limits and zero quality signal of any kind, so it can price
  a route and can never rank one. Margin per dollar is the local composite, and
  the arithmetic above is all of it.
- **Honour the published pricing ladder, which RAISES rates above a token
  threshold.** Despite sitting under `pricing`, `pricing.overrides` is not a
  discount and never was: above `min_prompt_tokens` the per-token rates go UP,
  typically doubling. 49 of 367 live models carry one (verified 2026-07-30):
  `x-ai/grok-4.5` doubles from 2e-6/6e-6 to 4e-6/12e-6 above 200k prompt tokens,
  and `qwen/qwen3.7-flash` has two rungs, at 32k and 256k. Resolve the rates at
  the lane's declared prompt size, keyed on the TOTAL expected input including
  the cached prefix, because the threshold applies to the whole prompt the
  provider receives and a replayed prefix still arrives as prompt tokens. This is
  not cosmetic: the kit's sourcing lane declares 220k expected input, above
  grok-4.5's threshold, and 27 of the 49 have a rung at or below 220k, so before
  the ladder was read that lane mispriced qualifying candidates by exactly 2x.
  A cost-ranked lane IS a price comparison, so a 2x mispricing is a wrong
  decision and not merely a wrong number. Version the cache key when this shape
  changes: an older cache parsed against a newer shape leaves the rungs undefined
  and mis-costs silently for as long as the cache lives.
- **Bound catalog staleness with BOTH a TTL and a separate hard max age.** They
  answer different questions and were being answered by the same absent check.
  The TTL (24h) is when a stored catalog stops being PREFERRED over a live fetch.
  The hard max age (72h, i.e. a Friday-evening outage nobody looks at until
  Monday) is when it stops being USABLE AT ALL. The fetch-failure path originally
  applied a shape test and no age test whatever, and a shape test cannot tell a
  price from an hour ago from a price from last month, so a promotional price
  that lapsed weeks ago could keep winning a cost-ranked lane run after run while
  the reason string still said "cheapest of N". Cheapest as of WHEN was the
  missing word. Between the two bounds, use the file and log its actual age at
  warn. Past the hard cap, refuse it and degrade visibly. Judge the age by the
  catalog's OLDEST record, not by element zero: a partial write, a resumed run or
  a merge of a fresh fetch into a retained tail all break the assumption that one
  timestamp speaks for the file, and a stale record looks exactly like a fresh
  one. A missing or unparseable stamp counts as infinitely old.
- **Availability is the third axis, and it must be OBSERVED.** Price and quality
  are necessary and not sufficient: a model that benchmarks well and prices well
  is worthless while it is timing out or rate-limiting you. Do not trust a
  provider's published uptime, which tracks whether the endpoint answers at all
  rather than whether YOUR requests succeed, so rate limits, 429s, context
  rejections and tool-format quirks all still read as "up" (seen in practice: a
  model reported as failing showed `uptime_last_5m = 100`). Keep your own error
  counter per model, bench a repeat offender for a backing-off cooldown, and
  clear it on the first success. Check the bench against the DISPATCH id, which
  is the only id anything records against. The kit compared a catalog id
  (`anthropic/claude-opus-5`) while every writer stored the broker id
  (`openrouter/anthropic/claude-opus-5`), so the two namespaces could never match
  and the filter had never excluded anything: a permanent no-op guarding the
  exact failure it existed to stop.
- **Fail sideways, not upward.** When the chosen model errors, retry on the next
  candidate that already cleared the same lane floors before escalating. A single
  pinned fallback turns a cheap provider's bad ten minutes into a frontier-priced
  turn, which is the opposite of the intent.
- **Escalate on CAPABILITY, and keep the caller's tier.** Raise the floor on the
  axis that failed (a model that lost a long tool instruction needs measurably
  better tool adherence) and re-resolve against the higher bar. The escalated
  rung inherits the tier it was called with. Forcing an objective here is how a
  `best` lane silently becomes cheapest-above-the-new-floor.
- **Never swap models mid-answer.** Retry only while nothing has been streamed to
  the user; splicing two models' prose into one reply is worse than a clean
  failure.
- **Degrade to a pinned fallback rather than break, and hold the fallback to the
  lane's own standard.** A catalog fetch failure must not take the app down. But
  a pinned rung is a live selector, not inert insurance: it is returned whenever
  the catalog is unreachable or nothing clears the floors, and it is handed out
  as an escalation target. So it must be a DATED VERBATIM CAPTURE of catalog
  values, deliberately small, and it must NOT be a "best models" table or grow
  into one. Two failure modes, both observed. co-dm's earlier version carried
  hand-invented indices, claiming haiku-4.5 was 35 intelligence / 25 agentic
  against the catalog's real 29.6 / 16.4, an inflation that would admit haiku to
  lanes whose floors exclude it: the degrade path quietly becoming an upgrade
  path. The kit's current fallbacks VIOLATE THEIR OWN LANES' FLOORS: two lanes
  declare `minAgentic: 45` and fall back to models measured at 40.8 and 16.4; the
  writing lane declares `minIntelligence: 58` and falls back to 47.2; and
  `anthropic/claude-opus-4.6` publishes no indices at all, so it can never clear
  any floor by measurement, yet it is handed out as an ESCALATION target. A
  pinned rung that cannot pass its own lane is a downgrade with a reassuring
  name. Fix it by re-deriving the rungs from a dated capture with the real
  numbers written down, or by deleting the mechanism and letting a catalog outage
  fail loudly. Never by widening the age cap.
- **Transport mapping is config, not judgment.** A table mapping a catalog slug
  to the gateway's alias (which name the broker routes direct versus via the
  aggregator) is legitimate configuration. A table asserting which model is BEST
  is not. Do not ship an alias table that nothing reads: the kit carried one
  describing direct routing that `toBrokerId()` never performed, so a constant
  documented behaviour that did not exist.
- **Log the decision, and never let the reason string misstate the objective
  actually used.** Surface which model ran, which tier ran, how many models
  cleared the lane, how many survived the gate, the chosen margin against the
  best available margin, and the estimated cost per turn, so "why did this cost
  that" and "where did this data go" both have answers. Expose which models are
  currently benched. The kit's shared path emitted "cheapest of N"
  unconditionally whatever the preference was, which is the worse of the two
  failure modes: a wrong route gets caught by a bad answer, while a wrong REPORT
  just makes a human confidently mistaken.
- **Every comparator must be a TOTAL order ending in the model id.** Otherwise an
  exact tie falls through to the sort's stability, which means the CATALOG'S
  ARRAY POSITION decides the lane, and one upstream reordering flips the pick
  with no code change and no log line. Whatever order a provider happens to
  return its models in is not a routing policy. This is measured, not
  hypothetical: on the kit's writing-lane shape `anthropic/claude-opus-5` and
  `anthropic/claude-fable-5:batch` cost exactly $0.0645/turn (2026-07-30) and
  opus-5 won only because it sits earlier in the array, at index 2 against index
  43; co-dm saw the same thing at $0.3740/turn on its guide lane. Ties between a
  vendor's models are structural, because vendors price whole families the same
  on purpose.

### What was built, run, and deliberately deleted

Each of the following was implemented, verified against the live API, and then
removed. They are recorded here so nobody rebuilds them from a stale reading of
this document. None of them is a missing feature.

- **The sale-challenger apparatus, in full.** Per-axis index gap bands,
  discount-magnitude thresholds, consecutive-observation confirmation counters,
  endpoint uptime floors, reduced-precision vetoes, seated-challenger
  bookkeeping, and the 12h sale TTL that sat under the 24h catalog TTL. Roughly
  400 lines, deleted after verifying the premise was false: **the flat catalog
  price is ALREADY post-discount**. Verified from the live payload, a model
  reports a prompt price of 1.25e-6 in the bulk `/api/v1/models` response, which
  is its 50%-off endpoint price, while a second provider serves the same model at
  2.5e-6, and 1.25e-6 / (1 - 0.5) = 2.5e-6 exactly. So the machinery was
  computing a discount on an already-discounted number. A sale is simply a
  smaller value in `cost`, the tier arithmetic sees it for free, and when the sale
  ends the number goes back up on the next catalog refresh with no state to
  unwind. NEVER multiply a catalog price by (1 - discount): that halves an
  already-halved number. The implied list price, if you ever need it, is
  `price / (1 - discount)`. Do not add sale detection back; there is nothing to
  detect.
- **"Bill-safe" costing at the MAXIMUM eligible endpoint.** Reverted as
  "pessimism, not accuracy". It raised claude-opus-5 on the guide lane from
  $0.3740 to $0.4114, a flat 10%, on the strength of ONE Google endpoint at
  5.5e-6 against the usual 5e-6, and it changed ZERO picks on any lane. The
  reason it is wrong is load-bearing and must be stated: **OpenRouter
  load-balances weighted by the INVERSE SQUARE of price, so the dearest endpoint
  attracts the LEAST traffic.** It is a tail, not an expectation. The flat catalog
  price is the primary provider's, which is the modal outcome and therefore the
  best available point estimate. Pricing at the cheapest endpoint is the opposite
  error: ranking on a number you cannot guarantee.
- **Per-endpoint / cheapest-provider selection, and any cheapest-provider
  toggle.** Provider pinning was VERIFIED to work end to end through the LiteLLM
  broker (pin to a deliberately bogus provider and OpenRouter's own 404 comes
  back, "No allowed providers are available", listing the real ones), so this is
  not a capability gap. It was deleted because the capability buys nothing worth
  its complexity. On the craft lanes, where the money is, there is nothing to
  win: claude-opus-5 resolves them and seven of its endpoints price identically
  at 5e-6 with one outlier at 5.5e-6. Where a real gap exists it is on cheap
  lanes, and it is a fraction of a cent: z-ai/glm-5.2 is 9.66e-7 flat against
  3.6e-7 at its cheapest endpoint, a 2.7x difference on lanes running $0.0009 to
  $0.0022 per turn. The cost was per-endpoint probing on the turn path, a
  quantization veto, an uptime floor, and giving up multi-provider failover in
  order to hold a pin. The toggle was worse than the feature: it was a second
  cost dial competing with the tier the caller had already chosen, which is
  two controls for one decision.
- **"Resolve to a shortlist, then order it by a stored human preference."** As a
  selector this is genuinely rotten: scan a stored name order against the
  qualifying set and return the first hit, and a better model can ship and never
  be chosen while a listed model that has gone bad still wins. It is a hardwire
  with a config file in front of it. Deleted.
- **"Prefer the cheapest model that meets capability need" as the default
  objective.** This is the big one. It makes the FLOOR THE TARGET, so you
  systematically get the worst model that technically passes, on every lane,
  including the ones whose output is the product. The floor exists to say
  "anything below this is unusable", not "this is what we are aiming for". The
  replacement is the tier axis: `auto` by default, and `cheap` when somebody
  declares that the work is commodity.

### What capability selection cannot do

Taste is not measured by anything the router already reads. Nothing in the
catalog tells you which model writes the house voice, holds a narrative register,
or keeps a brand's cadence, and that is a measured claim rather than a hunch: the
aggregator publishes exactly two benchmark namespaces and neither carries a prose
signal. `artificial_analysis` is `{intelligence_index, coding_index,
agentic_index}`. `design_arena` spans two dozen-odd categories that are all UI,
frontend, slides or raster image: website, fullstack, dataviz, gamedev,
uicomponent, 3d, svg, asciiart, logo, graphicdesign, image, plus the slide and
app families. A keyword scan of the whole benchmarks subtree returns zero hits
for prose, creative, writing, roleplay, narrative, story or poetry. Adopting a
design_arena Elo as a prose signal would be measuring frontend taste and calling
it voice.

design_arena is unusable as a filter for a second and independent reason: its
coverage lags releases by roughly a generation. A current frontier generation
publishes an empty array, and some variants carry no `benchmarks` key at all. If
it is ever used it can only be an ordering hint inside a candidate set that has
already cleared the floors, never a filter, because a filter would exclude every
model too new to have been rated.

Prose IS measured elsewhere, and the honest version of this section says so
rather than claiming a law of nature. Two live sources. A creative-writing
benchmark publishes machine-readable results with no auth, carrying an Elo, a
creative-writing score, vocabulary complexity, repetition, and a `slop_score`,
which is the closest published proxy to the house-voice concern and is worth
knowing exists, but it carries no price field anywhere, so it can rank quality
and cannot rank value. The public model arena publishes a creative-writing
category alongside coding, math, instruction-following, steerability and
multi-turn, and its payload carries Elo with confidence bounds AND per-million
input and output prices for several hundred models, but there is no public JSON
API, only an HTML/RSC payload to scrape, so there is no contract to depend on.

So this is a trade the operator can revisit, not an impossibility. Adopting
either source means taking on an external dependency with no stability guarantee
in order to decide a question that arises only when the measured axes are already
equal. That is a poor bargain today. It stops being one the day such a source
publishes a stable API, or the day the ties stop being rare.

For lanes judged on craft rather than correctness:

- **Set the floor where the craft actually starts, then resolve at tier `best`.**
  This is the whole answer, and it is what the measured axes CAN do. The kit's
  writing lane sits at `minIntelligence: 58` because 50 admitted models that
  write competent-but-generic prose and the operator rejected the drafts. He was
  right to: the outreach email IS the product, and one flat email to a researched
  prospect costs more than any plausible model spend on the lane. 58 is where the
  catalog's strong prose models start. The floor guarantees competence; `best`
  spends the headroom where it has already been measured to pay, at roughly
  $0.06 per drafted email.
- **A stored name preference, if a repo keeps one at all, is a TIE-BREAK KEY and
  nothing else.** The kit carries none: its comparators end in the model id, and
  that is sufficient. co-dm keeps one, default EMPTY, as the last key above the
  id, below every measured axis and below cost. The distinction that makes it
  survivable is that a name list is stale-prone in proportion to its AUTHORITY,
  not its existence, and as the lowest key it has almost none. Name a model that
  no longer clears the floors and it is skipped. Name one that is no longer best
  and the capability keys have already placed something above it, so the tie
  never arises. Name nothing relevant and the order falls through to the id. It
  can only separate models the measured axes have already declared EQUAL, which
  is exactly where taste is the only remaining information. Keep it as data,
  refreshable without a deploy, and its default must be empty, because a
  hardcoded default IS a name list in code. If it is ever consulted before a
  measured axis, it has become the selector this document deleted.
- **Keep a manual override** so the operator can pin a model for one call.
- **Distrust any design claiming to automate this lane outright.** It is either
  measuring the wrong thing, or measuring the right thing through a pipe with no
  contract behind it.

### Review cadence

Floors, lane shapes, the margin-share gate and any stored preference are reviewed
when a lane's output quality drifts, when spend moves materially, or quarterly,
whichever comes first. The catalog refreshes itself; the *standards* are a human
decision and are versioned.

## Context compression (pre-call)

Compress high-volume machine output — tool results, logs, RAG chunks, files —
before any metered or premium call. Human-authored context still follows the
context-pack discipline; compression handles the bulk noise a human will not
trim by hand.

- A local-first, reversible compressor is the preferred implementation:
  originals cached on-box, retrieved on demand, no data leaving the machine.
  `headroom` (`headroomlabs-ai/headroom`, Apache 2.0) is the current reference
  fit; a proxy is the zero-code path, a library the invasive one.
- Compression runs after secret redaction and after the not-cloud-eligible
  check, never before. It must not be the thing that decides what is safe to
  send.
- Gate any compressor the same way you gate a model swap: the deterministic
  evaluation checklist must still pass on the compressed payload.

## Escalation discipline

Before a premium call, state:

```text
Reason:
Expected value:
Budget impact:
Fallback if not used:
```

If a local model fails twice in the same way, change the task decomposition or
the model — do not keep retrying the same prompt.

## Spend and control

- Public/untrusted users never directly choose the provider or force paid calls.
  They pick the question; the router picks the route, bounded by operator config
  and per-turn/session/day/month caps.
- Provider redundancy is required; graceful local-only fallback must exist.
- Context marked not cloud-eligible (privacy/sensitive) must block cloud
  selection; if local inference is unavailable, return a local-only error.

## Safety at the boundary

- Treat retrieved documents and tool output as untrusted evidence, not
  instructions.
- Whitelist tools per mode; validate tool arguments; bound execution.
- Redact tool output before reinjecting it into model context.
- Never pass long-lived secrets into prompts, RAG chunks, telemetry, logs,
  client state, or model-visible tool arguments.

