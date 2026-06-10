<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->

# Secure Workflow and Secret Locations

This file records where secrets should live. It must never contain secret values.

## Local development

- `.env` and `.env.local` stay on the developer machine and are ignored by Git.
- Commit `.env.example` with names, descriptions, and safe placeholders only.
- Use OS/user-level secret stores for long-lived personal credentials when possible.
- Do not paste secret values into LLM chats, issue comments, docs, screenshots, logs, or test output.

## GitHub

Use GitHub Actions secrets and environments for deployment credentials.

Typical names:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_KEY`
- `MODULE_DEPLOY_KEY`
- provider keys such as `ANTHROPIC_API_KEY`
- app secrets such as `JWT_SECRET`, `SESSION_SECRET`, `DATABASE_URL`

Rules:

- environment-specific secrets belong to GitHub Environments;
- deploy keys are scoped to the one repo/module that needs them;
- rotate secrets after suspected exposure;
- never print secrets during workflow debugging.

## SSH

- Personal SSH keys stay under the user profile `.ssh` directory with normal OS permissions.
- Deploy keys are stored as GitHub Actions secrets or on the target server, not in repos.
- Known hosts are pinned or populated during CI with care.
- Do not give LLM agents raw private keys.

## VPS/server

- Runtime env files live on the server with restricted permissions.
- Services load env through systemd, Docker secrets, or a protected env file.
- Backups must not be publicly reachable.
- Logs should not include tokens, auth headers, cookies, or sensitive request bodies.

## Agent/tool access

- Prefer brokered tools that perform one safe action over broad shell access.
- Scope tokens to the exact repo/service/action.
- Use short-lived credentials where available.
- Redact before returning tool output to model context.

