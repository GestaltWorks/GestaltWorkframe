import json

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from api import intake
from gestaltworkframe.core.db import TerminalIntakeRecord, save_terminal_intake_submission
from sqlalchemy.exc import IntegrityError


def _request_with_client(client_host: str, forwarded_for: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/intake/submissions",
            "headers": [(b"x-forwarded-for", forwarded_for.encode())],
            "client": (client_host, 12345),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


async def _test_app(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'intake.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = FastAPI()
    app.middleware("http")(intake.intake_body_size_limit)
    app.include_router(intake.router)

    async def override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[intake.get_session] = override_session
    return app, maker, engine


async def _records(maker):
    async with maker() as session:
        result = await session.execute(select(TerminalIntakeRecord))
        return result.scalars().all()


class _FakeTerminalIntakeResult:
    def __init__(self, record: TerminalIntakeRecord | None):
        self.record = record

    def scalars(self):
        return self

    def first(self):
        return self.record

    def scalar_one_or_none(self):
        return self.record


class _FakeRetrySession:
    def __init__(self):
        self.existing = TerminalIntakeRecord(
            terminal_session_id="terminal-session-1",
            selected_mode="pipeline",
        )
        self.execute_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, statement):
        self.execute_calls += 1
        return _FakeTerminalIntakeResult(None if self.execute_calls == 1 else self.existing)

    def add(self, record: TerminalIntakeRecord) -> None:
        pass

    async def commit(self) -> None:
        self.commit_calls += 1
        if self.commit_calls == 1:
            raise IntegrityError("insert", {}, Exception("duplicate terminal session"))

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def refresh(self, record: TerminalIntakeRecord) -> None:
        pass


def _payload(session_id: str = "terminal-session-1") -> dict[str, object]:
    return {
        "terminal_session_id": session_id,
        "selected_mode": "pipeline",
        "source_path": "/terminal",
        "intake": {
            "objective": " Explore automation support or consulting ",
            "building": "PSA cleanup\x00 and routing",
            "maturity": "Just starting",
            "help_needed": "Service Inquiry",
        },
    }


@pytest.mark.asyncio
async def test_save_terminal_intake_retries_integrity_error_upsert():
    session = _FakeRetrySession()

    record = await save_terminal_intake_submission(
        "terminal-session-1",
        "educator",
        {
            "objective": "Learn automation",
            "building": "Training path",
            "maturity": "Just starting",
            "help_needed": "Education",
        },
        session,
        ip_address="203.0.113.35",
    )

    assert record is session.existing
    assert session.commit_calls == 2
    assert session.rollback_calls == 1
    assert record.selected_mode == "educator"
    assert record.ip_address == "203.0.113.35"


def test_client_ip_only_trusts_forwarded_for_from_local_proxy() -> None:
    trusted = _request_with_client("127.0.0.1", "203.0.113.20")
    untrusted = _request_with_client("198.51.100.99", "203.0.113.20")

    assert intake.client_ip(trusted) == "203.0.113.20"
    assert intake.client_ip(untrusted) == "198.51.100.99"


@pytest.mark.asyncio
async def test_intake_body_size_limit_checks_body_without_content_length(monkeypatch):
    monkeypatch.setattr(intake, "_INTAKE_MAX_BODY_BYTES", 10)

    async def receive():
        return {"type": "http.request", "body": b"x" * 20, "more_body": False}

    async def call_next(request: Request) -> Response:
        return Response("ok")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/intake/submissions",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        },
        receive,
    )

    response = await intake.intake_body_size_limit(request, call_next)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_intake_body_size_limit_checks_actual_body_when_content_length_is_small(monkeypatch):
    monkeypatch.setattr(intake, "_INTAKE_MAX_BODY_BYTES", 10)

    async def receive():
        return {"type": "http.request", "body": b"x" * 20, "more_body": False}

    async def call_next(request: Request) -> Response:
        return Response("ok")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/intake/submissions",
            "headers": [(b"content-length", b"1")],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "scheme": "http",
            "query_string": b"",
        },
        receive,
    )

    response = await intake.intake_body_size_limit(request, call_next)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_intake_submission_is_captured_with_metadata(tmp_path):
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/intake/submissions",
                headers={"x-forwarded-for": "203.0.113.30", "referer": "https://example.com/terminal", "user-agent": "pytest"},
                json=_payload(),
            )

        assert response.status_code == 201
        records = await _records(maker)
        assert len(records) == 1
        record = records[0]
        assert record.terminal_session_id == "terminal-session-1"
        assert record.submission_count == 1
        assert record.selected_mode == "pipeline"
        assert record.objective == "Explore automation support or consulting"
        assert record.building == "PSA cleanup and routing"
        assert record.source_path == "/terminal"
        assert record.referrer == "https://example.com/terminal"
        assert record.user_agent == "pytest"
        assert json.loads(record.data)["help_needed"] == "Service Inquiry"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_submission_updates_existing_terminal_session(tmp_path):
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    payload = _payload()

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.32"}, json=payload)
            payload["selected_mode"] = "educator"
            payload["intake"]["help_needed"] = "Automation Educator"
            updated = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.33"}, json=payload)

        assert first.status_code == 201
        assert updated.status_code == 200
        records = await _records(maker)
        assert len(records) == 1
        assert records[0].selected_mode == "educator"
        assert records[0].help_needed == "Automation Educator"
        assert records[0].ip_address == "203.0.113.33"
        assert records[0].submission_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_rate_limits_by_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "_IP_LIMIT", 1)
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.31"}, json=_payload("terminal-session-1"))
            second = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.31"}, json=_payload("terminal-session-2"))

        assert first.status_code == 201
        assert second.status_code == 429
        assert len(await _records(maker)) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_rate_limits_existing_session_by_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "_IP_LIMIT", 1)
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.34"}, json=_payload())
            second = await client.post("/intake/submissions", headers={"x-forwarded-for": "203.0.113.34"}, json=_payload())

        assert first.status_code == 201
        assert second.status_code == 429
        records = await _records(maker)
        assert len(records) == 1
        assert records[0].submission_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_rate_limits_repeated_session_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "_SESSION_LIMIT", 1)
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/intake/submissions", json=_payload())
            second = await client.post("/intake/submissions", json=_payload())

        assert first.status_code == 201
        assert second.status_code == 429
        records = await _records(maker)
        assert len(records) == 1
        assert records[0].submission_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_rejects_oversized_body(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "_INTAKE_MAX_BODY_BYTES", 10)
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/intake/submissions", json=_payload())

        assert response.status_code == 413
        assert await _records(maker) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_intake_rejects_invalid_mode(tmp_path):
    app, maker, engine = await _test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    payload = _payload()
    payload["selected_mode"] = "shell"

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/intake/submissions", json=payload)

        assert response.status_code == 422
        assert await _records(maker) == []
    finally:
        await engine.dispose()