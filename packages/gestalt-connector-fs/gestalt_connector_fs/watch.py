# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class SnapshotEntry:
    relative_path: str
    mtime_ns: int
    size_bytes: int


@dataclass(frozen=True)
class FileChange:
    change_type: Literal["added", "modified", "deleted"]
    relative_path: str


def snapshot_tree(root_path: Path, exclude_dirs: set[str] | frozenset[str] = frozenset()) -> dict[str, SnapshotEntry]:
    snapshot: dict[str, SnapshotEntry] = {}
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root_path)
        if any(part.startswith(".") or part in exclude_dirs for part in relative.parts):
            continue
        stat = path.stat()
        relative_path = relative.as_posix()
        snapshot[relative_path] = SnapshotEntry(relative_path, stat.st_mtime_ns, stat.st_size)
    return snapshot


def diff_snapshots(before: dict[str, SnapshotEntry], after: dict[str, SnapshotEntry]) -> list[FileChange]:
    changes: list[FileChange] = []
    for relative_path in sorted(after.keys() - before.keys()):
        changes.append(FileChange("added", relative_path))
    for relative_path in sorted(before.keys() - after.keys()):
        changes.append(FileChange("deleted", relative_path))
    for relative_path in sorted(before.keys() & after.keys()):
        if before[relative_path] != after[relative_path]:
            changes.append(FileChange("modified", relative_path))
    return changes


async def watch_for_changes(root_path: Path, poll_interval_seconds: float = 5.0, exclude_dirs: set[str] | frozenset[str] = frozenset()) -> AsyncIterator[list[FileChange]]:
    previous = snapshot_tree(root_path, exclude_dirs)
    while True:
        await asyncio.sleep(poll_interval_seconds)
        current = snapshot_tree(root_path, exclude_dirs)
        changes = diff_snapshots(previous, current)
        if changes:
            yield changes
        previous = current