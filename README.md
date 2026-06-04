# Gestalt Workframe

A multi-mode, brandable chatbot framework: guided intake, persona routing
(Pipeline / Automator / Educator), retrieval-grounded answers, local-LLM-first
inference, operator-controlled cloud spillover budgets, and per-deployment
configuration bundles for brand, identity, copy, intake, newsletter, discovery,
connectors, and redaction policy.

Ship your own bundle under `deployments/<id>/` and select it via the
`DEPLOYMENT_ID` env var. See `objectives.md` for product vision and `claude.md`
for architectural conventions.

## Repo structure

```
api/           FastAPI app — /chat/stream, /contact, /intake/*, /health, /health/providers, admin discovery
core/          Chat engine, LLM providers, router, persona system, discovery scheduler
deployments/   Per-deployment brand, identity, copy, intake, newsletter, discovery config bundles
docs/schemas/  Versioned canonical JSON Schema artifacts
mcp_servers/   One folder per MCP server (kb, lessons, cta, …)
kb/            Ingestion pipeline, source registry, watchlist, vector store helpers
llm/           Local LLM bring-up scripts and model configs
packages/      Connector protocol package plus connector implementations
tests/         pytest suite (mirrors source tree)
web/           Next.js frontend — landing page, guided terminal, Library, contact form, admin health
```

## Model routing principles

- Never build systems entirely dependent on a single provider.
- Match the specific model to the specific task.
- Reserve expensive intelligence for hard judgment, deep logic, and high-stakes turns.
- Deploy smaller, faster models for routine execution.
- Treat intelligence and compute as operating expenses with strict unit economics.
- Route by best value for the request, not by a hard local-vs-cloud hierarchy.
  Best value considers task fit, model capability, availability, cost, latency,
  risk, admin policy, and budget. The personal GPU is an optional local provider,
  not a production dependency or a requirement to run every local candidate.
- `llm/profiles.json` is the model-routing reference. It records recommended task
  families, `avoid_for` task tags, active/candidate/disabled deployment status,
  runtime group, default enablement, capability flags, tool-calling quality,
  context/output limits, and evidence links.
  Default premium Claude path is Sonnet 4.6 or newer when paid escalation is
  enabled; Opus is available for deeper reasoning, architecture, critical
  review, and high-stakes agentic work when the value justifies the higher cost.
- Profiles are routing/capability declarations, not a guarantee of one physical
  model per mode. Multiple profiles can point at the same endpoint/model when it
  is the best callable tool for the job. Model-driven tool loops are route-aware:
  if no reliable tool-calling route is eligible, the backend uses direct retrieval
  and only sends quarantined retrieved context to the selected model.
- Gemini is represented as an OpenAI-compatible Google AI Studio cloud route. It
  is callable only when the API process has the server-side base URL and API key
  configured. Gemma is a separate open-weight local/hosted model family.
- Local llama.cpp profile URLs preserve their per-profile ports when
  `LOCAL_LLM_BASE_URL` points at the remote GPU host.
- OpenAI is not an active provider yet. If added later, default policy is
  free-tier-only unless the operator explicitly enables paid OAI spend
  server-side.

## CI/CD

Branch strategy:

- `dev` is the development integration branch.
- `main` is the production branch.

GitHub Actions:

- `CI` runs backend tests and frontend lint/build on pushes to `dev`, pushes to `main`, and pull requests.
- `Claude code review` runs on PR open/update/reopen/ready-for-review and manual dispatch. It does not run on direct pushes. Review output is written to the job summary and posted to the PR when GitHub permissions allow it. It reports model, token usage, and approximate estimated cost using Sonnet pricing assumptions unless `CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION` / `CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION` are overridden.
- `Deploy development` runs on pushes to `dev` and deploys to the `development` environment.
- `Deploy production` is manual-only from `main`; type `DEPLOY` in GitHub Actions to release production.

