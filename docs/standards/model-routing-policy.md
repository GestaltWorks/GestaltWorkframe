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
   Free-tier routes have zero marginal cost; metered/premium cost is incurred
   only when task fit justifies it.
4. The application owns policy, credentials, routing, memory, and final
   acceptance. Models are workers, not decision-makers.
5. Tokens-per-call is a cost lever independent of tier. Shrink the payload
   before the call; this compounds with routing instead of competing with it.

## Default routing order (best value)

1. Deterministic tools first when they can verify or compute the answer.
2. Local/free models for routine execution: summarizing, drafting routine code,
   refactors with clear tests, markdown/templates, boilerplate, first-pass
   investigation.
3. Metered (low-cost) cloud only when free/local is inadequate.
4. Premium cloud reserved for: architecture with long-term cost, security
   review, hard debugging after local attempts fail, release readiness review,
   and code where mistakes cause data loss, auth/payment bugs, or outages.

Under "best value" the cost/value lean toward cheaper tiers wins ties and
near-ties; task fit still dominates, so a genuinely hard turn escalates over the
lean.

## Capability-based selection (never hardcode model IDs)

A model name in application code is a stale constant with a short shelf life.
It encodes a judgment ("this is the best one for X") that stops being true the
week a better or cheaper model ships, and it can only be corrected by a code
change and a redeploy. Hardcoding is the "habit" Principle 2 forbids.

**Declare what the task requires; resolve the model at runtime.**

A task lane is a policy record, not a name:

```text
guide:  must:[tools], minContext:200k, minAgentic:50, minIntelligence:55, prefer:quality
lookup: must:[tools], minIntelligence:30, maxPromptPrice:$1/M,           prefer:cost
```

Resolution order: fetch the catalog -> drop anything missing a hard requirement
-> drop expired/deprecated -> apply the quality floor -> rank by `prefer` ->
take the top. Hard requirements are objective and machine-checkable; they are
never a matter of taste.

- **Catalog.** OpenRouter's `GET /api/v1/models` is public (no auth) and returns,
  per model: `pricing` (prompt, completion, and `input_cache_read`),
  `supported_parameters` (`tools`, `tool_choice`, `structured_outputs`,
  `reasoning`, ...), `context_length`, `expiration_date`, and
  `benchmarks.artificial_analysis` (`intelligence_index`, `coding_index`,
  `agentic_index`). Cache it (~24h) and ship a small pinned fallback list: a
  catalog fetch failure must degrade, never break the app.
- **Tool-calling is a hard filter, not a preference.** Any agentic loop must drop
  every model whose `supported_parameters` lacks `tools`. A cheap model that
  cannot call tools is not cheap; it is broken.
- **Quality floors use published indices**, not vibes. `agentic_index` is the
  proxy for tool adherence; `intelligence_index` for reasoning. Floors are
  reviewable numbers, so a lane's standard is legible and arguable.
- **Rank on cost per turn, not on a sticker rate.** Two things are routinely
  left out and both flip picks:
  - *Output.* Completion is usually priced 3-5x input, so a model that is cheap
    to prompt can be expensive to answer. Rank on
    `input x expected_in + completion x expected_out`, with the expected shape
    declared per lane (a lookup and a long-form build are not the same turn).
  - *Cache reads.* When a large stable prefix is replayed each turn (system
    prompt, style corpus, retrieved context), include `input_cache_read`. A
    "cheap" model without cache pricing can lose to a cached premium one.

  Both are estimates; declare them explicitly as tunable lane parameters rather
  than burying them in a comparison. Ranking on input price alone is the common
  bug — it looks right and quietly picks the wrong model.
- **Transport mapping is config, not judgment.** A table mapping a catalog slug
  to the gateway's alias (which name the broker routes direct vs. via the
  aggregator) is legitimate configuration. A table asserting which model is
  *best* is not.
- **Data handling is a hard filter, applied before price.** Exclude `:free`
  tiers: they are generally trained on submitted prompts, and a cost ranking
  will otherwise always select them, because zero is a degenerate optimum. A
  route is only "cheap" if it is allowed to see the payload — apply the
  not-cloud-eligible check (see "Spend and control") as a filter on the
  candidate set, not as an afterthought. Exclude preview/experimental builds
  from anything user-facing; a live session must not break because a provider
  rotated a preview endpoint. Any override is explicit and opt-in.
- **Availability is the third axis, and it must be OBSERVED.** Price and quality
  are necessary and not sufficient: a model that benchmarks well and prices well
  is worthless while it is timing out or rate-limiting you. Do not trust a
  provider's published uptime for this — that metric tracks whether the endpoint
  answers at all, not whether *your* requests succeed, so rate limits, 429s,
  context rejections, and tool-format quirks all still read as "up" (seen in
  practice: a model reported as failing showed `uptime_last_5m = 100`). Keep
  your own error counter per model, bench a repeat offender for a backing-off
  cooldown, and clear it on the first success.
- **Fail sideways, not upward.** When the chosen model errors, retry on the next
  candidate that already cleared the same lane floors before escalating to the
  premium fallback. A single pinned fallback turns a cheap provider's bad ten
  minutes into a frontier-priced turn — the opposite of the intent.
- **Never swap models mid-answer.** Retry only while nothing has been streamed
  to the user; splicing two models' prose into one reply is worse than a clean
  failure.
- **Log the chosen model back to the operator.** Automatic selection without a
  visible decision is unauditable: surface which model ran and why, so "why did
  this cost that" and "where did this data go" both have answers. Expose which
  models are currently benched, so a degrading provider is visible rather than
  silently routed around.

### What capability selection cannot do

Taste is not benchmarkable. No index tells you which model writes the house
voice, holds a narrative register, or keeps a brand's cadence. For lanes judged
on craft rather than correctness:

- resolve to a **shortlist** that clears the objective floors, then order it by a
  stored human preference — data (config), refreshable without a deploy, not a
  constant in code;
- keep a **manual override** so the operator can pin a model for one call;
- distrust any design claiming to automate this lane. It is measuring the wrong
  thing.

### Review cadence

Floors and preferences are reviewed when a lane's output quality drifts, when
spend moves materially, or quarterly — whichever comes first. The catalog
refreshes itself; the *standards* are a human decision and are versioned.

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

