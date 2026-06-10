# claude.md — Project Guide for AI Assistants

Read this before suggesting changes. Update it when decisions change. Keep this
file short and repo-specific; deeper material lives in `docs/`.

## What this project is
A reusable, brandable, multi-mode chatbot framework embedded in a website.
OpenRouter is the primary model-access path: a single API key covers free-tier,
low-cost, and premium escalation across hundreds of models. Paid cloud escalation
is operator-controlled. Local GPU and direct-SDK (Anthropic, Gemini) providers are
disabled bolt-ons that operators can enable when they bring their own hardware or
keys. Three starting personas: Pipeline/Service Inquiry, Automator/Practitioner
Assistance, Educator. See `objectives.md` for the why.

## Read first

- `README.md` — product setup.
- `objectives.md` — product intent and the "why".
- `docs/architecture.md` — current state, target architecture, conventions, tests.
- `docs/routing-policy.md` — routing, knowledge-library, and education policy.
- `docs/threat-model.md` — public terminal threat model.
- `docs/standards/` — generic engineering, security, and routing doctrine
  (synced from the LLM Builder Kit). Use relative paths, never `d:\Scripts\...`.

## Hard rules for AI edits

1. **Don't widen scope.** Do exactly what's asked. Surface related issues as a
   question; don't silently fix them.
2. **Don't create docs unless asked.** Update existing docs in place.
3. **Never commit, push, merge, or deploy** without explicit human approval.
4. **No secrets in code, logs, prompts, or model-visible context.** Keys via
   env/secret store. Agents are identities: scoped, short-lived credentials only,
   from server-side storage. Prefer brokered server-side actions. See
   `docs/standards/secrets.md`.
5. **Security defaults are not optional.** For any public-API-reachable path:
   validate input with Pydantic, whitelist tools per mode, render model output as
   text only, rate-limit and log. See `docs/threat-model.md`.
6. **Provider abstraction is sacred.** New code talks to `LLMProvider`, never
   directly to `anthropic.Anthropic` or the local endpoint.
7. **Grounded answers.** Automator/Educator cite KB sources for KB claims;
   grade retrieval-grounded turns before user-visible streaming. Reject
   non-handoff service CTAs, uncited source claims, and unapproved external URLs.
8. **Don't bypass the router** to "just call Claude". Configure it in the router.
9. **Router owns orchestration.** Models are workers; the backend decides mode
   transitions, tool access, adequacy, and escalation. Tools reach models only
   through the server-side loop: whitelist, Pydantic validation, bounded
   execution, quarantined reinjection, logging, final-answer enforcement.
10. **No user-controlled credit burn.** Users pick the question, never the
    provider or the spend; router escalation is bounded by operator caps.
11. **Check the plan before changing direction.** Before architecture, routing,
    provider, mode, MCP/tool, cost, security, or deployment changes, compare
    against `objectives.md`, `docs/`, and this file; update docs in place first.
12. **Guided public experience.** No unconstrained public chatbot; require guided
    intake and keep users on configured paths.
13. **Docs before push.** When repo state, deployment, remotes, environment, or
    direction changes, update `README.md`, `claude.md`, `docs/`, `objectives.md`,
    and task tracking before pushing.
14. **Review proportionally to blast radius.** Branch + non-draft PR + CI for
    major features, security/public-API/provider/router/tool/deploy/data-model/
    cross-system changes. Direct commits to the release branch are fine for small
    isolated UI/copy/operational fixes after local validation.
15. **Drive the next step.** After a requested change, validate, classify review
    risk, and proceed to commit/PR/merge/deploy when request and policy allow.
    Stop only for ambiguity, failing validation, protected actions needing
    approval, or production smoke-test failures.

## What NOT to do
- Don't replace `uv` with `pip`/`poetry`, or `pnpm` with `npm`/`yarn` in `web/`.
- Don't add a second LLM SDK without going through the provider interface.
- Don't put LLM/provider logic in Next.js; the Node runtime is frontend + proxy.
- Don't render model output as HTML in the terminal — text only, sanitized links.
- Don't add a shell/exec/filesystem tool the model can reach on the server.
- Don't hardcode deployment-specific paths or branding into core code — load
  from the active `deployments/<id>/` bundle.

## Definition of done
Requirement met; relevant checks run (or blocker recorded); security-sensitive
paths reviewed; changed files and verification summarized; deployment/rollback
status stated when deployment is part of the task.
