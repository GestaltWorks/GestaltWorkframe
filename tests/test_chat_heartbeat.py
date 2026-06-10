"""Tests for the SSE heartbeat wrapper in api/chat.

The wrapper races a chunk async iterator against an asyncio timer. When
the underlying stream stalls past the configured interval, it yields a
colon-prefixed comment so reverse proxies do not idle-out the SSE
connection during long model turns.
"""

from __future__ import annotations

import asyncio

import pytest

from gestaltworkframe.api.chat import _with_heartbeat


async def _async_iter(items: list[str], *, delay: float = 0.0):
    for item in items:
        if delay:
            await asyncio.sleep(delay)
        yield item


@pytest.mark.asyncio
async def test_heartbeat_yields_chunks_unchanged_when_fast():
    out = []
    async for chunk in _with_heartbeat(_async_iter(["a", "b", "c"]), interval=0.5):
        out.append(chunk)
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_heartbeat_emits_keepalive_during_idle():
    """When the inner stream stalls, a heartbeat line is emitted."""
    out = []
    async for chunk in _with_heartbeat(_async_iter(["one", "two"], delay=0.15), interval=0.05):
        out.append(chunk)
        if len(out) >= 4:
            break

    assert "one" in out
    assert any(chunk.startswith(":") for chunk in out), f"expected a keepalive comment in {out}"


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_interval_zero():
    """interval=0 bypasses the heartbeat machinery entirely."""
    out = []
    async for chunk in _with_heartbeat(_async_iter(["x", "y"], delay=0.05), interval=0):
        out.append(chunk)
    assert out == ["x", "y"]


@pytest.mark.asyncio
async def test_heartbeat_propagates_exceptions():
    async def boom():
        yield "first"
        raise RuntimeError("inner failure")

    seen = []
    with pytest.raises(RuntimeError, match="inner failure"):
        async for chunk in _with_heartbeat(boom(), interval=0.5):
            seen.append(chunk)
    assert seen == ["first"]
