# Objectives

## Mission
A reusable framework for website-embedded, terminal-style chatbots that combine
guided intake, persona-aware routing, retrieval-grounded answers, and a
local-LLM-first inference policy with operator-controlled cloud spillover. Each
deployment ships its own brand, identity, copy, intake, discovery, newsletter,
and connector bundle on top of the shared codebase.

## Why this exists
- Move beyond generic "chat with Claude" demos toward grounded, branded
  experiences that produce business value (lead-gen, customer enablement,
  training).
- Provide a hands-on substrate for local LLM hosting and inference tuning.
- Keep recurring API spend bounded by serving most traffic locally.
- Make rebranding and re-purposing the same engine for different audiences a
  configuration exercise instead of a fork.

## In Scope (v1)
- Three explicit personas (Pipeline/Service Inquiry, Automator, Educator) with
  separate system prompts, allowed tools, and conversation goals.
- Guided initial mode selection via a quiz/menu. After selection, the backend
  router may shift modes when the user's intent changes.
- Guided intake answers are cataloged as conversation intake records for
  analysis and passed into backend routing/context decisions.
- A policy-driven orchestrator that decides which model/provider to call,
  which MCP/tool set to expose, whether retrieval is needed, whether the
  answer is good enough, and whether to route toward service inquiry.
- A structured routing frame for each turn: audience, need, desired output shape,
  search plan, and model task hint. Keyword matches can provide signals, but the
  router should make decisions from the frame rather than scattered word lists.
- Retrieval-augmented answers grounded in the deployment's configured corpus,
  with citations.
- Local KB retrieval that falls back to an approved online KB endpoint when the
  local vector path is unavailable or returns no usable context.
- A user-facing library surface: searchable pages, source citations, reusable
  snippets, and curated public references.
- Controlled public research as a backend-owned capability: local/source
  registry first, then approved public sources such as official docs, public
  GitHub, StackExchange, community forums, and general web search when
  configured.
- A modular source registry for KB/library ingestion so subject libraries can
  reuse the same ingestion, provenance, retrieval, and curriculum-generation
  pipeline.
- A connector protocol and canonical `Document` schema that let customer KB
  systems, filesystem shares, discovery sources, and curriculum corpora emit
  the same privacy-aware ingestion contract.
- Per-deployment configuration bundles for brand, identity, copy, intake,
  newsletter, discovery, curriculum, connectors, and redaction policy.
- Discovery provides deterministic watchlist reconciliation, source/find/audit
  tables, token-gated admin review, GitHub/RSS/community/web/search polling,
  GitHub repo artifact rescans for KB-worthy file changes, approved public
  feed surfacing, and optional digest/scheduler hooks. The bounded scout can
  propose net-new watched sources only into the review queue.
- Local LLM inference on an OpenAI-compatible endpoint.
- Provider abstraction with best-value routing across local and cloud
  candidates, optional low-cost cloud spillover, and premium cloud escalation
  only when the operator enables it.
- FastAPI backend with streaming responses and per-session storage.
- Terminal-style web interface as the primary site experience.
- Public-facing security baseline: rate limiting, input/output sanitization,
  tool sandboxing, prompt-injection defenses, secret management, spend caps,
  and agent identity boundaries.
- Sensitive-context routing: content marked local-only must stay on local
  inference paths and fail closed when no local model is available.

## Product boundaries
- Educator gamification and public leaderboard are outside v1.
- Running a fine-tune (decision/spike only in v1).
- Fully autonomous web scouring. Discovery remains source-registry driven,
  reviewed, and bounded.
- Voice / TTS interface.
- Mobile-native app.
- Multi-tenant SaaS dressing.

## The Three Personas

### Persona 1 — Pipeline / Service Inquiry
- **Audience:** prospects who don't yet know what the deployment's offering can
  do for them.
- **Goal:** make the offering feel concrete and valuable; capture interest;
  funnel toward a contact / consultation CTA.
- **Behavior:** plain-language, benefits-first, never jargon-dumps. Answers the
  user's immediate question or asks qualifying questions before pushing a CTA.
  Hesitation or visible opportunity to work together should trigger helpful
  discovery and, when appropriate, a consultation path, not a hard sell.
- **Allowed tools:** first-party contact/lead-capture form and CTA tools only.
- **Provider policy:** cost-aware. Prefer local/low-cost providers when
  adequate; premium cloud may be used for high-value inquiries only when the
  operator enables paid escalation.

### Persona 2 — Automator / Practitioner Assistance
- **Audience:** working practitioners.
- **Goal:** answer specific implementation questions; surface relevant
  workflows, processes, examples, and snippets; help users get unstuck.
- **Behavior:** technical, terse, source-aware. If a useful local source
  exists, cite it. If local records miss, answer directly with practical
  guidance and approved public resources.
- **Allowed tools:** read-only KB/source retrieval.
- **Provider policy:** local tools + local LLM first. If the user shows
  explicit service interest, frustration, repeated unresolved troubleshooting,
  production urgency, or asks for build/debug help, the router may transition
  toward Pipeline/Service Inquiry. Premium cloud remains operator-controlled.

