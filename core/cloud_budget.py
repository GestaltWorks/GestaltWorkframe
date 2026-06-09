import asyncio
import os
import uuid
from datetime import datetime, timezone

import aiosqlite
from pydantic import BaseModel


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
        if input_tokens is None or output_tokens is None:
            await self._set_accounting_block("missing_usage_metadata")
            return CloudBudgetDecision(allowed=False, reason="missing_usage_metadata")
        if input_tokens < 0 or output_tokens < 0:
            await self._set_accounting_block("invalid_usage_metadata")
            return CloudBudgetDecision(allowed=False, reason="invalid_usage_metadata")
        await self.init()
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
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")

    async def _sqlite_usage(self) -> dict[str, int | float]:
        await self.init()
        if not self._store_ready:
            return {"sessions": 0, "day": 0, "month": 0, "day_usd": 0.0, "month_usd": 0.0}
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                return await self._usage_from_db(db)
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            return {"sessions": 0, "day": 0, "month": 0, "day_usd": 0.0, "month_usd": 0.0}

    async def _usage_from_db(self, db: aiosqlite.Connection) -> dict[str, int | float]:
        day_key = f"day:{self._current_day()}"
        month_key = f"month:{self._current_month()}"
        session_cursor = await db.execute("SELECT COALESCE(SUM(count), 0) FROM cloud_budget_counter WHERE key LIKE 'session:%'")
        session_count = (await session_cursor.fetchone())[0]
        cursor = await db.execute("SELECT key, count FROM cloud_budget_counter WHERE key IN (?, ?)", (day_key, month_key))
        rows = await cursor.fetchall()
        day_cost_cursor = await db.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE substr(created_at, 1, 10) = ?",
            (self._current_day(),),
        )
        day_usd = (await day_cost_cursor.fetchone())[0]
        month_cost_cursor = await db.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0) FROM cloud_budget_usage_event WHERE substr(created_at, 1, 7) = ?",
            (self._current_month(),),
        )
        month_usd = (await month_cost_cursor.fetchone())[0]
        counts = {key: count for key, count in rows}
        return {
            "sessions": int(session_count),
            "day": int(counts.get(day_key, 0)),
            "month": int(counts.get(month_key, 0)),
            "day_usd": round(float(day_usd), 6),
            "month_usd": round(float(month_usd), 6),
        }

    def _preflight_block_reason(self, estimated_input_tokens: int, requested_output_tokens: int) -> str:
        if self.config.max_calls_per_turn < 1:
            return "turn_cap_zero"
        if self.config.max_calls_per_session < 1:
            return "session_cap_zero"
        if self.config.max_calls_per_day < 1:
            return "daily_cap_zero"
        if self.config.max_calls_per_month < 1:
            return "monthly_cap_zero"
        if self.config.max_daily_usd <= 0:
            return "daily_usd_cap_zero"
        if self.config.max_monthly_usd <= 0:
            return "monthly_usd_cap_zero"
        if self.config.max_input_tokens_per_call < 1:
            return "input_token_cap_zero"
        if self.config.max_output_tokens_per_call < 1:
            return "output_token_cap_zero"
        if estimated_input_tokens > self.config.max_input_tokens_per_call:
            return "input_token_cap_exceeded"
        if requested_output_tokens > self.config.max_output_tokens_per_call:
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
        if session_calls >= self.config.max_calls_per_session:
            return "session_cap_exhausted"
        if day_calls >= self.config.max_calls_per_day:
            return "daily_cap_exhausted"
        if month_calls >= self.config.max_calls_per_month:
            return "monthly_cap_exhausted"
        estimated_cost = self._input_cost(estimated_input_tokens) + self._output_cost(requested_output_tokens)
        if day_usd + estimated_cost > self.config.max_daily_usd:
            return "daily_usd_cap_exhausted"
        if month_usd + estimated_cost > self.config.max_monthly_usd:
            return "monthly_usd_cap_exhausted"
        return ""

    async def clear_accounting_block(self) -> CloudBudgetDecision:
        """Clear a stuck accounting_blocked flag.

        Accounting blocks fire when record_usage sees missing or invalid
        provider usage metadata. Once raised, every subsequent cloud call
        is denied until this flag is cleared. Operationally that means a
        single misbehaving provider can wedge cloud overflow indefinitely.
        This is the recovery path.

        Returns a decision describing what happened: allowed=True with
        reason="accounting_block_cleared" on success, allowed=False with
        a structured reason on failure (no-op if cloud spillover is
        disabled, store_unavailable if the SQLite layer is broken).
        """
        if not self.config.enabled:
            return CloudBudgetDecision(allowed=False, reason="cloud_spillover_disabled")
        await self.init()
        if not self._store_ready:
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES ('accounting_blocked', '0', ?) ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at",
                    (now,),
                )
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES ('last_accounting_error', '', ?) ON CONFLICT(key) DO UPDATE SET value = '', updated_at = excluded.updated_at",
                    (now,),
                )
                await db.commit()
            return CloudBudgetDecision(allowed=True, reason="accounting_block_cleared")
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            return CloudBudgetDecision(allowed=False, reason="budget_store_unavailable")

    async def _set_accounting_block(self, reason: str) -> None:
        await self.init()
        if not self._store_ready:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES ('accounting_blocked', '1', ?) ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
                    (now,),
                )
                await db.execute(
                    "INSERT INTO cloud_budget_state (key, value, updated_at) VALUES ('last_accounting_error', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (reason, now),
                )
                await db.commit()
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__

    async def _accounting_blocked(self) -> bool:
        return (await self._state_value("accounting_blocked")) == "1"

    async def _state_value(self, key: str) -> str:
        if not self._store_ready:
            return ""
        try:
            async with aiosqlite.connect(self.config.sqlite_path) as db:
                cursor = await db.execute("SELECT value FROM cloud_budget_state WHERE key = ?", (key,))
                row = await cursor.fetchone()
            return str(row[0]) if row else ""
        except Exception as exc:
            self._store_ready = False
            self._store_error = type(exc).__name__
            return ""

    def _input_cost(self, tokens: int, price_override: float | None = None) -> float:
        rate = price_override if price_override is not None else self.config.input_price_usd_per_million
        return tokens / 1_000_000 * rate

    def _output_cost(self, tokens: int, price_override: float | None = None) -> float:
        rate = price_override if price_override is not None else self.config.output_price_usd_per_million
        return tokens / 1_000_000 * rate

    def _session_key(self, session_id: str | None) -> str:
        return f"session:{session_id or 'anonymous'}"

    def _current_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _current_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

