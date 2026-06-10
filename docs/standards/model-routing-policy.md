<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->

# Model Routing Policy

Canonical local-first / best-value routing doctrine for any repo in this shop.
This is the source of truth; repos vendor a copy as `docs/standards/model-routing-policy.md`.

## Principles

1. Never depend entirely on a single provider. Use an aggregator (e.g.
   OpenRouter) as the primary access path; local GPU and direct-SDK providers are
   optional bolt-ons, disabled by default until hardware/keys are available.
2. Match the specific model to the specific task. Capability need, not habit,
   drives selection.
3. Treat intelligence and compute as operating expense with unit economics.
   Free-tier routes have zero marginal cost; metered/premium cost is incurred
   only when task fit justifies it.
4. The application owns policy, credentials, routing, memory, and final
   acceptance. Models are workers, not decision-makers.

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

