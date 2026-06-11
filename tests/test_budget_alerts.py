"""Tests for core/budget_alerts.py budget threshold alerting."""

from __future__ import annotations

import gestaltworkframe.core.budget_alerts as budget_alerts_module
from gestaltworkframe.core.budget_alerts import DEFAULT_THRESHOLDS, BudgetAlertManager


def _manager(tmp_path, monkeypatch, webhook_url=None, thresholds=None):
    if webhook_url is None:
        monkeypatch.delenv("BUDGET_ALERT_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("BUDGET_ALERT_WEBHOOK_URL", webhook_url)
    if thresholds is None:
        monkeypatch.delenv("BUDGET_ALERT_THRESHOLDS", raising=False)
    else:
        monkeypatch.setenv("BUDGET_ALERT_THRESHOLDS", thresholds)
    return BudgetAlertManager(str(tmp_path / "budget.db"))


def test_default_config_has_no_webhook(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    config = manager.get_config()
    assert config["webhook_configured"] is False
    assert config["thresholds"] == DEFAULT_THRESHOLDS
    assert config["store_ready"] is False


def test_custom_thresholds_are_parsed_clamped_and_deduped(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, thresholds="50, 90, 150, -10, 90")
    assert manager._thresholds == [0, 50, 90, 100]


def test_invalid_thresholds_fall_back_to_default(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, thresholds="not,numbers")
    assert manager._thresholds == DEFAULT_THRESHOLDS


async def test_init_creates_table_and_is_idempotent(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    await manager.init()
    assert manager._ready is True

    # Second call short-circuits without error.
    await manager.init()
    assert manager._ready is True


async def test_check_and_alert_without_webhook_returns_empty(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, webhook_url=None)
    fired = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)
    assert fired == []


async def test_check_and_alert_zero_limit_returns_empty(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget")
    fired = await manager.check_and_alert("openrouter", limit_usd=0.0, used_usd=5.0)
    assert fired == []


async def test_check_and_alert_fires_crossed_thresholds(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(budget_alerts_module.httpx)
    manager = _manager(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget", thresholds="80,100"
    )

    fired = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)

    assert len(fired) == 1
    alert = fired[0]
    assert alert.provider_id == "openrouter"
    assert alert.threshold_pct == 80
    assert alert.remaining_usd == 1.0
    assert len(recorder) == 1
    assert recorder[0].json["event"] == "budget_threshold_crossed"
    assert recorder[0].json["threshold_pct"] == 80


async def test_check_and_alert_does_not_refire_same_day(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(budget_alerts_module.httpx)
    manager = _manager(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget", thresholds="80"
    )

    first = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)
    second = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.5)

    assert len(first) == 1
    assert second == []
    assert len(recorder) == 1


async def test_check_and_alert_handles_webhook_failure_gracefully(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(budget_alerts_module.httpx)
    recorder.status_code = 500
    manager = _manager(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget", thresholds="80"
    )

    fired = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)

    # Alert is still recorded as fired even though the webhook delivery failed.
    assert len(fired) == 1


async def test_reset_alerts_clears_for_specific_provider(tmp_path, monkeypatch, fake_httpx_post):
    fake_httpx_post(budget_alerts_module.httpx)
    manager = _manager(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget", thresholds="80"
    )
    await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)
    await manager.check_and_alert("anthropic", limit_usd=10.0, used_usd=9.0)

    cleared = await manager.reset_alerts("openrouter")
    assert cleared == 1

    # openrouter can fire again, anthropic still deduped.
    refired = await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)
    still_deduped = await manager.check_and_alert("anthropic", limit_usd=10.0, used_usd=9.0)
    assert len(refired) == 1
    assert still_deduped == []


async def test_reset_alerts_clears_all_when_no_provider_given(tmp_path, monkeypatch, fake_httpx_post):
    fake_httpx_post(budget_alerts_module.httpx)
    manager = _manager(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/budget", thresholds="80"
    )
    await manager.check_and_alert("openrouter", limit_usd=10.0, used_usd=9.0)
    await manager.check_and_alert("anthropic", limit_usd=10.0, used_usd=9.0)

    cleared = await manager.reset_alerts()
    assert cleared == 2


async def test_reset_alerts_when_not_ready_returns_zero(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    # Point at a path whose parent directory does not exist so init() fails.
    manager._path = str(tmp_path / "missing-dir" / "budget.db")

    cleared = await manager.reset_alerts()

    assert cleared == 0
    assert manager._ready is False
