# Public Terminal Threat Model

The enforceable security hard rules are in the root `claude.md`. This file is the
reference threat model behind them.

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
