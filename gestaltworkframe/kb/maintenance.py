"""Operator maintenance helpers for KB poison recovery."""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from gestaltworkframe.kb.discovery_ingest import purge_discovery_find_from_chroma
from gestaltworkframe.kb.ingest import CHROMA_DB_DIR, main as ingest_main

logger = logging.getLogger(__name__)


def rebuild_chroma(*, yes: bool = False) -> Path | None:
    """Move the current Chroma directory aside and rebuild from configured sources."""

    if not yes:
        raise SystemExit("Pass --yes after stopping app services to rebuild Chroma.")
    backup_path: Path | None = None
    if CHROMA_DB_DIR.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = CHROMA_DB_DIR.with_name(f"{CHROMA_DB_DIR.name}.poison-backup-{stamp}")
        shutil.move(str(CHROMA_DB_DIR), str(backup_path))
        logger.warning("Moved existing Chroma store to %s", backup_path)
    ingest_main()
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="KB maintenance and poison recovery helpers.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    purge = subparsers.add_parser("purge-discovery-find", help="Remove one discovery find from Chroma metadata.")
    purge.add_argument("find_id")

    rebuild = subparsers.add_parser("rebuild-chroma", help="Rebuild Chroma from configured corpus sources.")
    rebuild.add_argument("--yes", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    if args.command == "purge-discovery-find":
        purge_discovery_find_from_chroma(args.find_id)
        logger.info("Purged discovery/%s from Chroma", args.find_id)
    elif args.command == "rebuild-chroma":
        rebuild_chroma(yes=args.yes)


if __name__ == "__main__":
    main()
