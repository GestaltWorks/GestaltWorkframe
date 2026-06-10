"""Tests for kb/maintenance.py operator maintenance helpers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import gestaltworkframe.kb.maintenance as maintenance


def test_rebuild_chroma_requires_yes_flag():
    with pytest.raises(SystemExit):
        maintenance.rebuild_chroma(yes=False)


def test_rebuild_chroma_without_existing_store(tmp_path, monkeypatch):
    chroma_dir = tmp_path / "chroma_db"
    monkeypatch.setattr(maintenance, "CHROMA_DB_DIR", chroma_dir)
    ingest_mock = MagicMock()
    monkeypatch.setattr(maintenance, "ingest_main", ingest_mock)

    result = maintenance.rebuild_chroma(yes=True)

    assert result is None
    ingest_mock.assert_called_once()


def test_rebuild_chroma_moves_existing_store(tmp_path, monkeypatch):
    chroma_dir = tmp_path / "chroma_db"
    chroma_dir.mkdir()
    (chroma_dir / "marker.txt").write_text("data")
    monkeypatch.setattr(maintenance, "CHROMA_DB_DIR", chroma_dir)
    ingest_mock = MagicMock()
    monkeypatch.setattr(maintenance, "ingest_main", ingest_mock)

    result = maintenance.rebuild_chroma(yes=True)

    assert result is not None
    assert result.name.startswith("chroma_db.poison-backup-")
    assert (result / "marker.txt").exists()
    assert not chroma_dir.exists()
    ingest_mock.assert_called_once()


def test_main_purge_discovery_find(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["maintenance", "purge-discovery-find", "abc123"])
    purge_mock = MagicMock()
    monkeypatch.setattr(maintenance, "purge_discovery_find_from_chroma", purge_mock)

    maintenance.main()

    purge_mock.assert_called_once_with("abc123")


def test_main_rebuild_chroma_dispatches_with_yes_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["maintenance", "rebuild-chroma", "--yes"])
    rebuild_mock = MagicMock()
    monkeypatch.setattr(maintenance, "rebuild_chroma", rebuild_mock)

    maintenance.main()

    rebuild_mock.assert_called_once_with(yes=True)


def test_main_rebuild_chroma_without_yes_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["maintenance", "rebuild-chroma"])
    rebuild_mock = MagicMock()
    monkeypatch.setattr(maintenance, "rebuild_chroma", rebuild_mock)

    maintenance.main()

    rebuild_mock.assert_called_once_with(yes=False)
