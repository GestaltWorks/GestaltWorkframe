"""Session-level cost attribution tracking.

Tracks cloud spend per conversation/session so operators can understand
which sessions incur costs, monitor unusual spend patterns, and attribute
expenses to specific use cases.

Environment:
  SESSION_COST_ALERT_THRESHOLD_USD  - Alert when single session exceeds this (default: 5.0)
  SESSION_COST_WEBHOOK_URL          - Optional webhook for high-cost session alerts
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

DEFAULT_ALERT_THRESHOLD_USD = 5.0


@dataclass
class SessionCost:
    session_id: str
    provider_id: str
    model: str
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    timestamp: str


class SessionCostTracker:
    """Tracks and reports costs per session."""

    def __init__(self, sqlite_path: str = "database.db") -> None:
        self._path = sqlite_path
        self._ready = False
        self._lock: asyncio.Lock | None = None
        self._alert_threshold_usd: float = DEFAULT_ALERT_THRESHOLD_USD
        self._webhook_url: str | None = None
        self._load_config()

    def _load_config(self) -> None:
        self._webhook_url = os.getenv("SESSION_COST_WEBHOOK_URL", "").strip() or None
        try:
            self._alert_threshold_usd = float(os.getenv("SESSION_COST_ALERT_THRESHOLD_USD", str(DEFAULT_ALERT_THRESHOLD_USD)))
        except ValueError:
            self._alert_threshold_usd = DEFAULT_ALERT_THRESHOLD_USD

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
                        CREATE TABLE IF NOT EXISTS session_cost_event (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL,
                            provider_id TEXT NOT NULL,
                            model TEXT NOT NULL,
                            input_tokens INTEGER NOT NULL,
                            output_tokens INTEGER NOT NULL,
                            input_cost_usd REAL NOT NULL,
                            output_cost_usd REAL NOT NULL,
                            total_cost_usd REAL NOT NULL,
                            created_at TEXT NOT NULL
                        )
                        """
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_sce_session ON session_cost_event(session_id, created_at)"
                    )
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_sce_created ON session_cost_event(created_at)"
                    )
                    await db.execute(
                        """
                        CREATE TABLE IF NOT EXISTS session_cost_alert_sent (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            session_id TEXT NOT NULL UNIQUE,
                            threshold_usd REAL NOT NULL,
                            sent_at TEXT NOT NULL
                        )
                        """
                    )
                    await db.commit()
                self._ready = True
            except Exception as exc:
                logger.warning("SessionCostTracker init failed: %s", exc)

    async def record_cost(
        self,
        session_id: str,
        provider_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        input_cost_usd: float,
        output_cost_usd: float,
    ) -> None:
        """Record a cost event for a session."""
        await self.init()
        if not self._ready:
            return
        total_cost = input_cost_usd + output_cost_usd
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                await db.execute(
                    """
                    INSERT INTO session_cost_event
                    (session_id, provider_id, model, input_tokens, output_tokens, input_cost_usd, output_cost_usd, total_cost_usd, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, provider_id, model, input_tokens, output_tokens, input_cost_usd, output_cost_usd, total_cost, now),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record session cost: %s", exc)
            return

        # Check threshold and alert
        if self._webhook_url and total_cost >= self._alert_threshold_usd:
            await self._check_and_alert(session_id, total_cost)

    async def _check_and_alert(self, session_id: str, this_call_cost: float) -> None:
        """Check if session total exceeds threshold and alert once."""
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(total_cost_usd), 0) FROM session_cost_event WHERE session_id = ?",
                    (session_id,),
                )
                session_total = float((await cursor.fetchone())[0])

                if session_total < self._alert_threshold_usd:
                    return

                # Check if already alerted
                cursor = await db.execute(
                    "SELECT 1 FROM session_cost_alert_sent WHERE session_id = ?",
                    (session_id,),
                )
                if await cursor.fetchone():
                    return

                await self._send_alert(session_id, session_total, this_call_cost)

                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO session_cost_alert_sent (session_id, threshold_usd, sent_at) VALUES (?, ?, ?)",
                    (session_id, self._alert_threshold_usd, now),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to check session cost threshold: %s", exc)

    async def _send_alert(self, session_id: str, session_total: float, this_call_cost: float) -> bool:
        """Send high-cost session alert to webhook."""
        if not self._webhook_url:
            return False
        payload: dict[str, Any] = {
            "event": "high_session_cost",
            "session_id": session_id,
            "session_total_usd": round(session_total, 4),
            "this_call_cost_usd": round(this_call_cost, 4),
            "threshold_usd": self._alert_threshold_usd,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Session {session_id[:16]}... exceeded cost threshold: ${session_total:.2f} "
            f"(threshold: ${self._alert_threshold_usd:.2f})",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
            logger.warning("High session cost alert sent for %s: $%.2f", session_id[:16], session_total)
            return True
        except Exception as exc:
            logger.warning("Failed to send session cost alert: %s", exc)
            return False

    async def get_session_summary(self, session_id: str) -> dict[str, Any]:
        """Get cost summary for a specific session."""
        await self.init()
        if not self._ready:
            return {"error": "tracker_not_ready"}
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    """
                    SELECT
                        COUNT(*) as call_count,
                        COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                        COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                        COALESCE(SUM(input_cost_usd), 0) as total_input_cost,
                        COALESCE(SUM(output_cost_usd), 0) as total_output_cost,
                        COALESCE(SUM(total_cost_usd), 0) as total_cost,
                        MIN(created_at) as first_call,
                        MAX(created_at) as last_call
                    FROM session_cost_event
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
                row = await cursor.fetchone()
                if row is None or row[0] == 0:
                    return {"session_id": session_id, "calls": 0, "total_cost_usd": 0.0}
                return {
                    "session_id": session_id,
                    "calls": row[0],
                    "input_tokens": int(row[1]),
                    "output_tokens": int(row[2]),
                    "input_cost_usd": round(float(row[3]), 6),
                    "output_cost_usd": round(float(row[4]), 6),
                    "total_cost_usd": round(float(row[5]), 6),
                    "first_call": row[6],
                    "last_call": row[7],
                }
        except Exception as exc:
            return {"error": str(exc)}

    async def get_top_sessions(
        self,
        hours: int = 24,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get top sessions by cost in the last N hours."""
        await self.init()
        if not self._ready:
            return []
        since = datetime.now(timezone.utc).replace(hour=datetime.now(timezone.utc).hour - hours).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    """
                    SELECT
                        session_id,
                        COUNT(*) as call_count,
                        COALESCE(SUM(total_cost_usd), 0) as total_cost,
                        MAX(created_at) as last_call
                    FROM session_cost_event
                    WHERE created_at > ?
                    GROUP BY session_id
                    ORDER BY total_cost DESC
                    LIMIT ?
                    """,
                    (since, limit),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "session_id": row[0],
                        "calls": row[1],
                        "total_cost_usd": round(float(row[2]), 6),
                        "last_call": row[3],
                    }
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("Failed to get top sessions: %s", exc)
            return []

    async def cleanup_old_records(self, days: int = 30) -> int:
        """Remove records older than specified days. Returns deleted count."""
        await self.init()
        if not self._ready:
            return 0
        cutoff = datetime.now(timezone.utc).replace(day=datetime.now(timezone.utc).day - days).isoformat()
        try:
            async with aiosqlite.connect(self._path) as db:
                cursor = await db.execute(
                    "DELETE FROM session_cost_event WHERE created_at < ?",
                    (cutoff,),
                )
                await db.execute(
                    "DELETE FROM session_cost_alert_sent WHERE sent_at < ?",
                    (cutoff,),
                )
                await db.commit()
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Failed to cleanup old session cost records: %s", exc)
            return 0

    def get_config(self) -> dict[str, Any]:
        """Return current tracker configuration."""
        return {
            "webhook_configured": self._webhook_url is not None,
            "alert_threshold_usd": self._alert_threshold_usd,
            "store_ready": self._ready,
        }
