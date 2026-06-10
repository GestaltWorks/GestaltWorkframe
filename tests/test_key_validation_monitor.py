"""Tests for core/key_validation_monitor.py validation failure monitoring."""

from __future__ import annotations

import gestaltworkframe.core.key_validation_monitor as key_validation_monitor_module
from gestaltworkframe.core.key_validation_monitor import (
    DEFAULT_FAILURE_THRESHOLD,
    KeyValidationMonitor,
)


def _monitor(tmp_path, monkeypatch, webhook_url=None, failure_threshold=None):
    if webhook_url is None:
        monkeypatch.delenv("KEY_VALIDATION_ALERT_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("KEY_VALIDATION_ALERT_WEBHOOK_URL", webhook_url)
    if failure_threshold is None:
        monkeypatch.delenv("KEY_VALIDATION_FAILURE_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("KEY_VALIDATION_FAILURE_THRESHOLD", failure_threshold)
    return KeyValidationMonitor(str(tmp_path / "monitor.db"))


def test_default_config_has_no_webhook(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    config = monitor.get_config()
    assert config["webhook_configured"] is False
    assert config["failure_threshold"] == DEFAULT_FAILURE_THRESHOLD
    assert config["store_ready"] is False


def test_invalid_failure_threshold_falls_back_to_default(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch, failure_threshold="not-a-number")
    assert monitor._failure_threshold == DEFAULT_FAILURE_THRESHOLD


def test_custom_failure_threshold_is_parsed(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch, failure_threshold="7")
    assert monitor._failure_threshold == 7


async def test_init_creates_tables_and_is_idempotent(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    await monitor.init()
    assert monitor._ready is True

    await monitor.init()
    assert monitor._ready is True


async def test_record_attempt_success_does_not_check_failures(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(key_validation_monitor_module.httpx)
    monitor = _monitor(tmp_path, monkeypatch, webhook_url="https://hooks.example.com/keys")

    await monitor.record_attempt("openrouter", success=True)

    assert recorder == []
    stats = await monitor.get_stats("openrouter")
    assert stats["success"] == 1
    assert stats["failures"] == 0


async def test_record_attempt_failure_without_webhook_skips_alert(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch, webhook_url=None)

    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")

    stats = await monitor.get_stats("openrouter")
    assert stats["failures"] == 1


async def test_record_attempt_below_threshold_does_not_alert(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(key_validation_monitor_module.httpx)
    monitor = _monitor(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/keys", failure_threshold="3"
    )

    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")
    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")

    assert recorder == []


async def test_record_attempt_at_threshold_sends_alert(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(key_validation_monitor_module.httpx)
    monitor = _monitor(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/keys", failure_threshold="2"
    )

    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")
    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key", details="401")

    assert len(recorder) == 1
    payload = recorder[0].json
    assert payload["event"] == "key_validation_failures"
    assert payload["provider_id"] == "openrouter"
    assert payload["failure_count_last_hour"] == 2


async def test_record_attempt_dedupes_alert_within_an_hour(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(key_validation_monitor_module.httpx)
    monitor = _monitor(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/keys", failure_threshold="1"
    )

    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")
    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")

    # Threshold of 1 is crossed both times, but the second alert is deduped.
    assert len(recorder) == 1


async def test_record_attempt_alert_failure_is_handled_gracefully(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(key_validation_monitor_module.httpx)
    recorder.status_code = 500
    monitor = _monitor(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/keys", failure_threshold="1"
    )

    await monitor.record_attempt("openrouter", success=False, failure_type="invalid_api_key")

    stats = await monitor.get_stats("openrouter")
    assert stats["failures"] == 1


async def test_get_stats_for_unknown_provider_is_empty(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    await monitor.init()

    stats = await monitor.get_stats("openrouter")

    assert stats == {"provider_id": "openrouter", "hours": 24, "success": 0, "failures": 0}


async def test_get_stats_without_provider_groups_by_provider(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    await monitor.record_attempt("openrouter", success=True)
    await monitor.record_attempt("anthropic", success=False, failure_type="timeout")

    stats = await monitor.get_stats()

    assert stats["providers"]["openrouter"]["success"] == 1
    assert stats["providers"]["anthropic"]["failures"] == 1


async def test_get_stats_when_not_ready_returns_error(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    monitor._path = str(tmp_path / "missing-dir" / "monitor.db")

    stats = await monitor.get_stats("openrouter")

    assert stats == {"error": "monitor_not_ready"}


async def test_cleanup_old_records_removes_nothing_for_recent_data(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    await monitor.record_attempt("openrouter", success=True)

    deleted = await monitor.cleanup_old_records(days=7)

    assert deleted == 0


async def test_cleanup_old_records_when_not_ready_returns_zero(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    monitor._path = str(tmp_path / "missing-dir" / "monitor.db")

    deleted = await monitor.cleanup_old_records()

    assert deleted == 0
