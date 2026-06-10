# Architecture

Detailed architecture for GestaltWorkframe. The root `claude.md` holds the short
guide and the enforceable hard rules; this file holds the deeper map.

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
- `core/key_store.py` is an AES-256-GCM encrypted SQLite key store for
  runtime API key management (openrouter, anthropic, google, openai, github,
  brave). Admin endpoints: POST/DELETE/GET/test under
  `/admin/api/provider-keys`. Storing a new key immediately rotates live
  provider instances via `LLMRouter.rotate_provider_key()`. Discovery
  handlers receive their auth token via `DiscoverySourceLike.auth_token`
  (resolved from key store by the scheduler; env var fallback preserved).
- `core/provider_balance.py` fetches live credit balance from OpenRouter
  (`GET /api/v1/auth/key`, 5-min cache) and estimates local tracking balance
  for other providers. Balance data appears in the admin health panel only.
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

## Testing
- `pytest` + `pytest-asyncio`. Tests live under `tests/`.
- Required tests for any new feature: happy path + one failure-mode test.
- The failover path **must** have an integration test that kills the local
  endpoint and asserts the conversation continues on a configured cloud
  fallback.
