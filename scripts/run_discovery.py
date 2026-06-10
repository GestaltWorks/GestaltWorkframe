"""CLI entry point for the discovery subsystem.

Invoke with `uv run python -m scripts.run_discovery`. Reconciles the static
seed against `discovery_source`, polls every due source, persists new findings
deduped against prior runs, and prints a digest to stdout.

Designed to be safe to run on demand. The deploy schedule (cron / GitHub
Actions / VPS systemd timer) lands in a later milestone; M1 only ships the
runnable command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from gestaltworkframe.core.db import async_session_maker, init_db
from gestaltworkframe.core.discovery_queue import list_recent_finds, list_source_health
from gestaltworkframe.core.discovery_scheduler import run_one_pass


async def _main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    await init_db()

    async with async_session_maker() as session:
        report = await run_one_pass(session)
        recent = await list_recent_finds(session, limit=args.show_recent)
        health = await list_source_health(session)

    print("=== Discovery run ===")
    print(json.dumps(report.to_dict(), indent=2, default=str))
    if args.show_recent and recent:
        print("\n=== Recent finds ===")
        for find in recent:
            print(f"- [{find['status']}] {find['source_name']} {find['finding_type']}: {find['title']}")
            if find["url"]:
                print(f"    {find['url']}")
    if args.show_health:
        print("\n=== Source health ===")
        for src in health:
            polled = src["last_polled_at"] or "never"
            print(
                f"- {src['name']} ({src['watch_type']}) last_polled={polled} "
                f"status={src['last_status'] or '-'} fails={src['consecutive_failures']}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one pass of the discovery scheduler.")
    parser.add_argument(
        "--show-recent",
        type=int,
        default=25,
        help="Print this many most-recent finds after the run (default 25, 0 to skip).",
    )
    parser.add_argument(
        "--show-health",
        action="store_true",
        help="Print per-source health after the run.",
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
