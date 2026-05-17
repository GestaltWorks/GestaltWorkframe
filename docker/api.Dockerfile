# Gestalt Workframe — FastAPI backend image.
# Build context is the repository root: `docker build -f docker/api.Dockerfile .`

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:/app/packages/gestalt-connector-protocol:/app/packages/gestalt-connector-fs:/app/packages/gestalt-connector-itglue:/app/packages/gestalt-connector-hudu:/app/packages/gestalt-connector-msgraph-files:/app/packages/gestalt-connector-s3"

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /data \
    && chown -R app:app /data

WORKDIR /app

COPY --from=builder --chown=app:app /app /app

# Docker preserves the mount-point's owner/mode when first attaching an empty
# named volume, so creating /data above as app:app means the SQLite + Chroma
# directories under /data/ stay writable by the non-root app user.

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
