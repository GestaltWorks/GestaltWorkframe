#!/usr/bin/env python3
"""CLI utility for encrypted key store backup and restore.

Usage:
  uv run python scripts/key_store_backup.py export --output keys_backup.json
  uv run python scripts/key_store_backup.py import --input keys_backup.json
  uv run python scripts/key_store_backup.py list

The exported file contains encrypted keys only. It is safe to store in version
control or backup systems. Keys remain encrypted with the same admin token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.key_store import ApiKeyStore


def get_store_path() -> str:
    return os.getenv("APP_DATABASE_PATH", os.getenv("APP_DATABASE_URL", "database.db"))


def get_admin_token() -> str:
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        print("Error: ADMIN_TOKEN env var required", file=sys.stderr)
        sys.exit(1)
    return token


async def cmd_export(args: argparse.Namespace) -> int:
    store = ApiKeyStore(get_store_path())
    manifest = await store.export_encrypted()
    output_path = Path(args.output)
    output_path.write_text(json.dumps(manifest, indent=2))
    print(f"Exported {manifest['key_count']} keys to {output_path}")
    return 0


async def cmd_import(args: argparse.Namespace) -> int:
    store = ApiKeyStore(get_store_path())
    admin_token = get_admin_token()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1
    manifest = json.loads(input_path.read_text())
    imported, skipped = await store.import_encrypted(manifest, admin_token)
    print(f"Imported {imported} keys, skipped {skipped} identical keys")
    return 0


async def cmd_list(args: argparse.Namespace) -> int:
    store = ApiKeyStore(get_store_path())
    keys = await store.list_keys()
    if not keys:
        print("No stored keys found (may use env fallbacks)")
        return 0
    print(f"{'Provider':<15} {'Updated At':<25} {'Salt/Nonce/Ciphertext (chars)'}")
    print("-" * 70)
    for key in keys:
        meta = f"{len(key['salt'])}/{len(key['nonce'])}/{len(key['ciphertext'])}"
        print(f"{key['provider_id']:<15} {key['updated_at']:<25} {meta}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Key store backup/restore utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export encrypted keys to file")
    export_parser.add_argument("--output", "-o", required=True, help="Output JSON file path")
    export_parser.set_defaults(func=cmd_export)

    import_parser = subparsers.add_parser("import", help="Import keys from file")
    import_parser.add_argument("--input", "-i", required=True, help="Input JSON file path")
    import_parser.set_defaults(func=cmd_import)

    list_parser = subparsers.add_parser("list", help="List stored keys (metadata only)")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return await args.func(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
