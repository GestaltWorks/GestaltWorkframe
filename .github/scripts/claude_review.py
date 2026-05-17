import json
import fnmatch
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

MAX_REVIEW_CHARS = 120_000
# Must match Anthropic's model ID and the default in .github/workflows/claude-review.yml.
DEFAULT_CLAUDE_REVIEW_MODEL = "claude-sonnet-4-6"
DEFAULT_INPUT_PRICE_USD_PER_MILLION = 3.0
DEFAULT_OUTPUT_PRICE_USD_PER_MILLION = 15.0
DEFAULT_CACHE_CREATION_INPUT_PRICE_MULTIPLIER = 1.25
DEFAULT_CACHE_READ_INPUT_PRICE_MULTIPLIER = 0.10
ALLOW_PATTERNS = [
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx", "*.md", "*.yml", "*.yaml",
    "*.toml", "*.json", "*.ps1", "web/**/*.ts", "web/**/*.tsx", ".github/**/*.yml",
]
PRIORITY_PATTERNS = [
    "api/main.py", "llm/*.ps1", "llm/profiles.json",
    ".github/workflows/*.yml", ".github/scripts/*.py",
    "tests/test_api_main.py", "claude.md", "README.md", "objectives.md",
]
EXCLUDE_PATTERNS = [
    "uv.lock", "web/pnpm-lock.yaml", "kb/chroma_db/**",
]
SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|client[_-]?secret)\s*[:=]\s*[^\s'\"]+"), r"\1=<REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk-<REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<REDACTED>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<REDACTED_PRIVATE_KEY>"),
]


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8", errors="replace")


def base_head() -> tuple[str, str]:
    base = os.getenv("BASE_SHA") or ""
    head = os.getenv("HEAD_SHA") or ""
    if not base:
        base = run_git(["rev-parse", "HEAD~1"]).strip()
    if not head:
        head = run_git(["rev-parse", "HEAD"]).strip()
    return base, head


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def include_file(path: str) -> bool:
    allowed = any(fnmatch.fnmatch(path, pattern) for pattern in ALLOW_PATTERNS)
    excluded = any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDE_PATTERNS)
    return allowed and not excluded


def priority_rank(path: str) -> tuple[int, str]:
    for index, pattern in enumerate(PRIORITY_PATTERNS):
        if fnmatch.fnmatch(path, pattern):
            return index, path
    return len(PRIORITY_PATTERNS), path


