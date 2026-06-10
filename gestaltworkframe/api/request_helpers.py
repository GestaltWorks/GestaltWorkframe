"""Shared FastAPI request helpers.

Three modules previously declared their own copy of `_TRUSTED_PROXY_HOSTS` +
`_client_ip` and a body-size middleware. The middleware bodies were identical
except for the path and max-bytes thresholds. This module is the single
source of truth for request-level parsing and limiting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

# Only the local nginx reverse proxy on the VPS is trusted to supply
# X-Forwarded-For. Production FastAPI sits behind that proxy on 127.0.0.1.
TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Header limit chosen to match the historical 128-char column width on
# ChatUsageRecord.ip_address. Public callers may submit arbitrary text in
# X-Forwarded-For; trim defensively.
_MAX_IP_LENGTH = 128


def client_ip(request: Request) -> str:
    """Return the best-effort caller IP from request state and headers.

    Honors X-Forwarded-For only when the immediate connection came from a
    trusted proxy host; otherwise returns the direct client.host. Falls back
    to "unknown" when the request has no client (e.g., in some test fixtures).
    """

    client = getattr(request, "client", None)
    client_host = client.host if client else "unknown"
    headers = getattr(request, "headers", {})
    forwarded_header = headers.get("x-forwarded-for", "") if headers else ""
    forwarded = forwarded_header.split(",")[0].strip()
    if forwarded and client_host in TRUSTED_PROXY_HOSTS:
        return forwarded[:_MAX_IP_LENGTH]
    return client_host[:_MAX_IP_LENGTH]


def make_body_size_limit(
    *,
    path: str,
    max_bytes: int | Callable[[], int],
    detail: str,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """Build a Starlette middleware that rejects oversized bodies on `path`.

    Starlette caches `request.body()` so the downstream Pydantic parser can
    still read the bytes. The middleware checks the actual byte count, not
    the caller-supplied Content-Length header, so missing or fraudulent
    headers cannot bypass the limit.

    `max_bytes` may be an int (bound at factory call time) or a zero-arg
    callable (resolved on each request). The callable form lets module-level
    constants stay monkeypatchable in tests and keeps environment-driven
    limits responsive to runtime changes without rebuilding the middleware.
    """

    resolve_limit: Callable[[], int]
    if callable(max_bytes):
        resolve_limit = max_bytes
    else:
        bound_limit = int(max_bytes)
        resolve_limit = lambda: bound_limit

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == path:
            body_size = len(await request.body())
            if body_size > resolve_limit():
                return JSONResponse(
                    {"detail": detail},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
        return await call_next(request)

    return middleware
