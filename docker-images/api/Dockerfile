# Stage 1: build dependencies into a venv
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Stage 2: runtime image
FROM python:3.13-slim

ENV PYTHONPATH=/app \
    PATH="/build/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /build/.venv /build/.venv

WORKDIR /app
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/

EXPOSE 8002

# --no-access-log: uvicorn's access line appends the raw query string, which on
# GET /auth/google/callback?code=… is a live Google authorization code.
# RequestLoggingMiddleware already emits method, path, status, elapsed, client_ip
# and trace_id per request, and logs the path without the query string.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
