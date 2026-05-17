# Contributing

Thanks for your interest in Gestalt Workframe. This project is a reusable
framework, so most contributions fall into one of three buckets:

- **Framework improvements** — router, provider abstraction, retrieval,
  connectors, discovery pipeline, admin tooling, tests.
- **New connectors** — additional implementations of the connector protocol
  defined in `packages/gestalt-connector-protocol/`.
- **Documentation and examples** — clarifications to `README.md`,
  `claude.md`, `objectives.md`, and the `deployments/test-brand/` bundle.

Deployment-specific branding, copy, and integrations belong in your own
`deployments/<id>/` bundle, not upstream.

## Ground rules

1. Read `claude.md` before opening a non-trivial PR. It captures the routing,
   security, and architecture conventions the framework relies on.
2. Keep changes scoped. Surface unrelated issues in a separate PR or issue.
3. No proprietary branding, customer data, secrets, or private endpoints in
   commits, fixtures, or tests.
4. New code paths reachable from the public API must validate input with
   Pydantic, respect the per-mode tool whitelist, and rate-limit appropriately.
5. Talk to LLM providers through `core.providers.LLMProvider`, never directly
   to a vendor SDK.

## Development setup

```bash
uv sync --group dev
uv run pytest                    # backend test suite
cd web && pnpm install && pnpm lint && pnpm build
```

`pyproject.toml` and `uv.lock` are the source of truth for Python deps. Use
`uv add` / `uv remove`. Frontend deps are managed with `pnpm`.

## Tests

- Every new feature: at least one happy-path test and one failure-mode test.
- Anything that touches provider selection or cloud spend must be covered by
  a routing test and a failover/budget test.
- Tests live under `tests/` (Python) and `web/__tests__/` (frontend) and
  mirror the source tree.

## Pull requests

- Branch off `dev` for development work, `main` for production hotfixes.
- Open a non-draft PR. CI must pass before merge.
- Reference the issue or design note the PR addresses.
- For changes that touch routing, providers, security, public APIs, data
  models, or deployment workflows, include a short "blast radius" note in
  the PR description.

## Reporting issues

Use GitHub issues for bugs, design questions, and feature requests. For
security-sensitive reports, follow `SECURITY.md` instead.
