"""Tests for core/key_store.py and the /admin/api/provider-keys/* endpoints.

key_store unit tests:
- set/get round-trip (correct token decrypts successfully)
- wrong admin token returns None (not an error, just None)
- has_key: True after set, False before set
- delete_key removes row; has_key becomes False
- delete_key on missing row returns False without error
- env_fallback reads the correct env var per provider
- effective_key: stored key takes precedence over env
- effective_key: falls back to env when no stored key
- two providers with different tokens don't cross-decrypt
- init() is idempotent

admin endpoint tests (TestClient, sqlite in-memory):
- POST /provider-keys/{id} stores and returns masked status
- POST /provider-keys/{id} rejects unknown provider_id
- DELETE /provider-keys/{id} removes stored key
- GET /provider-keys lists all providers with source info
- POST /provider-keys/{id}/test returns test result
- All endpoints require valid admin token (401 without it)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import gestaltworkframe.api.main as api_main
from gestaltworkframe.core.key_store import ApiKeyStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    s = ApiKeyStore(str(tmp_path / "keys.db"))
    await s.init()
    return s


# ---------------------------------------------------------------------------
# ApiKeyStore unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_and_get_round_trip(store):
    ok = await store.set_key("openrouter", "sk-or-testkey", "admin-token")
    assert ok is True
    retrieved = await store.get_key("openrouter", "admin-token")
    assert retrieved == "sk-or-testkey"


@pytest.mark.asyncio
async def test_wrong_admin_token_returns_none(store):
    await store.set_key("anthropic", "sk-ant-secret", "correct-token")
    result = await store.get_key("anthropic", "wrong-token")
    assert result is None


@pytest.mark.asyncio
async def test_has_key_false_before_set(store):
    assert await store.has_key("google") is False


@pytest.mark.asyncio
async def test_has_key_true_after_set(store):
    await store.set_key("google", "AIza-testkey", "tok")
    assert await store.has_key("google") is True


@pytest.mark.asyncio
async def test_delete_key_removes_row(store):
    await store.set_key("openai", "sk-openai", "tok")
    deleted = await store.delete_key("openai")
    assert deleted is True
    assert await store.has_key("openai") is False


@pytest.mark.asyncio
async def test_delete_key_missing_row_returns_false(store):
    result = await store.delete_key("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_env_fallback_reads_correct_var(store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-or-key")
    assert store.env_fallback("openrouter") == "env-or-key"


@pytest.mark.asyncio
async def test_env_fallback_empty_for_unknown_provider(store):
    assert store.env_fallback("unknown_provider") == ""


@pytest.mark.asyncio
async def test_effective_key_stored_takes_precedence(store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    await store.set_key("openrouter", "stored-key", "tok")
    result = await store.effective_key("openrouter", "tok")
    assert result == "stored-key"


@pytest.mark.asyncio
async def test_effective_key_falls_back_to_env(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-ant-key")
    result = await store.effective_key("anthropic", "tok")
    assert result == "env-ant-key"


@pytest.mark.asyncio
async def test_different_providers_independent(store):
    await store.set_key("openrouter", "sk-or", "tok-a")
    await store.set_key("anthropic", "sk-ant", "tok-b")
    assert await store.get_key("openrouter", "tok-a") == "sk-or"
    assert await store.get_key("anthropic", "tok-b") == "sk-ant"
    # Cross-decrypt: wrong token for openrouter should fail
    assert await store.get_key("openrouter", "tok-b") is None


@pytest.mark.asyncio
async def test_init_is_idempotent(store):
    await store.init()
    await store.init()
    # Verify the table still works after multiple init() calls
    await store.set_key("openai", "sk-test", "tok")
    assert await store.has_key("openai") is True


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_key(store):
    await store.set_key("openrouter", "old-key", "tok")
    await store.set_key("openrouter", "new-key", "tok")
    result = await store.get_key("openrouter", "tok")
    assert result == "new-key"


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------

def _make_client(tmp_path, monkeypatch):
    """Build a TestClient with in-memory SQLite + stubbed LLM services."""
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", "test-admin")
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("APP_DATABASE_PATH", db_path)
    monkeypatch.setenv("CLOUD_SPILLOVER_DB_PATH", db_path)

    # Stub out the LLM provider health check so TestClient startup doesn't
    # need a real model endpoint.
    mock_provider = MagicMock()
    mock_provider.is_healthy = AsyncMock(return_value=False)
    mock_provider.health_status = None
    mock_provider.close = AsyncMock()
    mock_provider.model = "stub"
    mock_provider.profile_name = "stub"
    mock_provider.provider_role = "primary"
    mock_provider.cost_tier = "local"
    mock_provider.allowed_response_policies = ["local_only"]
    mock_provider.capabilities = []
    mock_provider.tool_calling_quality = "none"

    # Patch build_app_services to inject our key store backed by the tmp db.
    from gestaltworkframe.core.key_store import ApiKeyStore
    from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
    from gestaltworkframe.core.router import LLMRouter, ProviderRoute
    from gestaltworkframe.core.orchestrator import Orchestrator
    from gestaltworkframe.core.policy import CloudSpendPolicy
    from gestaltworkframe.core.chat_orchestrator import ChatTurnOrchestrator
    from gestaltworkframe.api.services import AppServices, ChatMetrics

    async def _fake_build():
        key_store = ApiKeyStore(db_path)
        await key_store.init()
        global_gate = CloudBudgetGate(CloudBudgetConfig(enabled=False, sqlite_path=db_path))
        multi_gate = MultiProviderBudgetGate(global_gate)
        route = ProviderRoute(
            name="stub",
            provider=mock_provider,
            provider_type="LocalProvider",
            model="stub",
            role="primary",
            cost_tier="local",
            allowed_response_policies=["local_only"],
        )
        router = LLMRouter(
            primary=mock_provider,
            routes=[route],
            cloud_budget=multi_gate,
        )
        orchestrator = Orchestrator(CloudSpendPolicy())
        chat_turns = MagicMock(spec=ChatTurnOrchestrator)
        return AppServices(
            local_provider=mock_provider,
            secondary_provider=None,
            cloud_budget=multi_gate,
            llm_router=router,
            orchestrator=orchestrator,
            chat_turns=chat_turns,
            balance_checker=None,
            key_store=key_store,
            chat_metrics=ChatMetrics(),
        )

    monkeypatch.setattr(api_main, "build_app_services", _fake_build)
    client = TestClient(api_main.app, raise_server_exceptions=True)
    return client


def test_set_provider_key_stores_and_returns_status(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/admin/api/provider-keys/openrouter",
            json={"key": "sk-or-newkey"},
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider_id"] == "openrouter"
    assert body["stored"] is True
    assert "test" in body


def test_set_provider_key_rejects_unknown_provider(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/admin/api/provider-keys/unknown_xyz",
            json={"key": "sk-test"},
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unknown_provider"


def test_set_provider_key_requires_auth(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/admin/api/provider-keys/openrouter",
            json={"key": "sk-test"},
        )
    assert resp.status_code == 401


def test_delete_provider_key(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        # First store a key
        client.post(
            "/admin/api/provider-keys/anthropic",
            json={"key": "sk-ant-key"},
            headers={"x-admin-token": "test-admin"},
        )
        resp = client.delete(
            "/admin/api/provider-keys/anthropic",
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["provider_id"] == "anthropic"


def test_list_provider_keys_shows_all_providers(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        resp = client.get(
            "/admin/api/provider-keys",
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    # All four known providers should be listed
    for pid in ("openrouter", "anthropic", "google", "openai"):
        assert pid in body["providers"]
        entry = body["providers"][pid]
        assert "has_stored_key" in entry
        assert "has_env_key" in entry
        assert "active_source" in entry


def test_test_provider_key_no_key_configured(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/admin/api/provider-keys/google/test",
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error"] == "no_key_configured"
def test_set_provider_key_never_returns_key_value(tmp_path, monkeypatch):
    """Verify the set-key endpoint never echoes the key back in any response field."""
    client = _make_client(tmp_path, monkeypatch)
    secret = "sk-or-secret-key-should-not-appear-in-response-abc123"
    with client:
        resp = client.post(
            "/admin/api/provider-keys/openrouter",
            json={"key": secret},
            headers={"x-admin-token": "test-admin"},
        )
    assert resp.status_code == 200
    body_str = resp.text
    assert secret not in body_str, "Raw key value must never appear in any response field"
    body = resp.json()
    assert "key" not in body, "Response must not have a top-level 'key' field"
    # Nested test result must also not contain the key
    assert secret not in str(body.get("test", {}))


# ---------------------------------------------------------------------------
# Phase 5: live key validation tests (mocked HTTP)
# ---------------------------------------------------------------------------

def test_test_provider_key_anthropic_valid(tmp_path, monkeypatch):
    """Anthropic key test hits /v1/models and returns valid=True on 200."""
    client = _make_client(tmp_path, monkeypatch)
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        with client:
            # Store a key first so /test has something to test
            client.post(
                "/admin/api/provider-keys/anthropic",
                json={"key": "sk-ant-valid"},
                headers={"x-admin-token": "test-admin"},
            )
            resp = client.post(
                "/admin/api/provider-keys/anthropic/test",
                headers={"x-admin-token": "test-admin"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["error"] == ""


def test_test_provider_key_anthropic_invalid(tmp_path, monkeypatch):
    """Anthropic key test returns valid=False on 401."""
    client = _make_client(tmp_path, monkeypatch)
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        with client:
            client.post(
                "/admin/api/provider-keys/anthropic",
                json={"key": "sk-ant-bad"},
                headers={"x-admin-token": "test-admin"},
            )
            resp = client.post(
                "/admin/api/provider-keys/anthropic/test",
                headers={"x-admin-token": "test-admin"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error"] == "invalid_api_key"


def test_test_provider_key_google_valid(tmp_path, monkeypatch):
    """Google key test returns valid=True on 200 from Gemini models endpoint."""
    client = _make_client(tmp_path, monkeypatch)
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        with client:
            client.post(
                "/admin/api/provider-keys/google",
                json={"key": "AIza-valid"},
                headers={"x-admin-token": "test-admin"},
            )
            resp = client.post(
                "/admin/api/provider-keys/google/test",
                headers={"x-admin-token": "test-admin"},
            )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


def test_test_provider_key_openai_invalid(tmp_path, monkeypatch):
    """OpenAI key test returns valid=False on 401."""
    client = _make_client(tmp_path, monkeypatch)
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    with patch("httpx.AsyncClient") as mock_cls:
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_http

        with client:
            client.post(
                "/admin/api/provider-keys/openai",
                json={"key": "sk-bad"},
                headers={"x-admin-token": "test-admin"},
            )
            resp = client.post(
                "/admin/api/provider-keys/openai/test",
                headers={"x-admin-token": "test-admin"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["error"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# Phase 5: key rotation propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotate_provider_key_updates_openai_compatible(tmp_path):
    """rotate_provider_key() calls update_api_key on matching provider instances."""
    from unittest.mock import AsyncMock
    from gestaltworkframe.core.key_store import ApiKeyStore
    from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
    from gestaltworkframe.core.router import LLMRouter, ProviderRoute

    provider = AsyncMock()
    provider.update_api_key = AsyncMock()

    route = ProviderRoute(
        name="openrouter-test",
        provider=provider,
        provider_type="OpenAICompatibleProvider",
        model="some-model",
        role="primary",
        cost_tier="low_cost",
        allowed_response_policies=["default"],
        provider_budget_id="openrouter",
    )
    gate = CloudBudgetGate(CloudBudgetConfig(enabled=False, sqlite_path=":memory:"))
    multi = MultiProviderBudgetGate(gate)
    router = LLMRouter(primary=provider, routes=[route], cloud_budget=multi)

    count = await router.rotate_provider_key("openrouter", "new-key-xyz")
    assert count == 1
    provider.update_api_key.assert_awaited_once_with("new-key-xyz")


@pytest.mark.asyncio
async def test_rotate_provider_key_skips_wrong_budget_id(tmp_path):
    """rotate_provider_key() only touches routes with the matching provider_budget_id."""
    from unittest.mock import AsyncMock
    from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
    from gestaltworkframe.core.router import LLMRouter, ProviderRoute

    provider_or = AsyncMock()
    provider_or.update_api_key = AsyncMock()
    provider_an = AsyncMock()
    provider_an.update_api_key = AsyncMock()

    route_or = ProviderRoute(
        name="openrouter-route",
        provider=provider_or,
        provider_type="OpenAICompatibleProvider",
        model="x",
        role="primary",
        cost_tier="low_cost",
        allowed_response_policies=["default"],
        provider_budget_id="openrouter",
    )
    route_an = ProviderRoute(
        name="claude-route",
        provider=provider_an,
        provider_type="ClaudeProvider",
        model="y",
        role="primary",
        cost_tier="premium",
        allowed_response_policies=["default"],
        provider_budget_id="anthropic",
    )

    from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate, MultiProviderBudgetGate
    gate = CloudBudgetGate(CloudBudgetConfig(enabled=False, sqlite_path=":memory:"))
    multi = MultiProviderBudgetGate(gate)
    router = LLMRouter(primary=provider_or, routes=[route_or, route_an], cloud_budget=multi)

    count = await router.rotate_provider_key("anthropic", "new-an-key")
    assert count == 1
    provider_an.update_api_key.assert_awaited_once_with("new-an-key")
    provider_or.update_api_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 5: github + brave in key store provider map
# ---------------------------------------------------------------------------

def test_key_store_has_github_and_brave_providers():
    """key_store._PROVIDER_ENV_VARS includes github and brave."""
    from gestaltworkframe.core.key_store import _PROVIDER_ENV_VARS
    assert "github" in _PROVIDER_ENV_VARS
    assert _PROVIDER_ENV_VARS["github"] == "APP_GITHUB_TOKEN"
    assert "brave" in _PROVIDER_ENV_VARS
    assert _PROVIDER_ENV_VARS["brave"] == "BRAVE_SEARCH_API_KEY"


@pytest.mark.asyncio
async def test_set_and_get_github_key(store):
    """github provider_id can be stored and retrieved like any other."""
    ok = await store.set_key("github", "ghp_test_token_abc", "tok")
    assert ok is True
    result = await store.get_key("github", "tok")
    assert result == "ghp_test_token_abc"


@pytest.mark.asyncio
async def test_set_and_get_brave_key(store):
    """brave provider_id can be stored and retrieved."""
    ok = await store.set_key("brave", "BSA_secret_key_xyz", "tok")
    assert ok is True
    result = await store.get_key("brave", "tok")
    assert result == "BSA_secret_key_xyz"


@pytest.mark.asyncio
async def test_env_fallback_github(store, monkeypatch):
    """env_fallback for github reads APP_GITHUB_TOKEN."""
    monkeypatch.setenv("APP_GITHUB_TOKEN", "env-github-token")
    result = store.env_fallback("github")
    assert result == "env-github-token"


@pytest.mark.asyncio
async def test_env_fallback_brave(store, monkeypatch):
    """env_fallback for brave reads BRAVE_SEARCH_API_KEY."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "env-brave-key")
    result = store.env_fallback("brave")
    assert result == "env-brave-key"
