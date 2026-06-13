# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the canonical provider resolution rule."""

from __future__ import annotations

from gestalt_llm_contract import (
    LLMProvider,
    ProviderConfig,
    env,
    provider_config_from_env,
    resolve_provider_config,
)


def test_openrouter_key_selects_openrouter_with_defaults():
    cfg = resolve_provider_config({env.OPENROUTER_API_KEY: "sk-or-x"})
    assert cfg.kind == "openrouter"
    assert cfg.base_url == env.DEFAULT_OPENROUTER_BASE_URL
    assert cfg.model == env.DEFAULT_OPENROUTER_MODEL
    assert cfg.api_key == "sk-or-x"
    assert cfg.fallback_enabled is False


def test_openrouter_overrides_base_and_model():
    cfg = resolve_provider_config(
        {
            env.OPENROUTER_API_KEY: "sk-or-x",
            env.OPENROUTER_BASE_URL: "https://proxy.example/v1",
            env.OPENROUTER_MODEL: "anthropic/claude-3.5-sonnet",
        }
    )
    assert cfg.base_url == "https://proxy.example/v1"
    assert cfg.model == "anthropic/claude-3.5-sonnet"


def test_falls_back_to_local_without_openrouter():
    cfg = resolve_provider_config(
        {env.LOCAL_LLM_BASE_URL: "http://localhost:9000/v1", env.LOCAL_LLM_MODEL: "qwen"}
    )
    assert cfg.kind == "local"
    assert cfg.base_url == "http://localhost:9000/v1"
    assert cfg.model == "qwen"
    assert cfg.api_key == ""


def test_local_uses_default_base_url():
    cfg = resolve_provider_config({})
    assert cfg.kind == "local"
    assert cfg.base_url == env.DEFAULT_LOCAL_BASE_URL


def test_fallback_requires_both_flag_and_key():
    # Flag on but no key -> disabled.
    cfg = resolve_provider_config(
        {env.OPENROUTER_API_KEY: "k", env.ENABLE_CLAUDE_FALLBACK: "true"}
    )
    assert cfg.fallback_enabled is False
    # Key but flag off -> disabled.
    cfg = resolve_provider_config(
        {env.OPENROUTER_API_KEY: "k", env.ANTHROPIC_API_KEY: "sk-ant"}
    )
    assert cfg.fallback_enabled is False
    # Both -> enabled.
    cfg = resolve_provider_config(
        {
            env.OPENROUTER_API_KEY: "k",
            env.ANTHROPIC_API_KEY: "sk-ant",
            env.ENABLE_CLAUDE_FALLBACK: "1",
        }
    )
    assert cfg.fallback_enabled is True
    assert cfg.anthropic_api_key == "sk-ant"
    assert cfg.anthropic_model == env.DEFAULT_ANTHROPIC_MODEL


def test_truthy_variants():
    for raw in ("1", "true", "TRUE", "yes", "on", " On "):
        cfg = resolve_provider_config(
            {
                env.OPENROUTER_API_KEY: "k",
                env.ANTHROPIC_API_KEY: "a",
                env.ENABLE_CLAUDE_FALLBACK: raw,
            }
        )
        assert cfg.fallback_enabled is True, raw
    for raw in ("0", "false", "no", "off", ""):
        cfg = resolve_provider_config(
            {
                env.OPENROUTER_API_KEY: "k",
                env.ANTHROPIC_API_KEY: "a",
                env.ENABLE_CLAUDE_FALLBACK: raw,
            }
        )
        assert cfg.fallback_enabled is False, raw


def test_values_are_stripped():
    cfg = resolve_provider_config({env.OPENROUTER_API_KEY: "  sk-or-x  "})
    assert cfg.api_key == "sk-or-x"


def test_from_env_reads_os_environ(monkeypatch):
    monkeypatch.setenv(env.OPENROUTER_API_KEY, "sk-or-env")
    cfg = provider_config_from_env()
    assert cfg.kind == "openrouter"
    assert cfg.api_key == "sk-or-env"


def test_config_is_frozen():
    cfg = resolve_provider_config({})
    try:
        cfg.kind = "openrouter"  # type: ignore[misc]
    except Exception as exc:
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:
        raise AssertionError("ProviderConfig should be frozen")


def test_protocol_is_runtime_checkable():
    class Good:
        async def chat(self, *, system, user, temperature=0.2, max_tokens=4096):
            return "ok"

    class Bad:
        pass

    assert isinstance(Good(), LLMProvider)
    assert not isinstance(Bad(), LLMProvider)
    assert isinstance(resolve_provider_config({}), ProviderConfig)
