import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from pydantic import BaseModel

from gestaltworkframe.core.budget_alerts import BudgetAlertManager
from gestaltworkframe.core.session_cost_tracker import SessionCostTracker

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except ValueError:
        return default


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return max(float(os.getenv(name, str(default))), 0.0)
    except ValueError:
        return default


class CloudBudgetConfig(BaseModel):
    enabled: bool = False
    max_calls_per_turn: int = 0
    max_calls_per_session: int = 0
    max_calls_per_day: int = 0
    max_calls_per_month: int = 0
    max_daily_usd: float = 0.0
    max_monthly_usd: float = 0.0
    max_input_tokens_per_call: int = 0
    max_output_tokens_per_call: int = 0
    input_price_usd_per_million: float = 3.0
    output_price_usd_per_million: float = 15.0
    sqlite_path: str = "database.db"

    @classmethod
    def from_env(cls) -> "CloudBudgetConfig":
        return cls(
            enabled=_env_bool("ENABLE_CLOUD_SPILLOVER"),
            max_calls_per_turn=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_TURN"),
            max_calls_per_session=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_SESSION"),
            max_calls_per_day=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_DAY"),
            max_calls_per_month=_env_int("CLOUD_SPILLOVER_MAX_CALLS_PER_MONTH"),
            max_daily_usd=_env_float("CLOUD_SPILLOVER_MAX_DAILY_USD"),
            max_monthly_usd=_env_float("CLOUD_SPILLOVER_MAX_MONTHLY_USD"),
            max_input_tokens_per_call=_env_int("CLOUD_SPILLOVER_MAX_INPUT_TOKENS_PER_CALL"),
            max_output_tokens_per_call=_env_int("CLOUD_SPILLOVER_MAX_OUTPUT_TOKENS_PER_CALL"),
            input_price_usd_per_million=_env_float("CLOUD_SPILLOVER_INPUT_PRICE_USD_PER_MILLION", 3.0),
            output_price_usd_per_million=_env_float("CLOUD_SPILLOVER_OUTPUT_PRICE_USD_PER_MILLION", 15.0),
            sqlite_path=os.getenv("CLOUD_SPILLOVER_DB_PATH", "database.db"),
        )


class CloudBudgetDecision(BaseModel):
    allowed: bool
    reason: str
# Provider-scoped USD budget caps. These are additive to CloudBudgetConfig:
# the global gate still enforces call-count and token caps; per-provider
# configs enforce the USD spend limits for each API provider separately.
_PROVIDER_BUDGET_ENV_PREFIXES: dict[str, str] = {
    "openrouter": "OPENROUTER_BUDGET",
    "anthropic": "ANTHROPIC_BUDGET",
    "google": "GOOGLE_BUDGET",
    "openai": "OPENAI_BUDGET",
}


class ProviderBudgetConfig(BaseModel):
    provider_id: str
    enabled: bool = False
    max_daily_usd: float = 0.0
    max_monthly_usd: float = 0.0

    @classmethod
    def from_env(cls, provider_id: str) -> "ProviderBudgetConfig":
        prefix = _PROVIDER_BUDGET_ENV_PREFIXES.get(provider_id, provider_id.upper() + "_BUDGET")
        enabled = _env_bool(f"{prefix}_ENABLED")
        daily = _env_float(f"{prefix}_MAX_DAILY_USD")
        monthly = _env_float(f"{prefix}_MAX_MONTHLY_USD")
        return cls(provider_id=provider_id, enabled=enabled, max_daily_usd=daily, max_monthly_usd=monthly)


