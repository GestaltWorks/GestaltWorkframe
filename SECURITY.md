# Security policy

## Reporting a vulnerability

If you believe you have found a security issue in Gestalt Workframe, please
**do not open a public GitHub issue**. Instead, open a private security
advisory through GitHub:

> Repository → Security → Report a vulnerability

Include:

- A description of the issue and its impact.
- The minimal steps or proof-of-concept needed to reproduce it.
- The affected commit, tag, or deployment configuration.
- Any suggested remediation, if you have one.

We aim to acknowledge reports within five business days and to issue a fix
or mitigation timeline within fifteen business days for confirmed issues.

## Scope

This repository ships a framework, not a hosted service. The following are
in scope when running an unmodified `deployments/test-brand/` deployment:

- The FastAPI application under `api/` and `core/`.
- The Next.js frontend under `web/`.
- The connector protocol and reference connectors under `packages/`.
- The discovery pipeline and admin endpoints.
- The packaged deploy scripts under `.github/scripts/`.

Out of scope:

- Operator misconfiguration of secrets, environment variables, or third-party
  providers in a downstream deployment.
- Bugs in third-party model providers, hosting platforms, or LLM runtimes.
- Issues that require physical access to an operator's machine or network.

## Public chat threat model

The public chat surface is intentionally narrow. The relevant controls are
documented in `claude.md` under *Public terminal threat model*. Reports that
demonstrate a bypass of any of those controls are always in scope, including:

- Prompt injection that exfiltrates secrets, bypasses guided intake, or
  causes the model to call disallowed tools.
- Public credit burn or budget bypass on cloud providers.
- SSRF, path traversal, or arbitrary fetch through the discovery pipeline or
  any connector.
- Leakage of model names, provider routes, tokens, or local infrastructure
  details through public endpoints.

## Handling secrets

Never include real API keys, tokens, SSH keys, customer data, or production
URLs in:

- commits, branches, or PR descriptions,
- test fixtures, KB documents, or deployment bundles,
- bug reports, screenshots, or recordings.

If a secret is accidentally committed, rotate it first and report it through
the channel above.

## Supported versions

Only the current `main` branch receives security updates. There are no
long-term-support branches at this time.
