import tomllib
from pathlib import Path


def _workflow_env_value(workflow: str, name: str) -> int:
    prefix = f"{name}: \""
    line = next(item for item in workflow.splitlines() if prefix in item)
    return int(line.split(prefix, 1)[1].split("\"", 1)[0])


def test_deploy_preserves_production_kb_vector_store():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "--exclude 'kb/chroma_db/'" in script


def test_deploy_preserves_generated_vps_ssh_keys():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "--exclude '.ssh/'" in script


def test_production_deploy_validates_powershell_scripts():
    workflow = Path(".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")

    assert "Validate PowerShell scripts" in workflow
    assert "shell: pwsh" in workflow
    assert "llm/download_model.ps1" in workflow
    assert "llm/start_server.ps1" in workflow
    assert "llm/control_local_model.ps1" in workflow


def test_windows_control_script_starts_llama_with_logs_and_working_directory():
    script = Path("llm/control_local_model.ps1").read_text(encoding="utf-8")

    assert "-WorkingDirectory $workingDirectory" in script
    assert "-RedirectStandardOutput $stdoutLog" in script
    assert "-RedirectStandardError $stderrLog" in script
    assert "llama-server exited immediately" in script


def test_production_deploy_syncs_cloud_spillover_budget_caps():
    workflow = Path(".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    for name in (
        "ENABLE_CLOUD_SPILLOVER",
        "ENABLE_LOW_COST_CLOUD",
        "ENABLE_CLAUDE_FALLBACK",
        "CLOUD_SPILLOVER_MAX_CALLS_PER_TURN",
        "CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION",
        "CLOUD_SPILLOVER_MAX_CALLS_PER_DAY",
        "CLOUD_SPILLOVER_MAX_CALLS_PER_MONTH",
        "CLOUD_SPILLOVER_MAX_DAILY_USD",
        "CLOUD_SPILLOVER_MAX_MONTHLY_USD",
        "CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL",
        "CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL",
    ):
        assert name in workflow
        assert f'sync_remote_env_secret "{name}"' in script


def test_deploy_syncs_discovery_github_tokens():
    prod = Path(".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    dev = Path(".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    for name in (
        "APP_GITHUB_TOKEN",
        "LIBRARY_PUBLISHER_GITHUB_APP_ID",
        "LIBRARY_PUBLISHER_GITHUB_INSTALLATION_ID",
        "LIBRARY_PUBLISHER_GITHUB_PRIVATE_KEY_B64",
    ):
        assert name in prod
        assert name in dev
        assert f'sync_remote_env_secret "{name}"' in script


def test_deploy_rejects_multiline_env_values_before_syncing_dotenv():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "reject_multiline_env_value" in script
    assert "must be a single-line value before syncing to .env" in script
    assert "Use unwrapped base64 for encoded secrets" in script
    assert "reject_multiline_env_value \"$secret_name\" \"$secret_value\"" in script


def test_production_cloud_spillover_defaults_allow_real_conversation():
    workflow = Path(".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")

    assert _workflow_env_value(workflow, "CLOUD_SPILLOVER_MAX_CALLS_PER_TURN") == 1
    assert _workflow_env_value(workflow, "CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION") >= 10
    assert _workflow_env_value(workflow, "CLOUD_SPILLOVER_MAX_CALLS_PER_DAY") >= 20
    assert _workflow_env_value(workflow, "CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL") >= 16000
    assert _workflow_env_value(workflow, "CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL") >= 2048


def test_production_deploy_omits_stale_local_llm_control_env():
    workflow = Path(".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")

    assert "LOCAL_LLM_START_COMMAND" not in workflow
    assert "LOCAL_LLM_STOP_COMMAND" not in workflow
    assert "LOCAL_LLM_CONTROL_" not in workflow
    assert "LOCAL_LLM_WINDOWS_" not in workflow


def test_httpx_is_direct_dependency():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    direct_deps = {
        dep.split("[", 1)[0].split(">", 1)[0].split("<", 1)[0].split("=", 1)[0].strip()
        for dep in pyproject["project"]["dependencies"]
    }
    assert "httpx" in direct_deps


def test_legacy_cli_sample_files_removed():
    removed = [
        "main.py",
        "mcp_client.py",
        "mcp_server.py",
        "core/cli.py",
        "core/cli_chat.py",
        "core/chat.py",
        "core/claude.py",
        "core/tools.py",
        "tests/test_tools.py",
    ]

    assert all(not Path(path).exists() for path in removed)


def test_deploy_exposes_library_and_ai_discovery_files():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "location = /library" in script
    assert "location = /library/" in script
    assert "alias {web_dir}/out/library.html" in script
    assert "return 301 /library" in script
    assert "re.sub(" in script
    assert "LIBRARY_NGINX_TMP" in script
    assert "$ROOT_SITE_DIR/llms.txt" in script
    assert "$ROOT_SITE_DIR/robots.txt" in script


def test_deploy_cuts_root_over_to_terminal_landing_page():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "Root cutover" in script
    assert "location = / {" in script
    assert "root {web_dir}/out" in script
    assert "try_files /index.html =404" in script
    assert "location = /terminal" in script
    assert "alias {web_dir}/out/terminal.html" in script


def test_deploy_exposes_product_brief_and_pdf_download():
    script = Path(".github/scripts/deploy_vps.sh").read_text(encoding="utf-8")

    assert "location = /product-brief" in script
    assert "alias {web_dir}/out/product-brief/index.html" in script
    assert "location = /product-brief.pdf" in script
    assert "default_type application/pdf" in script
    assert "alias {web_dir}/out/product-brief.pdf" in script
    assert "Content-Disposition" in script
    assert "product-brief.pdf" in script
    assert "location /product-brief/assets/" in script
    assert "alias {web_dir}/out/product-brief/assets/" in script
