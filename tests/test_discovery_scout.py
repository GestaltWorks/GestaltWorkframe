from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from gestaltworkframe.core.db import DiscoveryFind, DiscoverySource
from gestaltworkframe.core.discovery_scout import DiscoveryScoutConfig, run_discovery_scout


class _Provider:
    async def chat(self, messages, tools=None, max_tokens=None):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "proposals": [
                                    {
                                        "name": "new_blog",
                                        "watch_type": "rss_feed",
                                        "target": "https://example.test/rss.xml",
                                        "reason": "Relevant public automation blog.",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }


@pytest.mark.asyncio
async def test_discovery_scout_queues_new_source_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_SCOUT_BUDGET_DB_PATH", str(tmp_path / "budget.db"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scout.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(DiscoverySource(name="seed", watch_type="rss_feed", target="https://example.test/feed.xml"))
        await session.commit()
        report = await run_discovery_scout(
            session,
            _Provider(),
            config=DiscoveryScoutConfig(enabled=True, max_daily_usd=1.0, max_calls_per_day=1, max_output_tokens=256),
        )
        finds = (await session.execute(select(DiscoveryFind))).scalars().all()

    assert report["status"] == "ok"
    assert report["queued"] == 1
    assert finds[0].finding_type == "new_source_candidate"
    await engine.dispose()