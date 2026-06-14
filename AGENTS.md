<!-- AUTO-SYNCED from the LLM Builder Kit. Do not edit here; edit the kit source and re-run sync-standards.ps1. -->

# AGENTS.md — EGI Agent Behavior Contract

This file governs how all AI agents (Cline, Claude, Devin, Cursor) operate in EGI repos.
Read this before writing a single line of code. Follow it without exception.

---

## Before You Start

1. **Read the operator profile.** `operator-profile.md` (in this kit's `instructions/`)
   tells you who you're working with, their communication preferences, what they want you
   to decide vs escalate, and their hard lines. Apply it. When it conflicts with a hard
   rule here or in `coding-rules.md`, the rule wins — and you flag the conflict.

2. **Read the task brief.** Your task brief is the authoritative source of scope:
   - `routing.tier` and `routing.approved` — if tier is `premium` and approved is `false`,
     write a plan only (see Plan-Only Mode below). Do not execute.
   - `targetFiles` — your scope. Stay inside it.
   - `definitionOfDone` — the exit condition. When this is met, you're done.
   - `webhookUrl` — POST status updates here throughout the task.

3. **Read the codebase, not just this document.** Use `list_files` and `read_file` to
   understand what currently exists before writing anything. Check package versions,
   read existing implementations, check for deprecation warnings in local docs.

4. **Follow the repo standards.** `docs/standards/coding-rules.md` is the law.
   If it conflicts with your training data, the file wins.

---

## Communication Rules

**You can ask in chat.** The operator gets push notifications from the panel and will see it.
Use chat for direct, time-sensitive questions where you need a response to unblock a specific
step. Keep it one question, one specific ask.

For things that need async tracking, approval workflow, or a record — POST to `/api/decisions`
instead. Decisions are persistent, logged, and actionable from the panel. Chat is ephemeral.

**Use judgment:**
- Quick clarification to unblock work right now → ask in chat
- Approval needed before proceeding with a significant action → POST to `/api/decisions`
- Something is broken and you can't continue → POST blocked status + ask in chat
- Architectural direction or preference choice → POST to `/api/decisions` (needs a record)

If you can make a reasonable assumption without asking, make it, document it in the task
status update, and proceed. Do not ask permission to make sensible decisions.

---

## Status Updates

POST to `webhookUrl` (from task brief) at each of these points:

```json
{ "status": "in-progress", "summary": "Starting: [first step description]", "filesChanged": [] }
{ "status": "in-progress", "summary": "Completed: [step]. Starting: [next step]", "filesChanged": ["file.ts"] }
{ "status": "done", "summary": "Task complete. [brief summary of what was done]", "filesChanged": ["all", "modified", "files"] }
{ "status": "blocked", "summary": "Blocked on: [specific issue]. Decision posted. Continuing with [other scope].", "filesChanged": [] }
```

If `webhookUrl` is not in your task brief, skip status posting and proceed.

---

## Commit Discipline

- **One concern per commit.** Not "misc changes" — one change, one commit message that says what it does.
- **Stay in your targetFiles.** Do not modify files outside your task's scope without explicit justification in your commit message.
- **If you find a bug outside your scope:** document it in the task status, do not fix it. Scope creep breaks other agents.
- **Commit messages:** imperative tense, specific. "Add JWT refresh rotation to auth service" not "fix auth".

---

## Handling Shared Files / Agent Conflicts

Before editing any file, check if another agent is likely working in the same area:
- If untracked changes exist in files outside your scope: POST a conflict decision immediately.
  "Untracked changes detected in [files]. Another agent may be working here. Proceeding
  with my scope only." Then proceed with only your targetFiles.
- Do not overwrite or revert changes you did not make.
- Do not squash or rebase unless explicitly told to.

---

## Merge and Ship Policy

**The default is to ship.** Once your PR is open and CI runs:

**You may expect AUTO-MERGE when ALL of:**
- CI is fully passing (no relevant skipped checks)
- Claude code review returns `autoMerge: true`
- No breaking changes detected
- No security flags raised
- PR scope is atomic (single task, single concern)

**The PR will be escalated to the operator when ANY of:**
- Breaking change in prod behavior detected
- Any security concern raised (even minor)
- Claude review returns `needs-operator`
- Task type is `architecture-decision`
- PR touches: auth, secrets, encryption, data migrations, payments, external API integrations

**As the agent, your job is:**
- Open clean, atomic PRs
- Write clear PR descriptions: what changed, why, how to verify
- Do not open a PR that you know will fail auto-merge unless the escalation is intentional
- If your task had multiple concerns, open multiple PRs — one per atomic change

---

## Plan-Only Mode

When `routing.approved: false` in your task brief, credits were insufficient to run the
required premium model at classification time. The system automatically switched you to
plan mode. This is not a human decision — it is automatic resource management.

1. **DO NOT execute.** Write a plan only.
2. Your plan must include: every file to touch, every step in order, expected outputs,
   edge cases, risks, and a confidence statement.
3. POST status `plan-ready` to the webhook.
4. Stop.

When credits are replenished, the system will notify the operator with options:
- Approve plan and switch to Act (execute what you planned)
- Review plan with premium model first
- Re-run classification and execute directly

You will be re-queued with `routing.approved: true`. At that point, pick up from the plan
you already wrote — do not re-plan, execute.

---

## Security and Secrets

- Never hardcode secrets, API keys, tokens, or credentials anywhere in source.
- Never log secrets, even in debug output.
- Never commit `.env` files or anything with real credentials.
- Use environment variables. Use the secrets store (`lib/secrets.ts` if it exists in this repo).
- If you need a new secret, add the variable name to `.env.example` with a placeholder and note it in your task status.

---

## Standards Reference

These files are vendored into each repo (synced from the builder kit) and are
authoritative for that repo. CI and collaborators rely on the in-repo copies:

| File | What it governs |
|------|----------------|
| `docs/standards/coding-rules.md` | Code style, patterns, what's allowed |
| `docs/standards/secrets.md` | Secret handling, rotation, storage |
| `docs/standards/model-routing-policy.md` | When to use which model, escalation rules |

Operator-global standards load from the builder kit via Cline's global rules folder
(`Documents\Cline\Rules`), so they apply on this machine without per-repo copies:
`writing-style.md`, `operator-profile.md`, `prompt-builder.md`, and the index `claude-rules.md`.

**When writing any markdown, docs, PR descriptions, task briefs, or comments:**
State what IS. Show the behavior rather than describe it. Use active voice and present
tense. State constraints as positive rules. "Use Y" not "avoid X". See `writing-style.md`
in the kit's `reference/`.

If a `docs/standards/` file is missing in a checkout, pull the canonical version from the
builder kit. Use relative paths inside repos.

---

## Framework and Dependency Handling

This project may use framework versions newer than your training data.

- Check `package.json`, `pyproject.toml`, or `go.mod` for actual versions before assuming APIs.
- Read local docs in the repo before using any framework-specific pattern.
- If you see a deprecation warning, fix it — don't suppress it.
- Do not add dependencies without explicit justification.

---

## Definition of Done

A task is done when:
1. All `definitionOfDone` criteria in the task brief are met
2. Lint passes
3. All relevant tests pass (add tests if none exist for new behavior)
4. PR is open with a clear description
5. Final status POSTed to `webhookUrl`

Do not mark done if tests are failing. Do not mark done if you skipped lint. Do not mark done
if you haven't verified the changes work as described.

