"""Tests for the token-gated admin API (api/admin.py) and the app services build.

These exercise the real app (TestClient) with a real AppServices graph built by
build_app_services, so they also act as a regression guard for the service-build
startup path. Provider-key storage points at a temp DB and the network-y key
test is mocked; everything else runs against the real router/budget/key-store.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import gestaltworkframe.api.admin as admin
import gestaltworkframe.api.main as api_main
import gestaltworkframe.api.services as services_mod
from gestaltworkframe.api.admin import ProviderKeyTestResult
from gestaltworkframe.core.key_store import ApiKeyStore

_TOKEN = "test-admin"
_AUTH = {"X-Admin-Token": _TOKEN}


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_POLICY_TOKEN", _TOKEN)
    # Avoid loading the heavy retrieval stack during the services build.
    monkeypatch.setattr(services_mod, "KnowledgeRetriever", lambda *a, **k: SimpleNamespace())

    db_path = tmp_path / "admin.db"
    services = asyncio.run(services_mod.build_app_services())
    # Isolate key storage + the validation monitor to the temp DB.
    services.key_store = ApiKeyStore(str(db_path))
    asyncio.run(services.key_store.init())
    asyncio.run(services.cloud_budget.init())
    admin._validation_monitor = None  # rebuild against the temp key-store path

    # Keep the key "test" off the network; we exercise the endpoint glue.
    async def _fake_test(provider_id, api_key, services=None):
        return ProviderKeyTestResult(valid=True)

    monkeypatch.setattr(admin, "_test_provider_key", _fake_test)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    maker = asyncio.run(_init())
    monkeypatch.setattr(admin, "async_session_maker", maker)

    async def override_session():
        async with maker() as session:
            yield session

    api_main.app.state.services = services
    api_main.app.dependency_overrides[api_main.get_session] = override_session
    try:
        yield TestClient(api_main.app)
    finally:
        api_main.app.dependency_overrides.clear()
        admin._validation_monitor = None
        asyncio.run(engine.dispose())


# --- build_app_services regression guard ------------------------------------

def test_build_app_services_constructs_full_graph(admin_client):
    # The fixture already built services via build_app_services; if the build
    # path regressed (e.g. the undefined key_store / awaited sync build_routes
    # bugs) the fixture would have raised. Confirm the graph is wired.
    services = api_main.app.state.services
    assert services.llm_router is not None
    assert services.cloud_budget is not None
    assert services.key_store is not None


# --- auth -------------------------------------------------------------------

def test_admin_requires_token(admin_client):
    assert admin_client.get("/admin/api/policy").status_code == 401
    assert admin_client.get("/admin/api/policy", headers={"X-Admin-Token": "wrong"}).status_code == 401


# --- policy -----------------------------------------------------------------

def test_admin_policy_get(admin_client):
    r = admin_client.get("/admin/api/policy", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "routing_strategy" in body
    assert "cloud_spillover" in body
    assert "route_overrides" in body


def test_admin_policy_patch_applies(admin_client):
    r = admin_client.patch(
        "/admin/api/policy",
        headers=_AUTH,
        json={
            "cloud_spillover_enabled": True,
            "max_calls_per_turn": 3,
            "max_daily_usd": 5.0,
            "max_output_tokens_per_call": 1024,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"]["max_calls_per_turn"] == 3
    assert body["policy"]["cloud_spillover_enabled"] is True
    # The change is live on the services graph.
    assert api_main.app.state.services.cloud_budget.config.max_calls_per_turn == 3


def test_admin_policy_patch_rejects_unknown_routing_strategy(admin_client):
    r = admin_client.patch(
        "/admin/api/policy", headers=_AUTH, json={"routing_strategy": "nonsense"}
    )
    assert r.status_code == 422


# --- health / diagnostics ---------------------------------------------------

def test_admin_health(admin_client):
    r = admin_client.get("/admin/api/health", headers=_AUTH)
    assert r.status_code == 200
    assert "status" in r.json()


def test_admin_router_diagnostics(admin_client):
    r = admin_client.get("/admin/api/router-diagnostics", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "routing_strategy" in body
    assert "circuit_breaker" in body
    assert "provider_routes" in body


# --- provider keys ----------------------------------------------------------

def test_list_provider_keys(admin_client):
    r = admin_client.get("/admin/api/provider-keys", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body and "openrouter" in body["providers"]
    assert "validation_monitor" in body


def test_set_provider_key_unknown_provider(admin_client):
    r = admin_client.post(
        "/admin/api/provider-keys/not-a-provider", headers=_AUTH, json={"key": "x"}
    )
    assert r.status_code == 400


def test_set_and_delete_provider_key_roundtrip(admin_client):
    set_resp = admin_client.post(
        "/admin/api/provider-keys/openrouter", headers=_AUTH, json={"key": "sk-or-test"}
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["stored"] is True

    listed = admin_client.get("/admin/api/provider-keys", headers=_AUTH).json()
    assert listed["providers"]["openrouter"]["has_stored_key"] is True

    del_resp = admin_client.delete("/admin/api/provider-keys/openrouter", headers=_AUTH)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True


def test_test_provider_key_no_key_configured(admin_client):
    r = admin_client.post("/admin/api/provider-keys/github/test", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["provider_id"] == "github"
    # github has no stored or env key in the test environment.
    assert body["valid"] is False


def test_validation_stats(admin_client):
    r = admin_client.get("/admin/api/provider-keys/validation-stats", headers=_AUTH)
    assert r.status_code == 200


# --- cloud budget -----------------------------------------------------------

def test_clear_accounting_block(admin_client):
    r = admin_client.post("/admin/api/cloud-budget/clear-accounting-block", headers=_AUTH)
    assert r.status_code == 200
    assert "cloud_budget" in r.json()


# --- handoffs ---------------------------------------------------------------

def test_handoffs_empty(admin_client):
    r = admin_client.get("/admin/api/handoffs", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["packets"] == []


def test_handoff_approve_bad_uuid(admin_client):
    r = admin_client.post("/admin/api/handoffs/contact/not-a-uuid/approve", headers=_AUTH)
    assert r.status_code == 400


def test_handoff_approve_unknown_type(admin_client):
    r = admin_client.post(
        f"/admin/api/handoffs/widget/{uuid4()}/approve", headers=_AUTH
    )
    assert r.status_code == 400


def test_handoff_approve_not_found(admin_client):
    r = admin_client.post(
        f"/admin/api/handoffs/contact/{uuid4()}/approve", headers=_AUTH
    )
    assert r.status_code == 404


def test_handoff_packet_bad_uuid(admin_client):
    r = admin_client.get("/admin/api/handoff-packet/contact/not-a-uuid", headers=_AUTH)
    assert r.status_code == 400