class CloudBudgetGate:
    def __init__(self, config: CloudBudgetConfig | None = None) -> None:
        self.config = config or CloudBudgetConfig()
        self._lock = asyncio.Lock()
        self._store_ready = False
        self._store_error = ""
        self._alert_manager = BudgetAlertManager(self.config.sqlite_path)
        self._session_cost_tracker = SessionCostTracker(self.config.sqlite_path)

    async def init(self) -> None:
        if self._store_ready:
            return
        if not self.config.enabled:
            return
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS cloud_budget_counter (key TEXT PRIMARY KEY, count INTEGER NOT NULL, updated_at TEXT NOT NULL)"
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_budget_usage_event (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        input_cost_usd REAL NOT NULL,
                        output_cost_usd REAL NOT NULL,
                        total_cost_usd REAL NOT NULL
                    )
                    """
                )
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS cloud_budget_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                await db.commit()
            self._store_ready = True
            self._store_error = ""
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__

    async def reserve(
        self,
        session_id: str | None,
        estimated_input_tokens: int = 0,
        requested_output_tokens: int = 0,
    ) -> CloudBudgetDecision:
        estimated_input_tokens = max(estimated_input_tokens, 0)
        requested_output_tokens = max(requested_output_tokens, 0)
        async with self._lock:
            if not self.config.enabled:
                return CloudBudgetDecision(allowed=False, reason="cloud_spillover_disabled")
            blocked = self._preflight_block_reason(estimated_input_tokens, requested_output_tokens)
            if blocked:
                return CloudBudgetDecision(allowed=False, reason=blocked)
            return await self._reserve_sqlite(session_id, estimated_input_tokens, requested_output_tokens)

    async def availability(
        self,
        estimated_input_tokens: int = 0,
        requested_output_tokens: int = 0,
    ) -> CloudBudgetDecision:
        estimated_input_tokens = max(estimated_input_tokens, 0)
        requested_output_tokens = max(requested_output_tokens, 0)
        if not self.config.enabled:
            return CloudBudgetDecision(allowed=False, reason="cloud_spillover_disabled")
        blocked = self._preflight_block_reason(estimated_input_tokens, requested_output_tokens)
        if blocked:
            return CloudBudgetDecision(allowed=False, reason=blocked)
        await self.init()
        if not self._store_ready:
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")
        if await self._accounting_blocked():
            return CloudBudgetDecision(allowed=False, reason="budget_accounting_blocked")
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                cursor = await db.execute("SELECT key, count FROM cloud_budget_counter WHERE key IN (?, ?)", (f"day:{self._current_day()}", f"month:{self._current_month()}"))
                rows = await cursor.fetchall()
                counts = {key: count for key, count in rows}
                used = await self._usage_from_db(db)
            blocked = self._cap_exhausted_reason(
                0,
                counts.get(f"day:{self._current_day()}", 0),
                counts.get(f"month:{self._current_month()}", 0),
                used["day_usd"],
                used["month_usd"],
                estimated_input_tokens,
                requested_output_tokens,
            )
            if blocked:
                return CloudBudgetDecision(allowed=False, reason=blocked)
            return CloudBudgetDecision(allowed=True, reason="within_budget")
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")

    async def record_usage(
        self,
        session_id: str | None,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        input_price_usd_per_million: float | None = None,
        output_price_usd_per_million: float | None = None,
    ) -> CloudBudgetDecision:
        if not self.config.enabled:
            return CloudBudgetDecision(allowed=True, reason="cloud_spillover_disabled")
        await self.init()
        if input_tokens is None or output_tokens is None:
            await self._set_accounting_block("missing_usage_metadata")
            return CloudBudgetDecision(allowed=False, reason="missing_usage_metadata")
        if input_tokens < 0 or output_tokens < 0:
            await self._set_accounting_block("invalid_usage_metadata")
            return CloudBudgetDecision(allowed=False, reason="invalid_usage_metadata")
        if not self._store_ready:
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                now = datetime.now(timezone.utc).isoformat()
                input_cost = self._input_cost(input_tokens, input_price_usd_per_million)
                output_cost = self._output_cost(output_tokens, output_price_usd_per_million)
                total_cost = input_cost + output_cost
                await db.execute(
                    """
                    INSERT INTO cloud_budget_usage_event (
                        id, created_at, session_id, provider, model, input_tokens, output_tokens,
                        input_cost_usd, output_cost_usd, total_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()), now, session_id or "anonymous", provider, model,
                        input_tokens, output_tokens, input_cost, output_cost, total_cost,
                    ),
                )
                await db.commit()
                # Check budget thresholds and alert
                used = await self._usage_from_db(db)
                if self.config.max_daily_usd > 0:
                    await self._alert_manager.check_and_alert("cloud_spillover", self.config.max_daily_usd, used["day_usd"])
                if self.config.max_monthly_usd > 0:
                    await self._alert_manager.check_and_alert("cloud_spillover", self.config.max_monthly_usd, used["month_usd"])
                # Record session cost attribution
                await self._session_cost_tracker.record_cost(
                    session_id=session_id or "anonymous",
                    provider_id=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    input_cost_usd=input_cost,
                    output_cost_usd=output_cost,
                )
            return CloudBudgetDecision(allowed=True, reason="usage_recorded")
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            await self._set_accounting_block("usage_accounting_failed")
            return CloudBudgetDecision(allowed=False, reason="usage_accounting_failed")

    async def snapshot(self) -> dict[str, object]:
        store = "sqlite" if self.config.enabled else "memory"
        used = await self._sqlite_usage() if self.config.enabled else {
            "sessions": 0, "day": 0, "month": 0, "day_usd": 0.0, "month_usd": 0.0,
        }
        return {
            "configured": True,
            "enabled": self.config.enabled,
            "store": store,
            "store_ready": self._store_ready if self.config.enabled else True,
            "store_error": self._store_error,
            "accounting_blocked": await self._accounting_blocked() if self.config.enabled else False,
            "last_accounting_error": await self._state_value("last_accounting_error") if self.config.enabled else "",
            "limits": self.config.model_dump(exclude={"enabled", "sqlite_path"}),
            "used": used,
            "alerts": self._alert_manager.get_config(),
            "session_cost": self._session_cost_tracker.get_config(),
        }

    async def _reserve_sqlite(
        self,
        session_id: str | None,
        estimated_input_tokens: int,
        requested_output_tokens: int,
    ) -> CloudBudgetDecision:
        await self.init()
        if not self._store_ready:
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")
        if await self._accounting_blocked():
            return CloudBudgetDecision(allowed=False, reason="budget_accounting_blocked")

        session_key = self._session_key(session_id)
        day_key = f"day:{self._current_day()}"
        month_key = f"month:{self._current_month()}"
        keys = [session_key, day_key, month_key]

        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                cursor = await db.execute("SELECT key, count FROM cloud_budget_counter WHERE key IN (?, ?, ?)", keys)
                rows = await cursor.fetchall()
                counts = {key: count for key, count in rows}
                used = await self._usage_from_db(db)
                blocked = self._cap_exhausted_reason(
                    counts.get(session_key, 0), counts.get(day_key, 0), counts.get(month_key, 0),
                    used["day_usd"], used["month_usd"], estimated_input_tokens, requested_output_tokens,
                )
                if blocked:
                    return CloudBudgetDecision(allowed=False, reason=blocked)

                now = datetime.now(timezone.utc).isoformat()
                for key in keys:
                    await db.execute(
                        """
                        INSERT INTO cloud_budget_counter (key, count, updated_at)
                        VALUES (?, 1, ?)
                        ON CONFLICT(key) DO UPDATE SET count = count + 1, updated_at = excluded.updated_at
                        """,
                        (key, now),
                    )
                await db.commit()
            return CloudBudgetDecision(allowed=True, reason="within_budget")
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            await self._set_accounting_block("reservation_failed")
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")

    def _preflight_block_reason(self, estimated_input_tokens: int, requested_output_tokens: int) -> str:
        if self.config.max_calls_per_turn > 0 and self.config.max_calls_per_turn < 1:
            return "call_cap_zero"
        if self.config.max_daily_usd == 0 and self.config.max_monthly_usd == 0:
            return ""
        if self.config.max_input_tokens_per_call > 0 and estimated_input_tokens > self.config.max_input_tokens_per_call:
            return "input_token_cap_exceeded"
        if self.config.max_output_tokens_per_call > 0 and requested_output_tokens > self.config.max_output_tokens_per_call:
            return "output_token_cap_exceeded"
        return ""

    def _cap_exhausted_reason(
        self,
        session_calls: int,
        day_calls: int,
        month_calls: int,
        day_usd: float,
        month_usd: float,
        estimated_input_tokens: int,
        requested_output_tokens: int,
    ) -> str:
        if self.config.max_calls_per_session > 0 and session_calls >= self.config.max_calls_per_session:
            return "session_call_cap_exhausted"
        if self.config.max_calls_per_day > 0 and day_calls >= self.config.max_calls_per_day:
            return "daily_call_cap_exhausted"
        if self.config.max_calls_per_month > 0 and month_calls >= self.config.max_calls_per_month:
            return "monthly_call_cap_exhausted"
        # Check USD caps against actual usage + estimated cost of this request
        est_cost = self._estimate_cost(estimated_input_tokens, requested_output_tokens)
        if self.config.max_daily_usd > 0 and (day_usd + est_cost) > self.config.max_daily_usd:
            return "daily_usd_cap_exhausted"
        if self.config.max_monthly_usd > 0 and (month_usd + est_cost) > self.config.max_monthly_usd:
            return "monthly_usd_cap_exhausted"
        return ""

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_cost = self._input_cost(input_tokens, self.config.input_price_usd_per_million)
        output_cost = self._output_cost(output_tokens, self.config.output_price_usd_per_million)
        return input_cost + output_cost

    def _input_cost(self, tokens: int, price_per_million: float | None = None) -> float:
        price = price_per_million if price_per_million is not None else self.config.input_price_usd_per_million
        return tokens * price / 1_000_000

    def _output_cost(self, tokens: int, price_per_million: float | None = None) -> float:
        price = price_per_million if price_per_million is not None else self.config.output_price_usd_per_million
        return tokens * price / 1_000_000

    async def _accounting_blocked(self) -> bool:
        err = await self._state_value("last_accounting_error")
        return bool(err)

    async def _set_accounting_block(self, reason: str) -> None:
        if not self.config.enabled:
            return
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    ("last_accounting_error", reason, now),
                )
                await db.commit()
        except Exception:
            pass

    async def _state_value(self, key: str) -> str:
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                cursor = await db.execute("SELECT value FROM cloud_budget_state WHERE key = ?", (key,))
                row = await cursor.fetchone()
                return row[0] if row else ""
        except Exception:
            return ""

    async def _usage_from_db(self, db: aiosqlite.Connection) -> dict[str, Any]:
        today = self._current_day()
        month = self._current_month()
        cursor = await db.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE created_at >= ?",
            (f"{today}T00:00:00+00:00",),
        )
        day_usd = float((await cursor.fetchone())[0])
        cursor = await db.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE created_at >= ?",
            (f"{month}-01T00:00:00+00:00",),
        )
        month_usd = float((await cursor.fetchone())[0])
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT session_id) FROM cloud_budget_usage_event WHERE created_at >= ?",
            (f"{today}T00:00:00+00:00",),
        )
        sessions = int((await cursor.fetchone())[0])
        return {"sessions": sessions, "day_usd": day_usd, "month_usd": month_usd}

    async def _sqlite_usage(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"sessions": 0, "day": 0, "month": 0, "day_usd": 0.0, "month_usd": 0.0}
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                day = self._current_day()
                month = self._current_month()
                cursor = await db.execute("SELECT COALESCE(SUM(count), 0) FROM cloud_budget_counter WHERE key = ?", (f"day:{day}",))
                day_calls = int((await cursor.fetchone())[0])
                cursor = await db.execute("SELECT COALESCE(SUM(count), 0) FROM cloud_budget_counter WHERE key = ?", (f"month:{month}",))
                month_calls = int((await cursor.fetchone())[0])
                used = await self._usage_from_db(db)
                return {
                    "sessions": used["sessions"],
                    "day": day_calls,
                    "month": month_calls,
                    "day_usd": used["day_usd"],
                    "month_usd": used["month_usd"],
                }
        except Exception:
            return {"sessions": 0, "day": 0, "month": 0, "day_usd": 0.0, "month_usd": 0.0}

    def _current_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _current_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _session_key(self, session_id: str | None) -> str:
        return f"session:{session_id or 'anonymous'}"



    async def clear_accounting_block(self) -> CloudBudgetDecision:
        """Clear the accounting blocked flag."""
        if not self.config.enabled:
            return CloudBudgetDecision(allowed=False, reason="cloud_spillover_disabled")
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    ("last_accounting_error", "", now),
                )
                await db.commit()
            return CloudBudgetDecision(allowed=True, reason="accounting_block_cleared")
        except Exception as exc:
            return CloudBudgetDecision(allowed=False, reason=f"clear_failed: {exc}")
