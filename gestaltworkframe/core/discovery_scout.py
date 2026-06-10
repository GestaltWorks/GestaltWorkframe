"""Bounded scout for proposing new discovery watch targets.

The scout is queue-gated: it writes `new_source_candidate` findings only. It does
not create or modify `discovery_source` rows and it never publishes content.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from gestaltworkframe.core.cloud_budget import CloudBudgetConfig, CloudBudgetGate
from gestaltworkframe.core.db import DISCOVERY_AUDIT_FIND_SEEN, DiscoveryAudit, DiscoveryFind, DiscoverySource
from gestaltworkframe.core.providers import LLMProvider


@dataclass(frozen=True)
class DiscoveryScoutConfig:
    enabled: bool
    max_daily_usd: float
    max_calls_per_day: int
    max_output_tokens: int

    @classmethod
    def from_env(cls) -> "DiscoveryScoutConfig":
        return cls(
            enabled=os.getenv("DISCOVERY_SCOUT_ENABLED", "0").strip() == "1",
            max_daily_usd=max(float(os.getenv("DISCOVERY_SCOUT_MAX_DAILY_USD", "1.00")), 0.0),
            max_calls_per_day=max(int(os.getenv("DISCOVERY_SCOUT_MAX_CALLS_PER_DAY", "1")), 0),
            max_output_tokens=max(int(os.getenv("DISCOVERY_SCOUT_MAX_OUTPUT_TOKENS", "768")), 128),
        )


async def run_discovery_scout(
    session: AsyncSession,
    provider: LLMProvider,
    *,
    config: DiscoveryScoutConfig | None = None,
) -> dict[str, Any]:
    cfg = config or DiscoveryScoutConfig.from_env()
    if not cfg.enabled:
        return {"status": "skipped", "reason": "discovery_scout_disabled", "queued": 0}

    sources = (await session.execute(select(DiscoverySource).where(DiscoverySource.active.is_(True)).limit(30))).scalars().all()
    if not sources:
        return {"status": "skipped", "reason": "no_active_sources", "queued": 0}

    prompt = _build_prompt(sources)
    estimated_input_tokens = max(1, len(prompt.encode("utf-8")) // 4)
    budget = CloudBudgetGate(
        CloudBudgetConfig(
            enabled=True,
            max_calls_per_turn=1,
            max_calls_per_session=1,
            max_calls_per_day=cfg.max_calls_per_day,
            max_calls_per_month=cfg.max_calls_per_day * 31,
            max_daily_usd=cfg.max_daily_usd,
            max_monthly_usd=cfg.max_daily_usd * 31,
            max_input_tokens_per_call=estimated_input_tokens + 500,
            max_output_tokens_per_call=cfg.max_output_tokens,
            sqlite_path=os.getenv("DISCOVERY_SCOUT_BUDGET_DB_PATH", os.getenv("CLOUD_SPILLOVER_DB_PATH", "database.db")),
        )
    )
    decision = await budget.reserve("discovery-scout", estimated_input_tokens, cfg.max_output_tokens)
    if not decision.allowed:
        return {"status": "blocked", "reason": decision.reason, "queued": 0}

    response = await provider.chat(
        [
            {"role": "system", "content": "Return only JSON. Propose public discovery watch targets."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=cfg.max_output_tokens,
    )
    text = _response_text(response)
    proposals = _parse_proposals(text)
    queued = await _queue_proposals(session, sources[0], proposals)
    await budget.record_usage("discovery-scout", provider.__class__.__name__, "discovery-scout", estimated_input_tokens, cfg.max_output_tokens)
    return {"status": "ok", "queued": queued, "proposals": len(proposals)}


def _build_prompt(sources: list[DiscoverySource]) -> str:
    source_lines = [f"- {s.name}: {s.watch_type} {s.target}" for s in sources[:30]]
    return (
        "Given these existing discovery sources, propose up to five net-new public watch targets. "
        "Allowed watch_type values: github_repo_watch, github_topic_watch, github_user_org_watch, rss_feed, "
        "subreddit_watch, youtube_channel_watch, web_diff, saved_search. "
        "Return JSON object {\"proposals\":[{\"name\":...,\"watch_type\":...,\"target\":...,\"reason\":...}]}.\n"
        + "\n".join(source_lines)
    )


def _response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "")
    content = getattr(response, "content", None)
    if isinstance(content, list):
        return "".join(str(getattr(block, "text", "")) for block in content)
    return str(content or response)


def _parse_proposals(text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    proposals = payload.get("proposals") if isinstance(payload, dict) else []
    if not isinstance(proposals, list):
        return []
    return [p for p in proposals if isinstance(p, dict) and p.get("name") and p.get("watch_type") and p.get("target")]


async def _queue_proposals(session: AsyncSession, source: DiscoverySource, proposals: list[dict[str, str]]) -> int:
    queued = 0
    now = datetime.now(timezone.utc)
    for proposal in proposals[:5]:
        external_id = f"scout:{proposal['watch_type']}:{proposal['target']}"
        existing = await session.execute(select(DiscoveryFind).where(DiscoveryFind.external_id == external_id))
        if existing.scalar_one_or_none() is not None:
            continue
        find = DiscoveryFind(
            discovery_source_id=source.id,
            finding_type="new_source_candidate",
            external_id=external_id,
            title=f"Scout proposal: {proposal['name']}",
            url=str(proposal.get("target", "")),
            summary_text=str(proposal.get("reason", ""))[:1000],
            raw_payload=json.dumps({"kind": "new_source_candidate", "proposal": proposal}, ensure_ascii=False),
            importance_signal="normal",
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(find)
        await session.flush()
        session.add(DiscoveryAudit(find_id=find.id, source_id=source.id, event_type=DISCOVERY_AUDIT_FIND_SEEN, actor="scout", after_state="pending"))
        queued += 1
    await session.commit()
    return queued