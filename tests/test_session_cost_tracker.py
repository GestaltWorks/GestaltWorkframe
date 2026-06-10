"""Tests for core/session_cost_tracker.py per-session cost attribution."""

from __future__ import annotations

import gestaltworkframe.core.session_cost_tracker as session_cost_tracker_module
from gestaltworkframe.core.session_cost_tracker import (
    DEFAULT_ALERT_THRESHOLD_USD,
    SessionCostTracker,
)


def _tracker(tmp_path, monkeypatch, webhook_url=None, alert_threshold_usd=None):
    if webhook_url is None:
        monkeypatch.delenv("SESSION_COST_WEBHOOK_URL", raising=False)
    else:
        monkeypatch.setenv("SESSION_COST_WEBHOOK_URL", webhook_url)
    if alert_threshold_usd is None:
        monkeypatch.delenv("SESSION_COST_ALERT_THRESHOLD_USD", raising=False)
    else:
        monkeypatch.setenv("SESSION_COST_ALERT_THRESHOLD_USD", alert_threshold_usd)
    return SessionCostTracker(str(tmp_path / "session_cost.db"))


def test_default_config_has_no_webhook(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    config = tracker.get_config()
    assert config["webhook_configured"] is False
    assert config["alert_threshold_usd"] == DEFAULT_ALERT_THRESHOLD_USD
    assert config["store_ready"] is False


def test_invalid_alert_threshold_falls_back_to_default(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch, alert_threshold_usd="not-a-number")
    assert tracker._alert_threshold_usd == DEFAULT_ALERT_THRESHOLD_USD


def test_custom_alert_threshold_is_parsed(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch, alert_threshold_usd="2.5")
    assert tracker._alert_threshold_usd == 2.5


async def test_init_creates_tables_and_is_idempotent(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    await tracker.init()
    assert tracker._ready is True

    await tracker.init()
    assert tracker._ready is True


async def test_record_cost_without_webhook_just_records(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)

    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 100, 50, 0.01, 0.02)

    summary = await tracker.get_session_summary("session-1")
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 100
    assert summary["output_tokens"] == 50
    assert summary["total_cost_usd"] == 0.03


async def test_get_session_summary_for_unknown_session(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    await tracker.init()

    summary = await tracker.get_session_summary("missing-session")

    assert summary == {"session_id": "missing-session", "calls": 0, "total_cost_usd": 0.0}


async def test_get_session_summary_when_not_ready_returns_error(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    tracker._path = str(tmp_path / "missing-dir" / "session_cost.db")

    summary = await tracker.get_session_summary("session-1")

    assert summary == {"error": "tracker_not_ready"}


async def test_record_cost_below_threshold_does_not_alert(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(session_cost_tracker_module.httpx)
    tracker = _tracker(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/cost", alert_threshold_usd="5.0"
    )

    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 100, 50, 0.5, 0.5)

    assert recorder == []


async def test_record_cost_crossing_threshold_sends_alert_once(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(session_cost_tracker_module.httpx)
    tracker = _tracker(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/cost", alert_threshold_usd="1.0"
    )

    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 100, 50, 0.6, 0.6)
    assert len(recorder) == 1
    payload = recorder[0].json
    assert payload["event"] == "high_session_cost"
    assert payload["session_id"] == "session-1"

    # A second call that's still over threshold doesn't re-alert.
    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 100, 50, 0.6, 0.6)
    assert len(recorder) == 1


async def test_record_cost_alert_failure_is_handled_gracefully(tmp_path, monkeypatch, fake_httpx_post):
    recorder = fake_httpx_post(session_cost_tracker_module.httpx)
    recorder.status_code = 500
    tracker = _tracker(
        tmp_path, monkeypatch, webhook_url="https://hooks.example.com/cost", alert_threshold_usd="1.0"
    )

    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 100, 50, 0.6, 0.6)

    summary = await tracker.get_session_summary("session-1")
    assert summary["total_cost_usd"] == 1.2


async def test_get_top_sessions_orders_by_cost(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    await tracker.record_cost("session-cheap", "openrouter", "gpt-4o", 10, 10, 0.01, 0.01)
    await tracker.record_cost("session-expensive", "openrouter", "gpt-4o", 1000, 1000, 1.0, 1.0)

    top = await tracker.get_top_sessions(hours=24, limit=10)

    assert top[0]["session_id"] == "session-expensive"
    assert top[0]["total_cost_usd"] == 2.0
    assert top[1]["session_id"] == "session-cheap"


async def test_get_top_sessions_when_not_ready_returns_empty(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    tracker._path = str(tmp_path / "missing-dir" / "session_cost.db")

    top = await tracker.get_top_sessions()

    assert top == []


async def test_cleanup_old_records_removes_nothing_for_recent_data(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    await tracker.record_cost("session-1", "openrouter", "gpt-4o", 10, 10, 0.01, 0.01)

    deleted = await tracker.cleanup_old_records(days=30)

    assert deleted == 0


async def test_cleanup_old_records_when_not_ready_returns_zero(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, monkeypatch)
    tracker._path = str(tmp_path / "missing-dir" / "session_cost.db")

    deleted = await tracker.cleanup_old_records()

    assert deleted == 0
