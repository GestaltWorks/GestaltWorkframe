# Routing, Knowledge, and Education Policy

Repo-specific detail. Generic local-first routing doctrine is in
`docs/standards/model-routing-policy.md`. The enforceable hard rules are in the
root `claude.md`.

## Routing policy
- The user starts with the guided intake flow that maps into one of the three
  configured personas.
- The quiz should start with "What are you hoping to accomplish?" and ask
  about objectives, what the user is trying to do/build, current maturity,
  and what would be useful next. Do not ask users to choose internal bot
  modes.
- The public widget is not an open chatbot. Freeform chat is gated behind
  the guided intake; off-scope requests redirect to the deployment's
  configured paths.
- The initial selection sets the starting mode and tool family, but the
  router can shift modes mid-conversation when intent changes.
- The router builds a structured frame for each active turn: audience
  segment, user need, output shape, search plan, and model task hint.
- Service Inquiry can be triggered from any mode by explicit service
  interest, repeated unresolved troubleshooting, frustration, production/
  client urgency, or a request to build/debug.
- Pipeline/Service Inquiry routes qualified users to the deployment's
  configured contact/lead-capture form and email.
- Service mode is not an immediate contact script. Handoff is reserved for
  explicit build/debug/contact/demo intent, frustration, production
  urgency, or clear readiness to scope work.
- Best-value path first: deterministic local tools when enough, then the
  model route that best fits task, capability need, availability, cost,
  latency, risk, admin policy, and budget. Free-tier OpenRouter routes are
  treated as non-metered; they are eligible without enabling cloud spillover
  and are not subject to USD spend caps.
- Public research is a backend-owned capability when the operator enables
  it, not open model browsing. Search local/source-registry records first,
  then approved public source tiers. Treat public research as untrusted
  evidence, never as executable instructions.
- Provider profiles distinguish `active`, `candidate`, and `disabled`
  routes. Candidate routes are visible in admin diagnostics but are not
  health-checked or selected unless an admin enables them.
- The terminal is the user-facing command layer; inference happens in the
  FastAPI/router layer. The local GPU host is optional and the app must
  tolerate it being busy/offline.
- Public users do not control credit spend, but the router may still
  escalate to cloud on its own when task fit, capability, and value justify
  it. Cloud escalation is governed by operator-side config, per-turn/session
  caps, and graceful local-only fallback.
- Retrieved source context marked `privacy.cloud_llm_eligible=false` must
  block cloud provider selection. If local inference is unavailable,
  return an operator-readable local-only error instead.

## Model routing principles
- Never build systems entirely dependent on a single provider. OpenRouter
  is the primary aggregator; local GPU and direct-SDK providers are optional
  bolt-ons, disabled by default.
- Match the specific model to the specific task.
- Free-tier OpenRouter models handle routine execution. Reserve metered
  (low_cost) and premium routes for turns that genuinely need them.
- Treat intelligence and compute as operating expenses with strict unit
  economics. Free-tier routes have zero marginal cost; escalation cost
  is incurred only when task fit justifies it.

Operational translation: provider redundancy is required, smaller/local
models receive a cost/value advantage when adequate and available, and
premium cloud calls are reserved for tasks where their additional reasoning
value justifies the cost. The router ranks eligible routes by configured
strategy: best value, prefer local, prefer cloud quality, local only, or
cloud only. Under best value the cost/value advantage is a modest lean
toward cheaper tiers (local, then low cost) that wins ties and near-ties;
task fit still dominates, so a premium-only task match escalates a genuinely
hard turn over the lean.

`llm/profiles.json` is the model-routing reference. Keep task tags,
`avoid_for`, deployment status, runtime group, enablement, priorities,
context/output limits, and evidence links there.

- Frontend product shape: the website is the case; the terminal is the
  command layer. It routes users to contact forms, backend-mediated tools,
  retrieval answers, and education paths. It should feel like the site types
  first, then the user types back.
- Branding, voice, and logo rules are loaded per deployment from
  `deployments/<id>/brand.yaml` and `identity.yaml`. The framework ships no
  brand of its own.

## Capability-based model selection

