# Stage 1: build dependencies into a venv
FROM python:3.13-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /build

COPY src/api/pyproject.toml .
RUN uv venv /venv && \
    uv pip install --python /venv/bin/python --no-cache .

# Stage 2: runtime image
FROM python:3.13-slim

ENV PYTHONPATH=/app \
    PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /venv /venv

WORKDIR /app
COPY src/ ./src/

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
