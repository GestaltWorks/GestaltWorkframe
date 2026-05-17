from __future__ import annotations

from core.db import database_url_from_env


def test_database_url_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_PATH", raising=False)

    assert database_url_from_env() == "sqlite+aiosqlite:///database.db"


def test_database_url_accepts_explicit_url(monkeypatch):
    monkeypatch.setenv("APP_DATABASE_URL", "sqlite+aiosqlite:////tmp/app.db")
    monkeypatch.setenv("APP_DATABASE_PATH", "ignored.db")

    assert database_url_from_env() == "sqlite+aiosqlite:////tmp/app.db"


def test_database_url_accepts_sqlite_path(monkeypatch):
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_DATABASE_PATH", "/var/lib/app/app.db")

    assert database_url_from_env() == "sqlite+aiosqlite:////var/lib/app/app.db"