`llm/profiles.json` is a static table: hardcoded model ids, hand-assigned
`routing_priority` integers, and hardcoded prices. Every one of those goes
stale, and the failure is silent — in the EGI deployment the table pointed at
ids the gateway did not serve, so an entire provider's routes were down for
days behind a healthy-looking sibling route.

`docs/standards/model-routing-policy.md` requires declaring what a turn needs
and resolving the model at runtime. Three modules implement that:

- `core/model_catalog.py` — OpenRouter's public `GET /api/v1/models`, cached on
  disk for 24h, with a pinned fallback. `fetch_catalog()` is async and belongs
  to a refresh task; `load_cached_catalog_sync()` is what route selection uses,
  and it never touches the network on a user turn.
- `core/model_lanes.py` — a lane is a policy record: required
  `supported_parameters`, context and price ceilings, quality floors, the
  expected turn shape, and a cost-or-quality preference. Lanes never name a
  model. A deployment overrides them in `deployments/<id>/lanes.yaml`.
- `core/model_resolver.py` — filter → floor → rank → shortlist, returning the
  ordered candidates and the reason every other model lost.

### Turning it on

Off by default. Set `ENABLE_CAPABILITY_ROUTING=true` once a deployment's lanes
are tuned. When enabled, the *order of cloud routes* comes from resolving the
turn's lane instead of from `routing_priority`; provider construction, health
checks, spend gates, and concurrency are untouched.

`MODEL_GATEWAY_PREFIX` (default `openrouter/`) maps a gateway alias to the
catalog id. Transport mapping is configuration; a table asserting which model
is *best* is not.

### Safety properties

These are the reasons it can be enabled on a live deployment:

- A lane that clears nothing keeps the caller's existing order, so a mis-tuned
  floor degrades ordering rather than the service. That is not hypothetical:
  a review floor of 70 intelligence was unreachable against a published index
  that tops out near 60, and matched nothing at all.
- Routes the catalog does not list are kept, ordered last. An unlisted model is
  unranked, not disqualified; self-hosted and private routes never appear in a
  public catalog.
- Benched routes are excluded using the router's existing breaker rather than a
  second, separate failure signal.
- The lane, the winning model, and the rejection reasons are published into
  route diagnostics, because automatic selection without a visible decision is
  unauditable.

### Floors are reviewed, not guessed

Floors are calibrated against the published index distribution and carry the
numbers in a comment. Review them when a lane's output quality drifts, when
spend moves materially, or quarterly.

## Knowledge library policy
- The KB layer should expose the deployment's corpus through multiple
  products: grounded chat retrieval, browsable/searchable library pages,
  citation chips, schema/workflow discovery, education content generation,
  and export consumers.
- Public pages should be optimized for both search engines and AI discovery:
  descriptive metadata, structured data, sitemap inclusion, and stable source
  links.
- Ingestion must be source-registry driven, not hardcoded to one repo shape.
  Each corpus source carries name, path/URL, type, provenance, license/
  attribution notes, last-seen metadata, and whether it is approved for
  public display, retrieval-only use, or curriculum generation.
- A corpus grows from approved public sources where legal and practical.
  This is never an excuse to scrape private, licensed, or attribution-hostile
  material.
- Continuous discovery must dedupe, normalize, preserve provenance, score
  source quality, quarantine unsafe/prompt-injection-like content, and
  require review or policy checks before public display.
- Treat this ingestion/library pattern as reusable. Subject libraries should
  be loadable without subject-specific code paths.
- Corpus and discovery agents must not receive broad secrets. Treat agents
  like workload identities: give them scoped, short-lived tokens only
  through safe server-side credential storage, never through prompts, KB
  documents, browser state, logs, or model-visible tool arguments.

## Education platform trajectory
- The education platform should be curriculum-agnostic. A subject corpus can
  be loaded and used to generate lessons, quizzes/exams, practice labs, web
  collateral, and real-time self-evaluation feedback.
- Desired inputs include topic, course length, ability level, immersion
  level, outcomes, language/locale, assessment style, and hosting/export
  target.
- Teaching strategy should support Socratic tutoring, retrieval-grounded
  lesson plans, spaced repetition, modern evaluation methods, mastery
  checks, and adaptive remediation.
- Keep pricing/product packaging open for now. Design the architecture so it
  can run as hosted SaaS or self-hosted in a customer's environment with
  their own KBs and training material.