Required repo-level deploy secrets used by both deployment workflows:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_KEY`

Deploy targets are operator-configured. Workflows sync source with `rsync`,
preserve remote `.env` and `database.db`, build the static frontend export, and
restart the FastAPI API through systemd. Production is gated by a manual
`workflow_dispatch` confirmation.

## Prerequisites

- Python ≥ 3.10, [uv](https://github.com/astral-sh/uv)
- Node.js ≥ 18, [pnpm](https://pnpm.io/)

## Quickstart

This repo ships with a generic `test-brand` deployment (Northstar Automation
Lab) you can run unmodified to see the framework end-to-end. Two paths,
pick one.

### Docker (recommended for evaluation)

A three-container stack — FastAPI backend, Next.js static export, and an
nginx reverse proxy — runs everything behind a single origin on port 8080.

```bash
git clone <this-repo>
cd GestaltWorkframe
cp .env.example .env             # optional; fill in only what you need
docker compose up --build
# open http://localhost:8080
```

Persistent state lives in a named Docker volume (`app-data`) holding the
SQLite database and the Chroma vector store. `deployments/` is mounted
read-only into the API container, so brand/identity/copy edits on the host
take effect on the next container restart.

### Local toolchain (recommended for development)

```bash
git clone <this-repo>
cd GestaltWorkframe

# Backend
uv sync
cp .env.example .env             # fill in only what you need
uv run uvicorn api.main:app --reload

