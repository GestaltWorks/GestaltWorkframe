import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / ".github" / "scripts" / "claude_review.py"
    spec = importlib.util.spec_from_file_location("claude_review", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_redact_removes_common_secret_shapes():
    review = _module()
    text = "ANTHROPIC_API_KEY=sk-ant-1234567890abcdef1234567890 token=abc123"

    redacted = review.redact(text)

    assert "1234567890abcdef" not in redacted
    assert "abc123" not in redacted
    assert "<REDACTED>" in redacted


def test_include_file_skips_locks_and_generated_paths():
    review = _module()

    assert review.include_file("core/router.py") is True
    assert review.include_file("web/app/page.tsx") is True
    assert review.include_file("web/pnpm-lock.yaml") is False
    assert review.include_file("kb/chroma_db/index.bin") is False


def test_missing_anthropic_key_skips_without_failure(monkeypatch):
    review = _module()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = review.call_claude("diff --git a/core/router.py b/core/router.py")

    assert "Skipped" in result
    assert "ANTHROPIC_API_KEY" in result


def test_call_claude_appends_usage_summary(monkeypatch):
    review = _module()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("CLAUDE_REVIEW_MODEL", raising=False)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"content":[{"text":"Looks good."}],"usage":{"input_tokens":1000,"output_tokens":100}}'

    def fake_urlopen(request, timeout):
        assert timeout == 60
        assert review.DEFAULT_CLAUDE_REVIEW_MODEL.encode("utf-8") in request.data
        return Response()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)

    result = review.call_claude("diff --git a/core/router.py b/core/router.py")

    assert "Looks good." in result
    assert "Claude Review Usage" in result
    assert "Approximate estimated cost: `$0.004500`" in result


def test_call_claude_uses_full_codebase_prompt(monkeypatch):
    review = _module()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b'{"content":[{"text":"Full review."}],"usage":{"input_tokens":1000,"output_tokens":100}}'

    def fake_urlopen(request, timeout):
        body = request.data.decode("utf-8")
        assert "full codebase snapshot" in body
        assert "Codebase snapshot" in body
        return Response()

    monkeypatch.setattr(review.urllib.request, "urlopen", fake_urlopen)

    result = review.call_claude("--- FILE: core/router.py ---", review_scope="full")

    assert "Full review." in result


def test_codebase_snapshot_includes_reviewable_files(monkeypatch, tmp_path):
    review = _module()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "router.py").write_text("token=secret-value\nprint('ok')\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review, "run_git", lambda args: "core/router.py\nuv.lock\n")

    snapshot = review.codebase_snapshot()

    assert "--- FILE: core/router.py ---" in snapshot
    assert "secret-value" not in snapshot
    assert "uv.lock" not in snapshot


def test_codebase_snapshot_prioritizes_command_control_files(monkeypatch, tmp_path):
    review = _module()
    for path in ["core/router.py", "gestaltworkframe/api/main.py", "llm/control_local_model.ps1", "tests/test_api_main.py"]:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        review,
        "run_git",
        lambda args: "core/router.py\ngestaltworkframe/api/main.py\nllm/control_local_model.ps1\ntests/test_api_main.py\n",
    )

    snapshot = review.codebase_snapshot()

    assert snapshot.index("--- FILE: gestaltworkframe/api/main.py ---") < snapshot.index("--- FILE: core/router.py ---")
    assert snapshot.index("--- FILE: llm/control_local_model.ps1 ---") < snapshot.index("--- FILE: core/router.py ---")


def test_usage_summary_estimates_cost(monkeypatch):
    review = _module()
    monkeypatch.delenv("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", raising=False)
    monkeypatch.delenv("CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION", raising=False)
    monkeypatch.delenv("CLAUDE_REVIEW_CACHE_CREATION_INPUT_PRICE_MULTIPLIER", raising=False)
    monkeypatch.delenv("CLAUDE_REVIEW_CACHE_READ_INPUT_PRICE_MULTIPLIER", raising=False)

    result = review.usage_summary(review.DEFAULT_CLAUDE_REVIEW_MODEL, {"input_tokens": 1000, "output_tokens": 100})

    assert "Claude Review Usage" in result
    assert "- Input tokens: `1000`" in result
    assert "- Output tokens: `100`" in result
    assert "Approximate estimated cost: `$0.004500`" in result
    assert "Anthropic billing is the source of truth" in result


def test_usage_summary_prices_cache_tokens_separately(monkeypatch):
    review = _module()
    monkeypatch.setenv("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", "3")
    monkeypatch.setenv("CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION", "15")
    monkeypatch.setenv("CLAUDE_REVIEW_CACHE_CREATION_INPUT_PRICE_MULTIPLIER", "1.25")
    monkeypatch.setenv("CLAUDE_REVIEW_CACHE_READ_INPUT_PRICE_MULTIPLIER", "0.1")

    result = review.usage_summary(
        review.DEFAULT_CLAUDE_REVIEW_MODEL,
        {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 1000,
            "output_tokens": 100,
        },
    )

    assert "Cache creation input tokens: `1000`" in result
    assert "Cache read input tokens: `1000`" in result
    assert "Approximate estimated cost: `$0.008550`" in result


def test_usage_summary_warns_for_non_default_model(monkeypatch):
    review = _module()
    monkeypatch.delenv("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", raising=False)
    monkeypatch.delenv("CLAUDE_REVIEW_OUTPUT_PRICE_USD_PER_MILLION", raising=False)

    result = review.usage_summary("claude-opus-4-7", {"input_tokens": 1000, "output_tokens": 100})

    assert f"Pricing defaults are for `{review.DEFAULT_CLAUDE_REVIEW_MODEL}`" in result


def test_price_env_uses_default_for_invalid_value(monkeypatch):
    review = _module()
    monkeypatch.setenv("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", "not-a-number")

    result = review.price_env("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", 3.0)

    assert result == 3.0


def test_price_env_uses_default_for_whitespace_value(monkeypatch):
    review = _module()
    monkeypatch.setenv("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", "  ")

    result = review.price_env("CLAUDE_REVIEW_INPUT_PRICE_USD_PER_MILLION", 3.0)

    assert result == 3.0


def test_usage_summary_skips_missing_usage():
    review = _module()

    result = review.usage_summary(review.DEFAULT_CLAUDE_REVIEW_MODEL, None)

    assert result == ""
    assert review.usage_summary(review.DEFAULT_CLAUDE_REVIEW_MODEL, {}) == ""


def test_pull_request_target_checkout_uses_base_ref():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "claude-review.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "ref: ${{ github.event.pull_request.base.sha || github.sha }}" in text


def test_pull_request_target_review_skips_forks():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "claude-review.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "github.event.pull_request.head.repo.full_name == github.repository" in text


def test_workflow_dispatch_supports_full_review_scope():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "claude-review.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "review_scope:" in text
    assert "- full" in text
    assert "REVIEW_SCOPE: ${{ github.event.inputs.review_scope || 'diff' }}" in text


def test_claude_review_workflow_exposes_review_output():
    workflow = Path(__file__).parents[1] / ".github" / "workflows" / "claude-review.yml"

    text = workflow.read_text(encoding="utf-8")

    assert "Print Claude review" in text
    assert "cat claude-review.md" in text
    assert "actions/upload-artifact@v4" in text
