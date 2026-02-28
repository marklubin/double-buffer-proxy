# syntax=docker/dockerfile:1

ARG VERSION=dev

# --- Builder stage ---
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cache-friendly)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY src/ src/
RUN uv sync --frozen --no-dev

# --- Runtime stage ---
FROM python:3.12-slim

# curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the virtual environment and source from builder
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src

# Copy entrypoint
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# Create directories for volumes
RUN mkdir -p /app/certs /app/data /app/logs

ENV PATH="/app/.venv/bin:$PATH"
ENV SYNIX_HOST=0.0.0.0
ENV SYNIX_TLS_CA_DIR=/app/certs
ENV SYNIX_DB_PATH=/app/data/dbproxy.sqlite
ENV SYNIX_LOG_DIR=/app/logs

LABEL org.opencontainers.image.title="Synix Claude Proxy" \
      org.opencontainers.image.description="Pre-computes conversation summaries so Claude Code compaction is instant" \
      org.opencontainers.image.version="${VERSION}"

EXPOSE 443 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsk https://localhost:443/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
