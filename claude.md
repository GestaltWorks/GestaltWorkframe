# claude.md — Project Guide for AI Assistants

This file tells any AI coding assistant how to work in this repo. Read it
before suggesting changes. Update it when decisions change.

## What this project is
A reusable, brandable, multi-mode chatbot framework embedded in a website.
OpenRouter is the primary model-access path: a single API key covers free-tier,
low-cost, and premium escalation across hundreds of models. Paid cloud escalation
is operator-controlled. Local GPU and direct-SDK (Anthropic, Gemini) providers are
disabled bolt-ons that operators can enable when they bring their own hardware or
keys. Three starting personas: Pipeline/Service Inquiry, Automator/Practitioner
Assistance, Educator. See `objectives.md` for the why.

## Current state (snapshot)

A FastAPI backend plus a Next.js frontend:

- `api/main.py` builds the app and middleware; route modules handle chat,
  intake, contact, admin, discovery, health, and content-feed endpoints.
- `core/db/` contains SQLModel models, engine/session helpers, migrations, and
  CRUD. Main storage defaults to `database.db`; override with
  `APP_DATABASE_PATH` or `APP_DATABASE_URL`.
- `core/routing_frame.py`, `core/orchestrator.py`, `core/chat_orchestrator.py`,
  `core/providers.py`, `core/provider_registry.py`, `core/router.py`, and
  `core/cloud_budget.py` own routing, provider selection, adequacy checks,
  circuit breakers, concurrency, and cloud spend gates.
- Discovery lives in `core/discovery_*`, `core/discovery_handlers/`,
  `kb/watchlist*`, and the publisher module: deterministic source polling,
  admin review, SSRF-guarded targets, corpus publishing, latest feed, optional
  scout/digest, canonical `Document` projections, and no secret storage.
- `packages/gestalt-connector-protocol/` defines the canonical `Document`
  model, connector protocol, redaction pipeline, generated schema contract,
  and connector-test harness. `packages/gestalt-connector-fs/` is the
  reference filesystem/UNC connector.
- `deployments/<id>/` contains brand, identity, site, nav, copy, intake,
  connector, redaction, newsletter, discovery, and curriculum bundles.
  Runtime selection uses `DEPLOYMENT_ID`. Public deployment config is exposed
  at `/api/deployment-config`; full config remains admin-gated.
- `web/src/components/ChatWidget.tsx` is the guided terminal widget.
- Workflows: CI, dev/prod deploy, hourly discovery, daily retention, and
  daily SQLite backup.

## Target architecture (v1)

Next.js is the frontend only: branded pages plus the guided terminal command
layer. It forwards chat/API traffic to FastAPI. FastAPI owns orchestration,
mode transitions, tool whitelists, retrieval, provider routing, cloud-spend
gates, persistence, and response adequacy. Local models run on a configurable
OpenAI-compatible endpoint when available.

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

## Conventions

### Language & tooling
- **Python ≥ 3.10**, async-first, for everything backend (api/, core/,
  mcp_servers/, kb/, llm/).
- Dependency manager: **uv** (`pyproject.toml`, `uv.lock` are source of truth).
  Don't hand-edit `pyproject.toml` for deps — use `uv add` / `uv remove`.
- Type-hint everything new. Prefer `pydantic` models at trust boundaries
  (HTTP, MCP I/O, persisted records).
- **TypeScript** for the frontend (`web/`). Next.js App Router. Tailwind CSS.
  Package manager: **pnpm**. Don't hand-edit `package.json` for deps — use
  `pnpm add` / `pnpm remove`.
- The terminal widget is a **client component** (needs EventSource + DOM
  buffer control). Marketing pages stay server-rendered.
- No LLM SDKs, MCP clients, or provider logic in the Node runtime. Frontend
  only talks to its own Next.js routes (which proxy) or directly to the
  FastAPI service.

### Code style
- Match the existing terse style in `core/`. No docstring-stuffing, no
  rationale-as-comment. Comments only when the *what* isn't obvious.
- Public functions: type hints in, type hints out.
- No `print()` in library code — use the structured logger once it exists.

### Directory layout (target — propose before moving things)
- `core/` — chat engine, providers, router, persona system (Python).
- `mcp_servers/` — one folder per MCP server (kb, lessons, cta, …) (Python).
- `api/` — FastAPI app, routes, schemas, middleware (Python).
- `kb/` — ingestion pipeline + index store helpers (Python).
- `llm/` — local LLM bring-up scripts, model configs, eval harness (Python).
- `web/` — Next.js app (TypeScript): branded landing page, guided terminal
  command layer, contact/lead-capture form. Has its own `package.json` /
  `pnpm-lock.yaml`.
- Python tests mirror the source tree under `tests/`. Frontend tests live under
  `web/__tests__/` when a frontend test harness is added.

## Hard rules for AI edits

1. **Don't widen scope.** Do exactly what's asked. If you see a related
   issue, surface it as a question, don't silently fix it.
2. **Don't create docs unless asked.** No new `*.md` beyond what's in the
   task list. Update existing docs in place.
3. **Never commit, push, merge, or deploy** without explicit human approval.
4. **No secrets in code, logs, prompts, or model-visible context.** All API keys
   via env / secret manager. Anthropic key, model names, and endpoints come from
   env.
   - Agents are identities. Any agent/tool that needs external access gets only
     scoped, short-lived credentials from server-side storage.
   - Never pass long-lived credentials, raw tokens, SSH keys, cookies, `.env`
     values, or secret-bearing config into LLM context, RAG chunks, telemetry, or
     client-visible state.
   - Prefer brokered server-side actions over handing credentials to agents.