class MultiProviderBudgetGate:
    """Per-provider USD budget tracking layered on top of the global CloudBudgetGate.

    Each provider (openrouter, anthropic, google, openai) gets its own
    `ProviderBudgetConfig` with independent daily/monthly USD caps. The global
    `CloudBudgetGate` still governs call-count and token caps; per-provider
    gates add a second USD check keyed to whichever provider would fund the call.

    Usage in the router:
      gate = multi_gate.gate_for("openrouter")
      decision = await gate.reserve(session_id, ...)
    """

    PROVIDER_IDS = ("openrouter", "anthropic", "google", "openai")

    def __init__(
        self,
        global_gate: CloudBudgetGate,
        provider_configs: dict[str, ProviderBudgetConfig] | None = None,
    ) -> None:
        self.global_gate = global_gate
        self._provider_configs: dict[str, ProviderBudgetConfig] = provider_configs or {}
        # Build a CloudBudgetGate for each provider that has a config.
        # The per-provider gates share the same sqlite_path as the global gate
        # but use provider-scoped USD caps. Call-count caps are set to very
        # large numbers so they don't double-count (global gate owns those).
        self._gates: dict[str, CloudBudgetGate] = {}
        for pid, cfg in self._provider_configs.items():
            if cfg.enabled and (cfg.max_daily_usd > 0 or cfg.max_monthly_usd > 0):
                per_config = CloudBudgetConfig(
                    enabled=True,
                    # Call-count caps disabled at per-provider level; global gate owns them.
                    max_calls_per_turn=999_999,
                    max_calls_per_session=999_999,
                    max_calls_per_day=999_999,
                    max_calls_per_month=999_999,
                    max_daily_usd=cfg.max_daily_usd,
                    max_monthly_usd=cfg.max_monthly_usd,
                    # Token caps: inherit from global config.
                    max_input_tokens_per_call=global_gate.config.max_input_tokens_per_call,
                    max_output_tokens_per_call=global_gate.config.max_output_tokens_per_call,
                    input_price_usd_per_million=global_gate.config.input_price_usd_per_million,
                    output_price_usd_per_million=global_gate.config.output_price_usd_per_million,
                    sqlite_path=global_gate.config.sqlite_path,
                )
                self._gates[pid] = CloudBudgetGate(per_config)

    @classmethod
    def from_env(cls, global_gate: CloudBudgetGate) -> "MultiProviderBudgetGate":
        configs = {pid: ProviderBudgetConfig.from_env(pid) for pid in cls.PROVIDER_IDS}
        return cls(global_gate=global_gate, provider_configs=configs)

    def gate_for(self, provider_id: str) -> CloudBudgetGate:
        """Return the provider-scoped gate, falling back to the global gate."""
        return self._gates.get(provider_id, self.global_gate)

    @property
    def config(self) -> CloudBudgetConfig:
        """Expose global gate config for backward-compatible reads (e.g. admin policy)."""
        return self.global_gate.config

    async def init(self) -> None:
        await self.global_gate.init()
        for gate in self._gates.values():
            gate._store_ready = self.global_gate._store_ready
            gate._store_error = self.global_gate._store_error

    async def reserve(
        self,
        session_id: str | None,
        estimated_input_tokens: int = 0,
        requested_output_tokens: int = 0,
        provider_id: str = "default",
    ) -> CloudBudgetDecision:
        decision = await self.global_gate.reserve(session_id, estimated_input_tokens, requested_output_tokens)
        if not decision.allowed:
            return decision
        if provider_id in self._gates:
            return await self._gates[provider_id].reserve(session_id, estimated_input_tokens, requested_output_tokens)
        return decision

    async def availability(
        self,
        estimated_input_tokens: int = 0,
        requested_output_tokens: int = 0,
        provider_id: str = "default",
    ) -> CloudBudgetDecision:
        decision = await self.global_gate.availability(estimated_input_tokens, requested_output_tokens)
        if not decision.allowed:
            return decision
        if provider_id in self._gates:
            return await self._gates[provider_id].availability(estimated_input_tokens, requested_output_tokens)
        return decision

    async def record_usage(
        self,
        session_id: str | None,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        input_price_usd_per_million: float | None = None,
        output_price_usd_per_million: float | None = None,
        provider_id: str = "default",
    ) -> CloudBudgetDecision:
        decision = await self.global_gate.record_usage(
            session_id, provider, model, input_tokens, output_tokens,
            input_price_usd_per_million, output_price_usd_per_million,
        )
        if provider_id in self._gates and input_tokens is not None and output_tokens is not None:
            await self._gates[provider_id].record_usage(
                session_id, provider, model, input_tokens, output_tokens,
                input_price_usd_per_million, output_price_usd_per_million,
            )
        return decision

    async def snapshot(self) -> dict[str, object]:
        base = await self.global_gate.snapshot()
        providers: dict[str, object] = {}
        for pid, cfg in self._provider_configs.items():
            gate = self._gates.get(pid)
            if gate:
                gate_snap = await gate.snapshot()
                providers[pid] = {
                    "enabled": cfg.enabled,
                    "max_daily_usd": cfg.max_daily_usd,
                    "max_monthly_usd": cfg.max_monthly_usd,
                    "used": gate_snap.get("used", {}),
                }
            else:
                providers[pid] = {
                    "enabled": cfg.enabled,
                    "max_daily_usd": cfg.max_daily_usd,
                    "max_monthly_usd": cfg.max_monthly_usd,
                    "used": None,
                }
        base["providers"] = providers
        return base

    async def clear_accounting_block(self) -> CloudBudgetDecision:
        return await self.global_gate.clear_accounting_block()
