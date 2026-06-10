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
