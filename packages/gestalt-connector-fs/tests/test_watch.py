# Copyright 2026 Gestalt Works
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import asyncio

import pytest

from gestalt_connector_fs import diff_snapshots, snapshot_tree, watch_for_changes


def test_diff_snapshots_reports_added_modified_and_deleted(tmp_path) -> None:
    first = tmp_path / "first.md"
    first.write_text("one", encoding="utf-8")
    before = snapshot_tree(tmp_path)
    first.write_text("two changed", encoding="utf-8")
    (tmp_path / "second.md").write_text("new", encoding="utf-8")
    after = snapshot_tree(tmp_path)
    changes = diff_snapshots(before, after)
    assert {change.change_type for change in changes} == {"added", "modified"}


@pytest.mark.asyncio
async def test_watch_for_changes_detects_added_file(tmp_path) -> None:
    watcher = watch_for_changes(tmp_path, poll_interval_seconds=0.01)

    async def add_file() -> None:
        await asyncio.sleep(0.02)
        (tmp_path / "new.md").write_text("hello", encoding="utf-8")

    task = asyncio.create_task(add_file())
    changes = await asyncio.wait_for(watcher.__anext__(), timeout=1)
    await task
    assert changes[0].change_type == "added"
    assert changes[0].relative_path == "new.md"