class ProviderBudgetGate:
    """Per-provider USD budget gate. Used by LLMRouter to enforce provider-specific caps."""

    def __init__(self, provider_id: str, config: ProviderBudgetConfig, sqlite_path: str = "database.db") -> None:
        self.provider_id = provider_id
        self.config = config
        self._sqlite_path = sqlite_path
        self._alert_manager = BudgetAlertManager(sqlite_path)
        self._session_cost_tracker = SessionCostTracker(sqlite_path)

    def is_enabled(self) -> bool:
        return self.config.enabled and (self.config.max_daily_usd > 0 or self.config.max_monthly_usd > 0)

    async def check_and_record(
        self,
        session_id: str | None,
        input_tokens: int,
        output_tokens: int,
        input_price_usd_per_million: float | None = None,
        output_price_usd_per_million: float | None = None,
        model: str = "unknown",
        check_only: bool = False,
    ) -> CloudBudgetDecision:
        if not self.is_enabled():
            return CloudBudgetDecision(allowed=True, reason="provider_budget_disabled")

        input_cost = (input_tokens * (input_price_usd_per_million or 3.0)) / 1_000_000
        output_cost = (output_tokens * (output_price_usd_per_million or 15.0)) / 1_000_000
        total_cost = input_cost + output_cost

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        try:
            async with aiosqlite.connect(self._sqlite_path) as db:
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE provider = ? AND created_at >= ?",
                    (self.provider_id, f"{today}T00:00:00+00:00"),
                )
                day_usd = float((await cursor.fetchone())[0]) + total_cost
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE provider = ? AND created_at >= ?",
                    (self.provider_id, f"{month}-01T00:00:00+00:00"),
                )
                month_usd = float((await cursor.fetchone())[0]) + total_cost

            if self.config.max_daily_usd > 0 and day_usd > self.config.max_daily_usd:
                return CloudBudgetDecision(allowed=False, reason="provider_daily_usd_cap_exhausted")
            if self.config.max_monthly_usd > 0 and month_usd > self.config.max_monthly_usd:
                return CloudBudgetDecision(allowed=False, reason="provider_monthly_usd_cap_exhausted")

            # Record usage and check alerts (skip if check_only)
            if check_only:
                return CloudBudgetDecision(allowed=True, reason="within_provider_budget")
            now = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(self._sqlite_path) as db:
                await db.execute(
                    """
                    INSERT INTO cloud_budget_usage_event (
                        id, created_at, session_id, provider, model, input_tokens, output_tokens,
                        input_cost_usd, output_cost_usd, total_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), now, session_id or "provider_gate", self.provider_id, model, input_tokens, output_tokens, input_cost, output_cost, total_cost),
                )
                await db.commit()

            # Fire alerts if thresholds crossed
            if self.config.max_daily_usd > 0:
                await self._alert_manager.check_and_alert(self.provider_id, self.config.max_daily_usd, day_usd)
            if self.config.max_monthly_usd > 0:
                await self._alert_manager.check_and_alert(self.provider_id, self.config.max_monthly_usd, month_usd)

            # Record session cost attribution
            await self._session_cost_tracker.record_cost(
                session_id=session_id or "anonymous",
                provider_id=self.provider_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
            )

            return CloudBudgetDecision(allowed=True, reason="within_provider_budget")
        except Exception as exc:
            logger.warning("ProviderBudgetGate check failed: %s", exc)
            return CloudBudgetDecision(allowed=False, reason="provider_budget_check_failed")

    async def snapshot(self) -> dict[str, Any]:
        if not self.is_enabled():
            return {
                "provider_id": self.provider_id,
                "enabled": False,
                "max_daily_usd": self.config.max_daily_usd,
                "max_monthly_usd": self.config.max_monthly_usd,
                "limits": {"max_daily_usd": self.config.max_daily_usd, "max_monthly_usd": self.config.max_monthly_usd},
                "used": {"day_usd": 0.0, "month_usd": 0.0},
            }
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            async with aiosqlite.connect(self._sqlite_path) as db:
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE provider = ? AND created_at >= ?",
                    (self.provider_id, f"{today}T00:00:00+00:00"),
                )
                day_usd = float((await cursor.fetchone())[0])
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE provider = ? AND created_at >= ?",
                    (self.provider_id, f"{month}-01T00:00:00+00:00"),
                )
                month_usd = float((await cursor.fetchone())[0])
            return {
                "provider_id": self.provider_id,
                "enabled": True,
                "max_daily_usd": self.config.max_daily_usd,
                "max_monthly_usd": self.config.max_monthly_usd,
                "limits": {"max_daily_usd": self.config.max_daily_usd, "max_monthly_usd": self.config.max_monthly_usd},
                "used": {"day_usd": day_usd, "month_usd": month_usd},
                "alerts": self._alert_manager.get_config(),
                "session_cost": self._session_cost_tracker.get_config(),
            }
        except Exception:
            return {
                "provider_id": self.provider_id,
                "enabled": True,
                "max_daily_usd": self.config.max_daily_usd,
                "max_monthly_usd": self.config.max_monthly_usd,
                "limits": {"max_daily_usd": self.config.max_daily_usd, "max_monthly_usd": self.config.max_monthly_usd},
                "used": {"day_usd": 0.0, "month_usd": 0.0},
                "error": "snapshot_failed",
            }



class MultiProviderBudgetGate:
    """Multi-provider budget gate that manages per-provider and global budget gates."""

    def __init__(
        self,
        global_gate: CloudBudgetGate | None = None,
        provider_configs: dict[str, ProviderBudgetConfig] | None = None,
        sqlite_path: str = "database.db",
    ) -> None:
        self.global_gate = global_gate or CloudBudgetConfig()
        # Use global gate's sqlite_path if available, otherwise use the passed sqlite_path
        if isinstance(self.global_gate, CloudBudgetGate):
            self._sqlite_path = self.global_gate.config.sqlite_path
        else:
            self._sqlite_path = sqlite_path
        self._gates: dict[str, ProviderBudgetGate] = {}
        self._headroom_cache: dict[str, float] = {}
        self.provider_configs = provider_configs or {}
        if provider_configs:
            for pid, cfg in provider_configs.items():
                self._gates[pid] = ProviderBudgetGate(pid, cfg, self._sqlite_path)

    @property
    def config(self) -> CloudBudgetConfig:
        """Return the global gate config for backward compatibility."""
        if isinstance(self.global_gate, CloudBudgetGate):
            return self.global_gate.config
        return CloudBudgetConfig()

    @classmethod
    def from_env(cls, global_gate: CloudBudgetGate | None = None) -> "MultiProviderBudgetGate":
        """Create a multi-provider gate from environment variables."""
        configs: dict[str, ProviderBudgetConfig] = {}
        for provider_id in ["openrouter", "anthropic", "google", "openai"]:
            cfg = ProviderBudgetConfig.from_env(provider_id)
            if cfg.enabled:
                configs[provider_id] = cfg
        sqlite_path = "database.db"
        if global_gate and hasattr(global_gate, "config"):
            sqlite_path = getattr(global_gate.config, "sqlite_path", sqlite_path)
        return cls(global_gate=global_gate, provider_configs=configs, sqlite_path=sqlite_path)

    @property
    def provider_gates(self) -> dict[str, ProviderBudgetGate]:
        """Return the dict of provider budget gates."""
        return self._gates

    def gate_for(self, provider_id: str | None) -> CloudBudgetGate | ProviderBudgetGate:
        """Return the budget gate for a provider, or the global gate if no specific gate exists."""
        if provider_id and provider_id in self._gates:
            return self._gates[provider_id]
        if isinstance(self.global_gate, CloudBudgetGate):
            return self.global_gate
        return CloudBudgetGate()

    def headroom(self, provider_id: str) -> float:
        """Return cached headroom (0.0-1.0) for a provider. Returns 1.0 if not configured."""
        return self._headroom_cache.get(provider_id, 1.0)

    async def refresh_headroom_cache(self) -> None:
        """Refresh the headroom cache for all configured providers."""
        for pid, gate in self._gates.items():
            if not gate.is_enabled():
                self._headroom_cache[pid] = 1.0
                continue
            try:
                snap = await gate.snapshot()
                limits = snap.get("limits", {})
                used = snap.get("used", {})
                max_daily = limits.get("max_daily_usd", 0.0)
                day_used = used.get("day_usd", 0.0)
                if max_daily > 0:
                    self._headroom_cache[pid] = max(0.0, 1.0 - (day_used / max_daily))
                else:
                    self._headroom_cache[pid] = 1.0
            except Exception:
                self._headroom_cache[pid] = 1.0

    async def record_usage(
        self,
        session_id: str | None,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        input_price_usd_per_million: float | None = None,
        output_price_usd_per_million: float | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Record usage for a provider if it has a configured gate."""
        # Use provider_id if given, otherwise fall back to provider string
        gate_key = provider_id if provider_id else provider
        gate = self._gates.get(gate_key)
        if gate:
            await gate.check_and_record(
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_price_usd_per_million=input_price_usd_per_million,
                output_price_usd_per_million=output_price_usd_per_million,
                model=model,
            )

    async def update_provider_budget(
        self,
        provider_id: str,
        max_daily_usd: float | None = None,
        max_monthly_usd: float | None = None,
    ) -> None:
        """Update budget caps for a provider at runtime."""
        if provider_id in self._gates:
            gate = self._gates[provider_id]
            if max_daily_usd is not None:
                gate.config.max_daily_usd = max_daily_usd
            if max_monthly_usd is not None:
                gate.config.max_monthly_usd = max_monthly_usd
        else:
            # Create new gate if not exists
            cfg = ProviderBudgetConfig(
                provider_id=provider_id,
                enabled=True,
                max_daily_usd=max_daily_usd or 0.0,
                max_monthly_usd=max_monthly_usd or 0.0,
            )
            self._gates[provider_id] = ProviderBudgetGate(provider_id, cfg, self._sqlite_path)

    async def snapshot(self) -> dict[str, Any]:
        """Return a combined snapshot of global and all provider budgets."""
        global_snap = await self.global_gate.snapshot() if isinstance(self.global_gate, CloudBudgetGate) else {}
        provider_snaps = {}
        for pid, gate in self._gates.items():
            provider_snaps[pid] = await gate.snapshot()
        return {
            "configured": True,
            "enabled": global_snap.get("enabled", False),
            "store": global_snap.get("store", "memory"),
            "store_ready": global_snap.get("store_ready", True),
            "limits": global_snap.get("limits", {}),
            "used": global_snap.get("used", {}),
            "providers": provider_snaps,
        }

    async def clear_accounting_block(self) -> CloudBudgetDecision:
        """Clear the accounting blocked flag on the global gate."""
        if isinstance(self.global_gate, CloudBudgetGate):
            try:
                async with aiosqlite.connect(self.global_gate.config.sqlite_path) as db:
                    now = datetime.now(timezone.utc).isoformat()
                    await db.execute(
                        "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        ("last_accounting_error", "", now),
                    )
                    await db.commit()
                return CloudBudgetDecision(allowed=True, reason="accounting_block_cleared")
            except Exception as exc:
                return CloudBudgetDecision(allowed=False, reason=f"clear_failed: {exc}")
        return CloudBudgetDecision(allowed=True, reason="no_global_gate")


    async def init(self) -> None:
        """Initialize the multi-provider budget gate by initializing the global gate."""
        if isinstance(self.global_gate, CloudBudgetGate):
            await self.global_gate.init()

    def provider_config(self, provider_id: str) -> ProviderBudgetConfig | None:
        """Return the budget config for a provider, or None if not configured."""
        if provider_id in self._gates:
            return self._gates[provider_id].config
        return None

    def provider_gate(self, provider_id: str):
        """Return the budget gate for a provider, or None if not configured."""
        return self._gates.get(provider_id)

    async def reserve(
        self,
        session_id: str,
        estimated_input_tokens: int = 0,
        requested_output_tokens: int = 0,
        provider_id: str | None = None,
    ) -> CloudBudgetDecision:
        """Reserve budget for a session. Uses provider-specific gate if available, otherwise global."""
        # Initialize global gate first to ensure DB tables exist
        if isinstance(self.global_gate, CloudBudgetGate):
            await self.global_gate.init()

        # Check global gate first
        if isinstance(self.global_gate, CloudBudgetGate):
            global_decision = await self.global_gate.reserve(session_id, estimated_input_tokens, requested_output_tokens)
            if not global_decision.allowed:
                return global_decision

        # Check provider-specific gate if provider_id given
        if provider_id and provider_id in self._gates:
            gate = self._gates[provider_id]
            # Provider gates use check_and_record for availability
            decision = await gate.check_and_record(
                session_id=session_id,
                input_tokens=estimated_input_tokens,
                output_tokens=requested_output_tokens,
                check_only=True,
            )
            if not decision.allowed:
                return decision

        return CloudBudgetDecision(allowed=True, reason="within_budget")