5. **Security defaults are not optional.** When adding any code path
   reachable from the public API:
   - validate input with pydantic,
   - whitelist tools per mode (don't pass the global tool set),
   - render model output as text only,
   - rate-limit and log the request.
6. **Provider abstraction is sacred.** New code talks to `LLMProvider`,
   never directly to `anthropic.Anthropic` or the local endpoint.
7. **Grounded answers.** Automator and Educator modes cite KB sources when
   making KB claims. Retrieval-grounded turns are graded before user-visible
   streaming so unsupported answers can be replaced instead of warning-suffixed
   after the fact. Reject non-handoff service CTAs, uncited source claims, and
   external URLs that are neither present in retrieved context nor explicitly
   approved public resources. The corpus is a source library, not a cage. If
   it has a useful hit, answer directly and cite it. If it has nothing
   relevant, answer directly with concise general guidance.
8. **Don't bypass the router** to "just call Claude" inside a feature.
   If a feature needs Claude specifically, configure it in the router.

9. **Router owns orchestration.** Models are workers behind `LLMProvider`; the
   Python backend decides mode transitions, tool access, answer adequacy, and
   escalation.
   Public chat uses backend-owned retrieval by default. Provider tools can only
   be passed into model calls through the server-side model-tool loop, with
   whitelisting, Pydantic argument validation, bounded execution, quarantined
   tool-result reinjection, logging, and final-answer enforcement.
10. **No user-controlled credit burn.** Public users can ask for help or
   support, but cannot directly force Claude, ensembles, or paid cloud calls.
   The router may still escalate to cloud on its own when task fit, capability,
   and value justify it, bounded by operator config and the per-turn/session/
   day/month spend caps. The user picks the question, never the provider or the
   spend.
11. **Check the plan before changing direction.** Before architecture, routing,
   provider, mode, MCP/tool, cost, security, or deployment changes, compare the
   proposed approach against `objectives.md` and this file. If the work reveals
   a new decision or changes the plan, update the docs in place before moving on.
12. **Guided public experience.** Do not build an unconstrained public chatbot.
   The public widget must require guided intake and keep users on the
   deployment's configured paths.
13. **Docs before push.** When repo state, deployment workflow, remotes,
   environment behavior, or project direction changes, update `README.md`,
   `claude.md`, `objectives.md`, and any task tracking before pushing.
14. **Review major changes proportionally to blast radius.** Use a branch,
   non-draft PR, and CI for major feature changes, landmark/milestone
   reviews, security or public API changes, provider/router/tool changes,
   deployment changes, data model changes, and cross-system changes. Direct
   commits to the production branch are acceptable for small isolated UI
   tweaks, copy changes, operational fixes, or explicit hotfixes after local
   validation. Adapt the exact branch/PR/CI/deploy mechanics to whatever
   tooling the host project actually uses.
15. **Drive the next step.** After making a requested change, do not stop at
   "changes are pending" unless blocked. Validate the change, classify
   review risk, and proceed to commit, PR, merge, and deploy when the
   user's request and review policy allow it. Stop and ask only for
   ambiguity, failing validation, protected actions that still need
   approval, or production smoke-test failures.

## Public terminal threat model
- **Public surfaces:** `/terminal`, `/chat/stream`, `/intake/submissions`,
  `/contact`, `/health/providers`, and admin health pages. Admin API routes stay
  token-gated and must never expose secrets or provider internals to public
  health responses.
- **Trust boundaries:** browser input, guided intake answers, chat history,
  retrieved KB chunks, MCP/tool results, and model output are untrusted. The
  backend owns routing, tool access, retrieval, provider selection, cloud spend,
  and service-handoff decisions.
- **Primary threats:** prompt injection, secret exfiltration, public credit burn,
  oversized payloads, spam submissions, cross-site browser posts, unsafe link or
  HTML rendering, poisoned KB/tool context, and accidental leakage of model names,
  budgets, route diagnostics, tokens, or local infrastructure details.
- **Controls in force:** guided intake gate, Pydantic validation, route-specific
  body limits, same-origin checks for public state-changing routes, IP/session/
  token abuse budgets, deterministic refusal for prompt override or secret
  requests, quarantined untrusted context, backend-only tool whitelists, local-
  first router policy, operator-controlled cloud caps, discovery target SSRF
  guards, generic stream errors, public health redaction, and text-only
  terminal rendering.
- **Residual risks:** IP limits are soft under concurrency, origin checks only
  reduce browser-based abuse, local ignored files can still contain operator
  secrets, and KB quality depends on source review. Treat these as operating
  limits for public traffic.

## Testing
- `pytest` + `pytest-asyncio`. Tests live under `tests/`.
- Required tests for any new feature: happy path + one failure-mode test.
- The failover path **must** have an integration test that kills the local
  endpoint and asserts the conversation continues on a configured cloud
  fallback.

## What NOT to do
- Don't replace `uv` with `pip`/`poetry`.
- Don't replace `pnpm` with `npm`/`yarn` in `web/`.
- Don't introduce a second LLM SDK alongside `anthropic` without going
  through the provider interface.
- Don't put LLM/provider logic in Next.js. The Node runtime is a frontend +
  proxy; orchestration belongs in FastAPI.
- Don't render model output as HTML in the terminal widget — text only,
  with explicit, sanitized link/citation rendering.
- Don't add a "shell tool", "exec tool", or anything that lets the model
  touch the filesystem on the server.
- Don't hardcode deployment-specific paths or branding into core code —
  load them from the active `deployments/<id>/` bundle.
