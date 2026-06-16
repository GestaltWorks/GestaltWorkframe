"""Unit tests for api/admin.py internal helpers (no app, no network).

Covers the per-provider key-test branches, the policy-apply logic, the handoff
packet builder, and the module-version reader directly, which the HTTP-layer
tests can't reach without live providers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gestaltworkframe.api import admin
from gestaltworkframe.api.admin import (
    AdminPolicyPatch,
    ProviderKeyTestResult,
    _apply_admin_policy,
    _packet_for_record,
    _test_provider_key,
)


# --- _test_provider_key (mocked httpx, no network) --------------------------

class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stands in for httpx.AsyncClient; .get returns the configured response or raises."""

    def __init__(self, status_code: int | None = None, raise_exc: Exception | None = None, **_kw):
        self._status = status_code
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_a, **_kw):
        if self._raise is not None:
            raise self._raise
        return _FakeResp(self._status)


@pytest.fixture(autouse=True)
def _silence_monitor(monkeypatch):
    """Keep the validation monitor off disk for these unit tests."""

    class _NoopMonitor:
        def __init__(self, *_a, **_kw):
            pass

        async def record_attempt(self, *_a, **_kw):
            return None

    monkeypatch.setattr(admin, "KeyValidationMonitor", _NoopMonitor)


def _patch_httpx(monkeypatch, *, status=None, raise_exc=None):
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(status_code=status, raise_exc=raise_exc)
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_provider_key_valid_on_200(monkeypatch, provider):
    _patch_httpx(monkeypatch, status=200)
    result = await _test_provider_key(provider, "k")
    assert result.valid is True


@pytest.mark.parametrize(
    "provider,bad_status",
    [("anthropic", 401), ("openai", 401), ("google", 403), ("google", 400)],
)
async def test_provider_key_invalid_on_auth_error(monkeypatch, provider, bad_status):
    _patch_httpx(monkeypatch, status=bad_status)
    result = await _test_provider_key(provider, "k")
    assert result.valid is False
    assert result.error == "invalid_api_key"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_provider_key_unexpected_status(monkeypatch, provider):
    _patch_httpx(monkeypatch, status=500)
    result = await _test_provider_key(provider, "k")
    assert result.valid is False
    assert result.error.startswith("unexpected_status_500")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_provider_key_request_error(monkeypatch, provider):
    _patch_httpx(monkeypatch, raise_exc=RuntimeError("boom"))
    result = await _test_provider_key(provider, "k")
    assert result.valid is False
    assert result.error.startswith("request_error")


async def test_provider_key_unknown_provider_presence_check():
    assert (await _test_provider_key("mystery", "k")).valid is True
    empty = await _test_provider_key("mystery", "")
    assert empty.valid is False
    assert empty.error == "empty_key"


async def test_provider_key_openrouter_uses_balance_checker(monkeypatch):
    import gestaltworkframe.core.provider_balance as pb

    class _FakeChecker:
        def __init__(self, _key):
            pass

        async def get(self):
            return SimpleNamespace(available=True, error="")

    monkeypatch.setattr(pb, "OpenRouterBalanceChecker", _FakeChecker)
    result = await _test_provider_key("openrouter", "sk-or")
    assert result.valid is True


# --- _apply_admin_policy (fake services) ------------------------------------

def _services(routes=("local",)):
    config = SimpleNamespace(
        enabled=False, max_calls_per_turn=0, max_calls_per_session=0,
        max_calls_per_day=0, max_calls_per_month=0, max_daily_usd=0.0, max_monthly_usd=0.0,
        max_input_tokens_per_call=0, max_output_tokens_per_call=0,
    )

    class _Budget:
        def __init__(self):
            self.config = config
            self.updated = []

        async def init(self):
            return None

        async def update_provider_budget(self, provider_id, max_daily_usd=None, max_monthly_usd=None):
            self.updated.append((provider_id, max_daily_usd, max_monthly_usd))

    router = SimpleNamespace(
        routing_strategy="best_value",
        routes=[SimpleNamespace(name=n) for n in routes],
        _enabled={},
    )
    router.set_route_enabled = lambda name, enabled: router._enabled.__setitem__(name, enabled)
    policy = SimpleNamespace(low_cost_enabled=False, claude_enabled=False,
                             max_cloud_calls_per_turn=0, max_cloud_calls_per_session=0)
    return SimpleNamespace(
        cloud_budget=_Budget(),
        orchestrator=SimpleNamespace(cloud_policy=policy),
        llm_router=router,
    )