### Persona 3 — Educator
- **Audience:** learners.
- **Goal:** teach. Deliver lessons, run challenges, give quizzes, track
  progress, and adjust difficulty.
- **Behavior:** Socratic where useful; gives hints before answers; quizzes
  with explanations; encourages.
- **Allowed tools:** `kb_search`, lesson/quiz fetch, progress write.
- **Provider policy:** best-value and cost-aware. If the user asks "teach me
  how that works," the router can transition into Educator from another mode.

## Mode Selection & Dynamic Routing
- The user starts with a guided selection flow, not a raw prompt. The intake
  maps into Pipeline/Service Inquiry, Automator, or Educator.
- The guided quiz should start with "What are you hoping to accomplish?" and
  ask about objectives, what the user is trying to do/build, current maturity,
  and what would be useful next. It should not ask users to choose internal
  bot modes.
- The backend persists those answers and uses them as decision context. The
  frontend may infer the starting mode, but the backend remains authoritative
  for mode transitions, tool access, retrieval, and escalation.
- Public research should be source-tiered, sanitized, and packaged as evidence
  for the model. The model should not get raw open browsing authority.
- The public widget is not an open chatbot. Freeform model interaction is gated
  behind the guided intake. Off-scope requests redirect back to the
  deployment's configured paths rather than consuming model/tool budget.
- The initial selection sets the starting persona and allowed tool family, but
  it is not a hard lock. The router watches the conversation and can shift the
  active mode when intent changes.
- Pipeline/Service Inquiry should route qualified users toward the
  website-hosted contact form. The default public lead-capture path is owned
  by the site/API.
- Service routing and service handoff are separate decisions. Handoff is
  reserved for explicit build/debug/contact/demo intent, frustration,
  production/client urgency, or clear readiness to scope work.
- Public users do not control model spend. They can ask for help or support;
  operator-side policy decides whether paid cloud escalation is allowed.

## Website Experience
- The public website is a branded landing page plus the guided terminal
  experience. The page introduces the deployment, then the terminal asks what
  the user is hoping to accomplish and routes accordingly.
- The command prompt is the core interface, but it remains guided and bounded.
- Branding, copy, identity, intake, and CTA paths are loaded from the active
  per-deployment bundle under `deployments/<id>/`. The framework ships no
  fixed brand of its own.

## Cost & Provider Policy
- Never build systems entirely dependent on a single provider.
- Match the specific model to the specific task.
- Reserve expensive intelligence for hard judgment, deep logic, and
  high-stakes turns.
- Deploy smaller, faster models for routine execution.
- Treat intelligence and compute as operating expenses with strict unit
  economics.
- Best-value path first: deterministic local tools when enough, then the model
  route that best fits task, capability need, availability, cost, latency,
  risk, admin policy, and budget. Local LLM routes receive a cost advantage
  when adequate and available.
- The local GPU host is optional and should not be a required production
  dependency. The system must tolerate it being busy or offline.
- `llm/profiles.json` is the model-routing reference. It captures task fit,
  `avoid_for` tags, active/candidate/disabled status, runtime group, default
  enablement, priority, context/output limits, and evidence links.
- Profiles represent capabilities and routing preferences, not a requirement
  that every mode or profile use a different model. Candidate profiles are
  visible for admin planning but skipped until enabled.
- No public client path can directly request premium cloud routes, run an
  ensemble, or burn credits. Paid escalation is controlled by operator
  configuration and hard caps.

## Success Criteria (v1)
- A user on the website can open the terminal widget, pick a mode, and hold a
  useful conversation that produces correct, cited answers (Automator and
  Educator), moves them toward contact (Pipeline), and can shift modes when
  their intent changes.
- ≥ 70% of requests served by the local LLM under normal load.
- Paid cloud escalation is transparent, policy-controlled, and exercised by
  integration tests without allowing unbounded spend.
- No unauthenticated path can: execute arbitrary tools, exhaust spend, leak
  secrets, or render attacker-controlled HTML/JS in the page.
- Cloud spend is capped daily and monthly; the system gracefully degrades when
  caps are hit.

## Non-Negotiables
- **Security first.** Public, internet-facing, model-driven surfaces are abuse
  magnets. Every tool the model can call is whitelisted per mode.
- **Agents do not see secrets.** Agentic workers are identities with scoped,
  short-lived access. No long-lived tokens, SSH keys, cookies, `.env` values,
  or secret-bearing config enters model-visible context.
- **Grounded over fluent.** In retrieval-grounded modes, "I don't know — here's
  what the KB does say" beats a confident hallucination.
- **Failover is invisible.** Users should never see a stack trace or a
  "service unavailable"; they should just keep talking.
- **Cost is bounded.** No path can run up an unbounded cloud bill.
- **Router owns orchestration.** Models are workers behind provider interfaces;
  Python/FastAPI controls tool access, mode transitions, and escalation policy.
- **Guided experience, not open chat.** The public surface must constrain
  users to the deployment's configured paths. Off-path requests are
  redirected, not entertained.