# Frontend (in a second shell)
cd web
pnpm install
echo "NEXT_PUBLIC_API_URL=http://127.0.0.1:8000" > .env.local
pnpm dev                         # http://localhost:3000
```

The site runs entirely offline against the bundled `test-brand` deployment.
Local-LLM-first inference is optional — without a `LOCAL_LLM_BASE_URL` or
cloud key, the chat surface returns a deterministic "no provider available"
message so you can still walk the routing, intake, and admin flows.

### Ship your own deployment

1. Copy `deployments/test-brand/` to `deployments/<your-id>/`.
2. Edit `identity.yaml`, `brand.yaml`, `copy/*.yaml`, `intake.yaml`, and
   `site.yaml` for your organization.
3. Set `DEPLOYMENT_ID=<your-id>` in `.env`.
4. Optionally configure connectors, redaction, discovery, and newsletter
   bundles in the same directory.
5. Provide an LLM route: either a local OpenAI-compatible endpoint via
   `LOCAL_LLM_BASE_URL`, or an `ANTHROPIC_API_KEY` / `GEMINI_CLOUD_API_KEY`
   plus the appropriate `ENABLE_*` flag.

See `.env.example` for the full list of environment variables grouped by
subsystem, and `claude.md` for routing/security/architecture conventions.

## Local setup details

Set `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000` in `web/.env.local` to point the form at the local backend during development. Leave it empty for production builds (nginx proxies `/contact` on the same host).

## Deployed web pages

| Route | Source | Notes |
|---|---|---|
| `/` | `web/src/app/page.tsx` | Primary public entry; hosts the ChatWidget terminal |
| `/terminal` | `web/src/app/terminal/page.tsx` | Guided terminal page |
| `/newsletter/subscribe` | `web/src/app/newsletter/subscribe/page.tsx` | Newsletter subscription page |
| `/privacy` | `web/src/app/privacy/page.tsx` | Public privacy policy |
| `/admin/health` | `web/src/app/admin/health/page.tsx` | Token-gated provider diagnostics and policy controls |
| `/admin/discovery` | `web/src/app/admin/discovery/page.tsx` | Token-gated discovery review UI |
| `/admin/newsletter` | `web/src/app/admin/newsletter/page.tsx` | Token-gated newsletter composer |
| `/newsletter/unsubscribe` | `api/newsletter_public.py` | Public token-driven unsubscribe |

## Health endpoints

- `/health` reports whether the FastAPI process is alive.
- `/health/providers` reports every configured model route with endpoint health,
  model-level availability, policy callability, block reasons, cloud spillover
  budget state, router circuit-breaker state, and last route-decision diagnostics.
  The route diagnostics are a best-effort snapshot of the most recent routing
  decision in the API process. Public provider health uses a brief cached snapshot;
  use the token-gated admin health endpoint to force a fresh check. Use this when
  checking whether the GPU host, tunnel, or cloud fallback is actually callable.
- `/admin/health` is an admin page for provider/model diagnostics and runtime
  policy toggles. It requires an admin policy token before reading or writing
  admin API state. Candidate routes are visible but are not health-checked or
  selected unless enabled. It is excluded from robots, but the token is the real gate.

## Discovery system

- `core/discovery_scheduler.py` runs one deterministic discovery pass over the
  static `kb/watchlist_seed.py` entries, reconciles them into `discovery_source`,
  polls due sources, and stores deduped findings in `discovery_find`.
- Source handlers currently cover GitHub repos, GitHub topics, GitHub user/org
  watches, RSS, subreddits, YouTube feeds, web diffs, and Brave-backed saved
  searches. Handlers are read-only, validate handler-specific target formats, and
  restrict arbitrary fetch targets to public `https://` hosts. They must not
  receive broad secrets.
- `core/discovery_queue.py` lists findings/source health, adds or edits watched
  sources, records review decisions with audit rows, and collapses GitHub repo
  artifact scans into one operator-facing row per top-level category. Child files
  stay attached in `raw_payload.children` for downstream indexing and citation.
- Token-gated admin API routes expose discovery operations:
  - `POST /admin/api/discovery/run-once`
  - `GET /admin/api/discovery/finds`
  - `POST /admin/api/discovery/finds/{id}/approve`
  - `POST /admin/api/discovery/finds/{id}/reject`
  - `GET /admin/api/discovery/sources`
  - `POST /admin/api/discovery/sources`
  - `PATCH /admin/api/discovery/sources/{id}`
- `/admin/discovery` is the token-gated review UI for source health, category
  rollups, inline curation decisions, and quick-add watched sources.
- `scripts/run_discovery.py` provides the same scheduler pass for operator use.
- Approving a find with `ingest_into_chroma=true` writes a reference document
  into the active Chroma collection before the find is marked ingested.
- `core/discovery_scout.py` is the bounded LLM-using scout. It is off by default,
  cost-capped, and only queues `new_source_candidate` findings. It never writes
  directly to `discovery_source` and never publishes content.
- `core/discovery_summary.py` and `core/discovery_digest.py` group large discovery
  queues into deterministic lanes, topics, prominent sources, new-source candidates,
  KB ingestion candidates, routine tracked-source updates, and suggested Updates
  and Additions picks. The digest renders these as an off-by-default newsletter
  using the existing M365 email service. `.github/workflows/discovery.yml`
  triggers the production scheduler hourly with `ADMIN_POLICY_TOKEN`; per-source
  cadence gates decide which sources are actually polled on each pass.
- Discovery rows must not store secrets. Public display, Chroma ingestion, and
  publication remain separate approval steps.
- Discovery findings now have an additive canonical `Document` projection column
  used by retrieval when present. Legacy discovery rows backfill this projection
  on first read.

## Connector framework and deployment bundles

- `packages/gestalt-connector-protocol/` defines the canonical `Document` model,
  structured body sections, connector protocol dataclasses, redaction pipeline,
  and `connector-test validate` harness.
- `docs/schemas/document.v1.json` is generated from the Pydantic `Document`
  model and checked in as the versioned schema artifact.
- `packages/gestalt-connector-fs/` is the reference filesystem/UNC connector. It
  walks a mounted/local share, extracts supported text/table formats, redacts
  sensitive content, and emits stable canonical documents.
- `deployments/<id>/` stores brand, identity, site, nav, copy, intake,
  connector, redaction, newsletter, discovery, and curriculum settings.
  `DEPLOYMENT_ID` selects the active bundle.
- `/api/deployment-config` exposes only public-safe brand/copy/nav/intake fields.
  `/admin/api/deployment-config` exposes the full parsed config behind the admin
  token.
- `/admin/api/privacy/audit.json` reports per-connector cloud-eligible versus
  local-only document counts and the rolling seven-day cloud-refused count.

## Secure provider key propagation

Provider API keys must not be typed into the public website or admin page.
`ANTHROPIC_API_KEY` is managed as a GitHub repository secret. The
development and production deploy workflows pass that secret to
`.github/scripts/deploy_vps.sh`, which writes or updates the key in the remote
service `.env` over SSH without printing it. If `/admin/health` reports
`missing_api_key`, add/update the GitHub secret and rerun the appropriate deploy.
Cloud fallback still requires server-side policy flags and budget caps; a key
alone must not allow public users to burn premium spend.

The admin page can toggle runtime cloud policy and individual model routes after
unlocking with `ADMIN_POLICY_TOKEN`. It can also switch routing strategy between
best value, prefer local, prefer cloud quality, local only, and cloud only. These
changes apply to the running API process and are intentionally reversible.
Persisted defaults still come from env and deploy configuration.

## Deployment

Preferred path is GitHub Actions, with review based on change risk:

- Reviewed deploy for review-required work: create a branch, open a non-draft
  PR, wait for CI (and any configured code review), merge to `main`, wait for
  production CI, then run `Deploy production` manually from GitHub Actions on
  `main` and type `DEPLOY`.
- Review-required work includes major feature changes, landmark/milestone
  reviews, security or public API changes, provider/router/tool changes,
  deployment changes, data model changes, cross-system changes, and anything
  with meaningful blast radius.
- Direct deploy for small isolated fixes: direct push to `main`, wait for CI,
  then manually run `Deploy production`. Use this for UI tweaks, copy changes,
  admin affordance refinements, and operational hotfixes.
- Development deploy: push or merge to `dev` to run CI and deploy development
  automatically.

For emergency manual deploys, use the same environment variables as the
workflows and run `.github/scripts/deploy_vps.sh`; do not hand-copy individual
files unless recovering from a failed deploy.

## Database

SQLite at the path configured via `APP_DATABASE_PATH` (defaults to
`database.db`). Tables: `contactrecord`, `conversation`,
`conversation_intake`, `messagerecord`, discovery source/find/audit,
`subscriber`, `subscriber_autoreply`, `newsletter_issue`,
`newsletter_delivery`, and notification/budget support tables. Role-specific
form fields are stored as JSON in `contactrecord.data`; guided terminal
intake answers are cataloged in `conversation_intake` for routing context
and analysis. Retention sweeps delete short-lived operational rows, minimize
subscriber rows on unsubscribe, and anonymize stale contact records instead
of retaining personal details indefinitely.

Approved discovery content can be published into an operator-configured
corpus repo, then indexed into the KB/Chroma delivery layer so tracked
blogs, repos, and feeds keep retrieval fresh after review. GitHub artifact
sources are reviewed as category rollups while child files remain available
to the publisher/indexer.

Backups: a nightly GitHub Actions workflow (`.github/workflows/backup.yml`)
runs `sqlite3 .backup` on the target host and stores gzipped snapshots for
the last 14 days.

Query the database directly with [DB Browser for SQLite](https://sqlitebrowser.org/)
or over SSH:
```bash
ssh "$VPS_USER@$VPS_HOST" "python3 -c \"
import sqlite3, json
conn = sqlite3.connect('database.db')
for r in conn.execute('SELECT created_at, role, name, email, data FROM contactrecord ORDER BY created_at DESC'):
    print(r[:4])
    print(' ', json.loads(r[4]))
\""
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | No | Comma-separated browser origins allowed by FastAPI. Defaults to `http://localhost:3000`. |
| `LOCAL_LLM_PROFILE` | No | Profile name from `llm/profiles.json` for the primary provider. |
| `LOCAL_LLM_PROVIDER` | No | Primary provider type when no profile is set: `llama_cpp` or `ollama`. |
| `LOCAL_LLM_BASE_URL` | No | llama.cpp OpenAI-compatible endpoint. Defaults to `http://localhost:8080/v1`. |
| `LOCAL_LLM_MODEL` | No | Local model name passed to the OpenAI-compatible endpoint. |
| `LOCAL_LLM_START_COMMAND` | No | Admin-only local runtime start command executed by the VPS API process. Usually an SSH command over WireGuard to the GPU host. Supports `{action}` and `{route}` template variables. |
| `LOCAL_LLM_STOP_COMMAND` | No | Admin-only local runtime stop command executed by the VPS API process. Usually an SSH command over WireGuard to the GPU host. Supports `{action}` and `{route}` template variables. |
| `LOCAL_LLM_CONTROL_TIMEOUT_SECONDS` | No | Timeout for local runtime control commands. Defaults to `20`. |
| `LOCAL_LLM_CONTROL_CWD` | No | Optional working directory for local runtime control commands. |
| `OLLAMA_BASE_URL` | No | Ollama endpoint when `LOCAL_LLM_PROVIDER=ollama`. |
| `LLM_PROFILES_PATH` | No | Override path for model profile JSON. |
| `LLM_PROFILES_STRICT` | No | Set to `1` to fail startup when profile JSON/schema loading fails. Defaults to logging the error and continuing with no loaded profile routes. |
| `GEMINI_CLOUD_BASE_URL` | No | Google AI Studio OpenAI-compatible cloud base URL for the Gemini cloud route. Production deploy sets `https://generativelanguage.googleapis.com/v1beta/openai`. |
| `GEMINI_CLOUD_API_KEY` | No | Server-side API key for the Gemini cloud route. The production workflow maps GitHub secret `GEMINI_CLOUD_API_KEY` into this app env var. Never enter provider keys in the browser/admin page. |
| `GEMINI_CLOUD_MODEL` | No | Optional model ID override for the Gemini cloud route. Defaults to the profile model. |
| `ROUTING_STRATEGY` | No | Runtime default for model route ordering: `best_value`, `prefer_local`, `prefer_cloud_quality`, `local_only`, or `cloud_only`. Defaults to `best_value`. The admin page can override this until restart/deploy. |
| `KB_FALLBACK_SEARCH_URL` | No | Approved online KB fallback endpoint. Used only when local KB retrieval has no usable context or the local Chroma path errors. The API is called server-side with query, tool, and limit parameters. |
| `ADMIN_POLICY_TOKEN` | Recommended | Token required by `/admin/api/*` to read admin health and change runtime model/cloud policy. Local development allows `local-dev-admin` only when unset and called through localhost. |
| `ENABLE_CLAUDE_FALLBACK` | No | Set to `1` only when the operator enables Claude fallback. Defaults off. |
| `ENABLE_CLOUD_SPILLOVER` | No | Set to `1` only when the operator explicitly allows paid cloud spillover. Defaults off. When enabled, budget counters are persisted in SQLite. |
| `ENABLE_LOW_COST_CLOUD` | No | Reserved low-cost cloud gate. Also requires `ENABLE_CLOUD_SPILLOVER=1`. Defaults off. |
| `CLOUD_SPILLOVER_MAX_CALLS_PER_TURN` | No | Maximum cloud fallback calls allowed per turn. Approved production starting cap: `1`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION` | No | Maximum cloud fallback calls allowed per conversation/session. Approved production starting cap: `10`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_CALLS_PER_DAY` | No | Maximum cloud fallback calls allowed per persisted UTC day. Approved production starting cap: `20`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_CALLS_PER_MONTH` | No | Maximum cloud fallback calls allowed per persisted UTC month. Approved production starting cap: `500`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_DAILY_USD` | No | Maximum estimated cloud spend per UTC day. Approved production starting cap: `5`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_MONTHLY_USD` | No | Maximum estimated cloud spend per UTC month. Approved production starting cap: `50`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL` | No | Maximum estimated input tokens per cloud fallback call. Approved production starting cap: `16000`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL` | No | Maximum requested output tokens per cloud fallback call. Approved production starting cap: `2048`. Defaults to `0`, which blocks cloud calls. |
| `CLOUD_SPILLOVER_INPUT_PRICE_USD_PER_MILLION` | No | Input token pricing assumption for estimated spend. Defaults to Sonnet assumption `3`. |
| `CLOUD_SPILLOVER_OUTPUT_PRICE_USD_PER_MILLION` | No | Output token pricing assumption for estimated spend. Defaults to Sonnet assumption `15`. |
| `CLOUD_SPILLOVER_DB_PATH` | No | SQLite file for persisted cloud spillover counters when spillover is enabled. Defaults to `database.db`. |
| `APP_DATABASE_PATH` | No | SQLite file path for the main app store. Defaults to `database.db`. Ignored when `APP_DATABASE_URL` is set. |
| `APP_DATABASE_URL` | No | Full SQLAlchemy async database URL for the main app store. Defaults to `sqlite+aiosqlite:///database.db`. |
| `RETENTION_CHAT_DAYS` | No | Days to retain chat conversations, messages, intake snapshots, and token usage. Defaults to progressive privacy value `30`; `0` disables this sweep. |
| `RETENTION_TERMINAL_INTAKE_DAYS` | No | Days to retain unlinked terminal intake submissions. Defaults to `30`; linked records follow chat/contact lifecycle. |
| `RETENTION_CONTACT_NOTIFICATION_DAYS` | No | Days to retain contact notification delivery logs. Defaults to `30`. |
| `RETENTION_SUBSCRIBER_AUTOREPLY_DAYS` | No | Days to retain per-contact subscriber auto-reply audit rows. Defaults to `30`. |
| `RETENTION_NEWSLETTER_DELIVERY_DAYS` | No | Days to retain per-subscriber newsletter delivery rows. Defaults to `90`; public web/LinkedIn issue delivery rows are not subscriber-specific and are retained. |
| `RETENTION_CONTACT_RECORD_DAYS` | No | Days before stale contact records are anonymized. Defaults to `730` (24 months); `0` disables anonymization. |
| `APP_GITHUB_TOKEN` | Recommended for discovery | Optional server-side GitHub read token for discovery handlers. Keeps repo/artifact rescans out of anonymous API rate limits. Never enters browser or model context. |
| `LIBRARY_PUBLISHER_GITHUB_APP_ID` | Required for corpus publishing | GitHub App ID for the corpus publisher identity. Used to mint short-lived installation tokens. |
| `LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID` | Required for corpus publishing | GitHub App installation ID for the app installed only on the target corpus repo. |
| `LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64` | Required for corpus publishing | Single-line, unwrapped base64 of the corpus publisher GitHub App private key PEM. Never enters browser or model context. |
| `LIBRARY_PUBLISHER_REPO` | No | Target repo (`owner/name`) for discovery corpus publishing. Operator-configured per deployment. |
| `LIBRARY_PUBLISHER_BASE_BRANCH` | No | Target branch for corpus publishing. Defaults to `main`. |
| `ANTHROPIC_API_KEY` | Only with Claude fallback | Anthropic key for the secondary provider. |
| `CLAUDE_PROFILE` | No | Claude profile name from `llm/profiles.json`. |
| `CLAUDE_MODEL` | No | Claude model name when no Claude profile is set. |
| `MS365_TENANT_ID` | No | Azure AD tenant for Graph mail. |
| `MS365_CLIENT_ID` | No | Azure app client ID for Graph mail. |
| `MS365_CLIENT_SECRET` | No | Azure app client secret for Graph mail. |
| `MS365_SEND_AS` | No | Sender address for outbound mail. Operator-configured per deployment. |
| `DISCOVERY_DIGEST_ENABLED` | No | Enables the discovery newsletter email after admin/scheduled discovery runs. Defaults off. |
| `DISCOVERY_DIGEST_RECIPIENT` | No | Optional newsletter recipient override. Defaults to the internal email recipient. Legacy `DISCOVERY_DIGEST_TO` is still read as a migration shim. |
| `DISCOVERY_DIGEST_MAX_ITEMS` | No | Number of recent findings to summarize in the discovery newsletter. Defaults to `100`. |
| `NEWSLETTER_APPROVAL_TO` | No | Comma-separated email addresses that receive the "draft awaiting approval" notification from the newsletter scheduler. Operator-configured per deployment. |
| `DISCORD_INVITE_URL` | No | Optional community invite link used in auto-replies. Set per deployment before sending public auto-replies that reference it. |
| `SERVICE_BOOKING_URL` | No | Scheduling URL used in the service-inquiry auto-reply on the contact form. Set per deployment before sending public service-inquiry auto-replies. |
| `SITE_PUBLIC_URL` | No | Site root used to build unsubscribe and admin-newsletter links in outbound email. Operator-configured per deployment. |
| `LINKEDIN_CLIENT_ID` | No | LinkedIn Developer app client id. Newsletter auto-post is dark-by-default; populate this plus `_CLIENT_SECRET`, `_REFRESH_TOKEN`, `_AUTHOR_URN` to enable it. Until enabled, the admin panel's "Copy for LinkedIn" button is the working path. |
| `LINKEDIN_CLIENT_SECRET` | No | LinkedIn Developer app client secret. |
| `LINKEDIN_REFRESH_TOKEN` | No | Long-lived refresh token from the LinkedIn OAuth flow with `w_member_social` scope. |
| `LINKEDIN_AUTHOR_URN` | No | Author URN for posts, e.g. `urn:li:person:abc123` (personal) or `urn:li:organization:456` (company page). |

## Newsletter pipeline

The newsletter cycle runs end to end as follows.

1. **Curation** in `/admin/discovery`. Mark sources as *Strong signal* and use
   per-find actions: **Feature in ticker** (30-day decay), **Send to next
   newsletter** (one-time inclusion), or **Dismiss**.
2. **Compose** via `/admin/newsletter` or the scheduler workflow. The
   composer snapshots every find with `newsletter_pending=True` into a
   `NewsletterIssue` with status `awaiting_approval`.
3. **Approval email**. The scheduler (workflow: `.github/workflows/newsletter.yml`,
   calls `POST /admin/api/newsletter/run-cycle`) composes a new draft only
   when the configured cycle has elapsed AND no `awaiting_approval` issue is
   already pending. When it does compose, it emails the addresses in
   `NEWSLETTER_APPROVAL_TO` with a link to `/admin/newsletter`.
4. **Editorial + approve**. Operator opens `/admin/newsletter`, edits the
   editorial markdown intro (subset: `**bold**`, `*italic*`, `[text](url)`,
   blank-line paragraphs) and the subject, previews the render targets
   (HTML, plain text, LinkedIn post), and clicks *Approve & distribute*.
5. **Distribution**. On approval the backend:
   - Sends one M365 Graph email to every active `Subscriber`, with their
     personalized `unsubscribe_token` in the footer
   - Writes a `NewsletterDelivery` row per channel + subscriber
   - Calls `core.linkedin.post_to_linkedin(rendered_linkedin_post)`; if
     LinkedIn env vars are unset, this returns `skipped` and the delivery row
     records `status="skipped"` with reason `not_configured`
   - Flips `newsletter_pending=False` on every find included in the issue

### Enabling LinkedIn auto-post

LinkedIn auto-post is **dark by default**. Until the four env vars below are
populated on the host and the service is restarted, every approved issue
records a `linkedin` delivery with `status=skipped`, and `/admin/newsletter`'s
*Copy for LinkedIn* button is the operating mechanism.

To enable real auto-post:

1. Register a LinkedIn Developer app at https://developer.linkedin.com/.
2. Request access to the **Marketing Developer Platform** for the
   `w_member_social` scope. Personal accounts post as
   `urn:li:person:<member_id>`; company pages post as
   `urn:li:organization:<page_id>`.
3. Run the OAuth flow once to obtain a refresh token with `w_member_social`.
4. Populate `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`,
   `LINKEDIN_REFRESH_TOKEN`, and `LINKEDIN_AUTHOR_URN` in the service `.env`
   on the host, then restart the API service.
5. Approve the next newsletter issue from `/admin/newsletter`. The
   `NewsletterDelivery` row for the `linkedin` channel will move from
   `skipped` to `sent` with the post URN recorded, or to `failed` with the
   LinkedIn API error captured.

The poster targets `https://api.linkedin.com/rest/posts` with a versioned API
surface (`LinkedIn-Version`). Update the constant in `core/linkedin.py` if
LinkedIn rolls the API version.

## Tests

```bash
uv run pytest
```

## Licensing

This repository is dual-licensed. The split is documented in
[NOTICE](NOTICE):

- **Framework** — everything outside `packages/` — is distributed under the
  [Functional Source License, Version 1.1, ALv2 Future License](LICENSE)
  (FSL-1.1-ALv2). You can read, run, modify, and redistribute the source
  for any Permitted Purpose, including internal use, non-commercial
  education and research, and professional services delivered to a
  licensee using the Software in compliance with the FSL. Each version
  automatically converts to Apache License 2.0 two years after it is
  published. Hosting Gestalt Workframe (or a fork) as a managed/SaaS
  offering, reselling it, or otherwise offering the same or substantially
  similar functionality to third parties is a Competing Use and requires a
  commercial license from Gestalt Works. See [COMMERCIAL.md](COMMERCIAL.md)
  for the commercial-use policy and implementation-services details.
- **Connector protocol and reference connectors** under `packages/` are
  distributed under the [Apache License, Version 2.0](packages/LICENSE) so
  that integrators can write and ship their own connectors without
  worrying about FSL compatibility. Each package declares Apache-2.0 in
  its own `pyproject.toml` and source headers.
