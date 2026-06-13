"""Key validation monitoring and alerting.

Tracks API key validation attempts, failures, and repeated failures per provider.
Alerts when failure patterns indicate a compromised, expired, or revoked key.

Environment:
  KEY_VALIDATION_ALERT_WEBHOOK_URL  - Optional webhook for validation failures
  KEY_VALIDATION_FAILURE_THRESHOLD  - Failures per hour before alert (default: 3)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_THRESHOLD = 3


@dataclass
class ValidationFailure:
    provider_id: str
    failure_type: str  # e.g., "invalid_api_key", "network_error", "timeout"
    timestamp: str
    details: str = ""


class KeyValidationMonitor:
    """Monitors key validation attempts and alerts on suspicious patterns."""

    def __init__(self, sqlite_path: str = "database.db") -> None:
        self._path = sqlite_path
        self._ready = False
        self._lock: asyncio.Lock | None = None
        self._webhook_url: str | None = None
        self._failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
        self._load_config()

    def _load_config(self) -> None:
        self._webhook_url = os.getenv("KEY_VALIDATION_ALERT_WEBHOOK_URL", "").strip() or None
        try:
            self._failure_threshold = int(os.getenv("KEY_VALIDATION_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD)))
        except ValueError:
            self._failure_threshold = DEFAULT_FAILURE_THRESHOLD

    async def init(self) -> None:
        if self._ready:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._ready:
                return
            try:
                async with aiosqlite.connect(self._path) as db:
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_validation_attempts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            provider_id TEXT NOT NULL,
                            success BOOLEAN NOT NULL,
                            failure_type TEXT,
                            details TEXT,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_kva_provider_created ON key_validation_attempts(provider_id, created_at)"
                    )
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_validation_alerts_sent (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            provider_id TEXT NOT NULL,
                            alert_type TEXT NOT NULL,
                            sent_at TEXT NOT NULL,
                            UNIQUE(provider_id, alert_type)
                        )
                        """
                    )
                    await db.commit()
                self._ready = True
            except Exception as exc:
                logger.warning("KeyValidationMonitor init failed: %s", exc)

    async def record_attempt(
        self,
        provider_id: str,
        success: bool,
        failure_type: str | None = None,
        details: str = "",
    ) -> None:
        """Record a validation attempt."""
        await self.init()
        if not self._ready:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    "INSERT INTO key_validation_attempts (provider_id, success, failure_type, details, created_at) VALUES (?, ?, ?, ?, ?)",
                    (provider_id, success, failure_type or "", details, now),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record validation attempt: %s", exc)

        if not success and self._webhook_url:
            await self._check_and_alert(provider_id, failure_type or "unknown", details)

    async def _check_and_alert(self, provider_id: str, failure_type: str, details: str) -> None:
        """Check failure patterns and alert if threshold crossed."""
        # Count failures in last hour
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM key_validation_attempts WHERE provider_id = ? AND success = 0 AND created_at > ?",
                    (provider_id, one_hour_ago),
                )
                failure_count = int((await cursor.fetchone())[0])

                if failure_count >= self._failure_threshold:
                    await self._send_alert(provider_id, failure_type, failure_count, details)
        except Exception as exc:
            logger.warning("Failed to check failure patterns: %s", exc)

    async def _send_alert(self, provider_id: str, failure_type: str, failure_count: int, details: str) -> bool:
        """Send alert to configured webhook."""
        if not self._webhook_url:
            return False
        alert_type = f"validation_failures_{provider_id}"
        now = datetime.now(timezone.utc).isoformat()
        # Check if alert already sent recently (deduplication)
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT sent_at FROM key_validation_alerts_sent WHERE provider_id = ? AND alert_type = ?",
                    (provider_id, alert_type),
                )
                row = await cursor.fetchone()
                if row:
                    last_sent = datetime.fromisoformat(row[0])
                    minutes_since = (datetime.now(timezone.utc) - last_sent).total_seconds() / 60
                    if minutes_since < 60:  # Only alert once per hour per provider
                        return False
        except Exception as exc:
            logger.debug("alert dedup check failed, proceeding to send: %s", exc)
        payload: dict[str, Any] = {
            "event": "key_validation_failures",
            "provider_id": provider_id,
            "failure_type": failure_type,
            "failure_count_last_hour": failure_count,
            "threshold": self._failure_threshold,
            "details": details,
            "timestamp": now,
            "message": f"Key validation failed {failure_count} times for {provider_id} in the last hour. "
            f"Failure type: {failure_type}. Check key validity.",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
            # Record alert sent
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO key_validation_alerts_sent (provider_id, alert_type, sent_at) VALUES (?, ?, ?)",
                    (provider_id, alert_type, now),
                )
                await db.commit()
            logger.warning("Key validation alert sent for %s: %d failures", provider_id, failure_count)
            return True
        except Exception as exc:
            logger.warning("Failed to send key validation alert: %s", exc)
            return False

    async def get_stats(self, provider_id: str | None = None, hours: int = 24) -> dict[str, Any]:
        """Get validation statistics."""
        await self.init()
        if not self._ready:
            return {"error": "monitor_not_ready"}
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                if provider_id:
                    cursor = await db.execute(
                        "SELECT success, COUNT(*) FROM key_validation_attempts WHERE provider_id = ? AND created_at > ? GROUP BY success",
                        (provider_id, since),
                    )
                    rows = await cursor.fetchall()
                    stats = {("success" if row[0] else "failures"): row[1] for row in rows}
                    return {
                        "provider_id": provider_id,
                        "hours": hours,
                        "success": stats.get("success", 0),
                        "failures": stats.get("failures", 0),
                    }
                else:
                    cursor = await db.execute(
                        "SELECT provider_id, success, COUNT(*) FROM key_validation_attempts WHERE created_at > ? GROUP BY provider_id, success",
                        (since,),
                    )
                    rows = await cursor.fetchall()
                    by_provider: dict[str, dict[str, int]] = {}
                    for row in rows:
                        pid, success, count = row
                        if pid not in by_provider:
                            by_provider[pid] = {"success": 0, "failures": 0}
                        by_provider[pid]["success" if success else "failures"] = count
                    return {"hours": hours, "providers": by_provider}
        except Exception as exc:
            return {"error": str(exc)}

    async def cleanup_old_records(self, days: int = 7) -> int:
        """Remove records older than specified days. Returns deleted count."""
        await self.init()
        if not self._ready:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "DELETE FROM key_validation_attempts WHERE created_at < ?",
                    (cutoff,),
                )
                await db.commit()
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Failed to cleanup old records: %s", exc)
            return 0

    def get_config(self) -> dict[str, Any]:
        """Return current monitor configuration."""
        return {
            "webhook_configured": self._webhook_url is not None,
            "failure_threshold": self._failure_threshold,
            "store_ready": self._ready,
        }
