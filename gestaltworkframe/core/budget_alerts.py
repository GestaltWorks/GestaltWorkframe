"""Budget threshold alerting system.

Checks provider budgets against configured thresholds (80%, 100%) and emits
webhook notifications when thresholds are crossed. Tracks fired alerts to
prevent spam (alerts fire once per threshold crossing until reset).

Environment:
  BUDGET_ALERT_WEBHOOK_URL   - Optional webhook endpoint for notifications
  BUDGET_ALERT_THRESHOLDS    - Comma-separated percentages (default: 80,100)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS = [80, 100]


@dataclass
class BudgetAlert:
    provider_id: str
    threshold_pct: int
    limit_usd: float
    used_usd: float
    remaining_usd: float
    fired_at: str
    message: str


class BudgetAlertManager:
    """Manages budget threshold checks and webhook notifications."""

    def __init__(self, sqlite_path: str = "database.db") -> None:
        self._path = sqlite_path
        self._ready = False
        self._lock: asyncio.Lock | None = None
        self._webhook_url: str | None = None
        self._thresholds: list[int] = DEFAULT_THRESHOLDS
        self._load_config()

    def _load_config(self) -> None:
        self._webhook_url = os.getenv("BUDGET_ALERT_WEBHOOK_URL", "").strip() or None
        thresholds_str = os.getenv("BUDGET_ALERT_THRESHOLDS", "80,100")
        try:
            self._thresholds = [int(x.strip()) for x in thresholds_str.split(",") if x.strip()]
            self._thresholds = sorted(set(max(0, min(100, t)) for t in self._thresholds))
        except Exception:
            self._thresholds = DEFAULT_THRESHOLDS

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
                        CREATE TABLE IF NOT EXISTS budget_alert_fired (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            provider_id TEXT NOT NULL,
                            threshold_pct INTEGER NOT NULL,
                            fired_at TEXT NOT NULL,
                            UNIQUE(provider_id, threshold_pct)
                        )
                        """
                    )
                    await db.commit()
                self._ready = True
            except Exception as exc:
                logger.warning("BudgetAlertManager init failed: %s", exc)

    async def check_and_alert(
        self,
        provider_id: str,
        limit_usd: float,
        used_usd: float,
    ) -> list[BudgetAlert]:
        """Check thresholds and fire alerts for any crossed. Returns fired alerts."""
        await self.init()
        if not self._ready or not self._webhook_url:
            return []
        if limit_usd <= 0:
            return []

        pct_used = min(100.0, (used_usd / limit_usd) * 100)
        remaining = max(0.0, limit_usd - used_usd)
        fired: list[BudgetAlert] = []

        for threshold in self._thresholds:
            if pct_used >= threshold:
                if await self._should_fire(provider_id, threshold):
                    alert = BudgetAlert(
                        provider_id=provider_id,
                        threshold_pct=threshold,
                        limit_usd=limit_usd,
                        used_usd=used_usd,
                        remaining_usd=remaining,
                        fired_at=datetime.now(timezone.utc).isoformat(),
                        message=f"Budget {threshold}% threshold crossed for {provider_id}: "
                        f"${used_usd:.2f} / ${limit_usd:.2f} used, ${remaining:.2f} remaining",
                    )
                    await self._send_alert(alert)
                    await self._record_fired(provider_id, threshold)
                    fired.append(alert)

        return fired

    async def _should_fire(self, provider_id: str, threshold: int) -> bool:
        """Check if this alert hasn't been fired yet today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT fired_at FROM budget_alert_fired WHERE provider_id = ? AND threshold_pct = ?",
                    (provider_id, threshold),
                )
                row = await cursor.fetchone()
                if row is None:
                    return True
                # Re-fire if it's a new day
                fired_date = row[0][:10] if row[0] else ""
                return fired_date != today
        except Exception as exc:
            logger.warning("Alert check failed: %s", exc)
            return False

    async def _record_fired(self, provider_id: str, threshold: int) -> None:
        """Record that an alert was fired."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    """
                    INSERT INTO budget_alert_fired (provider_id, threshold_pct, fired_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(provider_id, threshold_pct) DO UPDATE SET fired_at = excluded.fired_at
                    """,
                    (provider_id, threshold, now),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record alert: %s", exc)

    async def _send_alert(self, alert: BudgetAlert) -> bool:
        """Send alert to configured webhook."""
        if not self._webhook_url:
            return False
        payload: dict[str, Any] = {
            "event": "budget_threshold_crossed",
            "provider_id": alert.provider_id,
            "threshold_pct": alert.threshold_pct,
            "limit_usd": alert.limit_usd,
            "used_usd": alert.used_usd,
            "remaining_usd": alert.remaining_usd,
            "fired_at": alert.fired_at,
            "message": alert.message,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
            logger.info("Budget alert sent: %s", alert.message)
            return True
        except Exception as exc:
            logger.warning("Failed to send budget alert: %s", exc)
            return False

    async def reset_alerts(self, provider_id: str | None = None) -> int:
        """Reset fired alerts (e.g., after budget increase). Returns cleared count."""
        await self.init()
        if not self._ready:
            return 0
        try:
            async with aiosqlite.connect(self._path) as db:
                if provider_id:
                    cursor = await db.execute(
                        "DELETE FROM budget_alert_fired WHERE provider_id = ?",
                        (provider_id,),
                    )
                else:
                    cursor = await db.execute("DELETE FROM budget_alert_fired")
                await db.commit()
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Failed to reset alerts: %s", exc)
            return 0

    def get_config(self) -> dict[str, Any]:
        """Return current alert configuration."""
        return {
            "webhook_configured": self._webhook_url is not None,
            "thresholds": self._thresholds,
            "store_ready": self._ready,
        }