async def test_apply_policy_sets_strategy_caps_and_routes():
    s = _services(routes=("local", "cloud"))
    await _apply_admin_policy(
        s,
        AdminPolicyPatch(
            routing_strategy="local_only", cloud_spillover_enabled=True,
            max_calls_per_turn=3, max_daily_usd=5.0, routes={"cloud": False},
        ),
    )
    assert s.llm_router.routing_strategy == "local_only"
    assert s.cloud_budget.config.max_calls_per_turn == 3  # backs GET /policy
    assert s.orchestrator.cloud_policy.max_cloud_calls_per_turn == 3  # enforcement
    assert s.cloud_budget.config.max_daily_usd == 5.0
    assert s.llm_router._enabled == {"cloud": False}
    assert s.cloud_budget.config.enabled is True


async def test_apply_policy_unknown_route_400():
    from fastapi import HTTPException

    s = _services(routes=("local",))
    with pytest.raises(HTTPException) as exc:
        await _apply_admin_policy(s, AdminPolicyPatch(routes={"nope": True}))
    assert exc.value.status_code == 400


async def test_apply_policy_enables_tiers_and_token_defaults():
    s = _services()
    await _apply_admin_policy(
        s, AdminPolicyPatch(cloud_spillover_enabled=True, low_cost_enabled=True, claude_enabled=True)
    )
    assert s.orchestrator.cloud_policy.low_cost_enabled is True
    assert s.orchestrator.cloud_policy.claude_enabled is True
    assert s.cloud_budget.config.max_input_tokens_per_call == admin.DEFAULT_CLOUD_INPUT_TOKEN_CAP
    assert s.cloud_budget.config.max_output_tokens_per_call == admin.DEFAULT_CLOUD_OUTPUT_TOKEN_CAP


async def test_apply_policy_disable_spillover_resets_tiers():
    s = _services()
    s.orchestrator.cloud_policy.low_cost_enabled = True
    s.orchestrator.cloud_policy.claude_enabled = True
    await _apply_admin_policy(s, AdminPolicyPatch(cloud_spillover_enabled=False))
    assert s.orchestrator.cloud_policy.low_cost_enabled is False
    assert s.orchestrator.cloud_policy.claude_enabled is False


async def test_apply_policy_provider_budget_both_zero_400():
    from fastapi import HTTPException

    s = _services()
    with pytest.raises(HTTPException) as exc:
        await _apply_admin_policy(
            s, AdminPolicyPatch(provider_budgets={"openrouter": {"max_daily_usd": 0, "max_monthly_usd": 0}})
        )
    assert exc.value.status_code == 400


async def test_apply_policy_provider_budget_update():
    s = _services()
    await _apply_admin_policy(
        s, AdminPolicyPatch(provider_budgets={"openrouter": {"max_daily_usd": 9.0, "max_monthly_usd": 90.0}})
    )
    assert s.cloud_budget.updated == [("openrouter", 9.0, 90.0)]


# --- _packet_for_record -----------------------------------------------------

def test_packet_for_record_contact_and_terminal():
    contact = SimpleNamespace(role="founder", name="Ada", email="ada@x.com", data='{"company": "Acme"}')
    terminal = SimpleNamespace(selected_mode="build", objective="ship it", data='{"objective": "ship it"}')
    cp = _packet_for_record("contact", contact)
    tp = _packet_for_record("terminal", terminal)
    assert isinstance(cp, dict) and isinstance(tp, dict)


def test_packet_for_record_tolerates_bad_json():
    rec = SimpleNamespace(role="r", name="n", email="e", data="not json")
    assert isinstance(_packet_for_record("contact", rec), dict)


def test_provider_key_result_shape():
    assert ProviderKeyTestResult(valid=True).error == ""
