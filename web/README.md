# web

Next.js frontend. Builds to a static export. No server-side rendering — all LLM and API logic lives in the Python backend.

## Dev

```bash
pnpm install
pnpm dev        # http://localhost:3000
```

Set `NEXT_PUBLIC_API_URL=http://127.0.0.1:8000` in **`.env.development.local`** (not `.env.local`) to route fetches to a local backend during `pnpm dev`. Leave it empty (or unset) for production — a reverse proxy serves the API on the same host and the frontend uses relative URLs.

> ⚠️ **`.env.local` leaks into production builds.** Next.js reads `.env.local` during `next build` as well as `next dev`, and `NEXT_PUBLIC_*` vars are **inlined as literal strings into the static export at build time**. Always use `.env.development.local` for dev-only overrides.

## Pages

| Route | File | Description |
|---|---|---|
| `/` | `src/app/page.tsx` | Static-export landing page |
| `/terminal` | `src/app/terminal/page.tsx` | Guided terminal |
| `/newsletter/subscribe` | `src/app/newsletter/subscribe/page.tsx` | Newsletter subscription page |
| `/privacy` | `src/app/privacy/page.tsx` | Privacy policy |
| `/admin/health` | `src/app/admin/health/page.tsx` | Token-gated provider/policy health panel |
| `/admin/discovery` | `src/app/admin/discovery/page.tsx` | Token-gated discovery review and curation panel |
| `/admin/newsletter` | `src/app/admin/newsletter/page.tsx` | Token-gated newsletter composer and approval panel |

## Key components

| File | Description |
|---|---|
| `src/components/ChatWidget.tsx` | Terminal UI, guided intake capture, provider-health checks, and SSE stream rendering |
| `src/components/NewsletterSignupForm.tsx` | Lightweight newsletter signup form |