def pr_diff() -> str:
    pr_number = os.getenv("PR_NUMBER") or ""
    repo = os.getenv("GITHUB_REPOSITORY") or ""
    token = os.getenv("GH_TOKEN") or ""
    if not pr_number or not repo:
        return ""

    patches = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
            headers={
                "accept": "application/vnd.github+json",
                "authorization": f"Bearer {token}",
                "x-github-api-version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            files = json.loads(response.read().decode("utf-8"))
        if not files:
            break
        for file_info in files:
            filename = file_info.get("filename", "")
            patch = file_info.get("patch", "")
            if filename and patch and include_file(filename):
                patches.append(f"diff --git a/{filename} b/{filename}\n{patch}")
        page += 1
    return "\n".join(patches)


def diff_text(base: str, head: str) -> str:
    diff = pr_diff()
    if not diff:
        pathspecs = [*ALLOW_PATTERNS, *[f":!{pattern}" for pattern in EXCLUDE_PATTERNS]]
        diff = run_git(["diff", "--unified=80", base, head, "--", *pathspecs])
    return redact(diff)[:MAX_REVIEW_CHARS]


def codebase_snapshot() -> str:
    paths = sorted((path for path in run_git(["ls-files"]).splitlines() if include_file(path)), key=priority_rank)
    sections = []
    total = 0
    manifest = redact("--- REVIEW SNAPSHOT ORDER ---\n" + "\n".join(paths) + "\n")
    sections.append(manifest[: min(len(manifest), 8_000)])
    total += len(sections[-1])
    for path in paths:
        try:
            content = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        section = redact(f"--- FILE: {path} ---\n{content}\n")
        if total + len(section) > MAX_REVIEW_CHARS:
            remaining = MAX_REVIEW_CHARS - total
            if remaining > 200:
                sections.append(section[:remaining] + "\n--- SNAPSHOT TRUNCATED ---\n")
            break
        sections.append(section)
        total += len(section)
    return "\n".join(sections)


def write_review(text: str) -> None:
    with open("claude-review.md", "w", encoding="utf-8") as handle:
        handle.write(text.strip() + "\n")


def price_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def usage_summary(model: str, usage: dict[str, int] | None) -> str:
    if not usage:
        return ""

    # Per Anthropic's response schema, input_tokens is non-cached input;
    # cache creation and cache read tokens are billed separately.
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
    input_price = price_env("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", DEFAULT_INPUT_PRICE_USD_PER_MILLION)
    output_price = price_env("CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION", DEFAULT_OUTPUT_PRICE_USD_PER_MILLION)
    cache_creation_multiplier = price_env(
        "CLAUDE_REVIEW_CACHE_CREATION_INPUT_PRICE_MULTIPLIER",
        DEFAULT_CACHE_CREATION_INPUT_PRICE_MULTIPLIER,
    )
    cache_read_multiplier = price_env(
        "CLAUDE_REVIEW_CACHE_READ_INPUT_PRICE_MULTIPLIER",
        DEFAULT_CACHE_READ_INPUT_PRICE_MULTIPLIER,
    )
    estimated_cost = (
        (input_tokens * input_price)
        + (cache_creation_tokens * input_price * cache_creation_multiplier)
        + (cache_read_tokens * input_price * cache_read_multiplier)
        + (output_tokens * output_price)
    ) / 1_000_000

    lines = [
        "## Claude Review Usage",
        "",
        f"- Model: `{model}`",
        f"- Input tokens: `{input_tokens}`",
        f"- Output tokens: `{output_tokens}`",
    ]
    if cache_creation_tokens or cache_read_tokens:
        lines.extend([
            f"- Cache creation input tokens: `{cache_creation_tokens}`",
            f"- Cache read input tokens: `{cache_read_tokens}`",
        ])
    lines.extend([
        f"- Approximate estimated cost: `${estimated_cost:.6f}`",
        f"- Pricing assumption: `${input_price:g}/M input`, `${output_price:g}/M output`, "
        f"`{cache_creation_multiplier:g}x` cache creation, `{cache_read_multiplier:g}x` cache read",
        "- Anthropic billing is the source of truth.",
    ])
    if model != DEFAULT_CLAUDE_REVIEW_MODEL:
        lines.append(
            f"- Pricing defaults are for `{DEFAULT_CLAUDE_REVIEW_MODEL}`; override pricing env vars if needed."
        )
    return "\n".join(lines)


def call_claude(review_text: str, review_scope: str = "diff") -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return "## Claude Code Review\n\nSkipped: `ANTHROPIC_API_KEY` is not configured."

    if review_scope == "full":
        prompt = (
            "You are reviewing a production-bound full codebase snapshot. "
            "The snapshot is size-limited and begins with a file-order manifest. "
            "Focus on correctness, security, secret handling, deployment risk, tests, "
            "architecture drift, routing/provider correctness, frontend/backend contract mismatches, "
            "and maintainability. Do not ask for or reveal secrets. If a value appears redacted, "
            "treat that as intentional. Prioritize concrete findings over generic advice.\n\n"
            f"Codebase snapshot:\n```text\n{review_text}\n```"
        )
    else:
        prompt = (
            "You are reviewing a production-bound diff. "
            "Focus on correctness, security, secret handling, deployment risk, tests, "
            "and maintainability. Do not ask for or reveal secrets. If a value appears "
            "redacted, treat that as intentional. Be concise and specific.\n\n"
            f"Diff:\n```diff\n{review_text}\n```"
        )
    model = os.getenv("CLAUDE_REVIEW_MODEL", DEFAULT_CLAUDE_REVIEW_MODEL)
    payload = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        return f"## Claude Code Review\n\nClaude API call failed: HTTP {exc.code}.\n\n```text\n{detail}\n```"

    content = body.get("content", [])
    text = content[0].get("text", "") if content else ""
    parts = ["## Claude Code Review\n\n" + (text or "No review text returned.")]
    usage = usage_summary(model, body.get("usage"))
    if usage:
        parts.append(usage)
    return "\n\n".join(parts)


def main() -> int:
    review_scope = os.getenv("REVIEW_SCOPE", "diff").strip().lower()
    if review_scope == "full":
        review_text = codebase_snapshot()
    else:
        base, head = base_head()
        review_text = diff_text(base, head)
    if not review_text.strip():
        write_review("## Claude Code Review\n\nSkipped: no reviewable diff.")
        return 0
    write_review(call_claude(review_text, review_scope=review_scope))
    return 0


if __name__ == "__main__":
    sys.exit(main())
