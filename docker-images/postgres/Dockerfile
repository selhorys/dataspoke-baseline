# syntax=docker/dockerfile:1
# Custom PostgreSQL 17 image with pgvector and Apache AGE extensions.
#
# Strategy: multi-stage build.
#   - Stage 1 (builder): uses the upstream postgres:17 image (has full build
#     toolchain and PG dev headers) to compile Apache AGE from source against
#     PG17. pgvector is also built here to guarantee the correct .so version.
#   - Stage 2 (runtime): uses bitnami/postgresql:17 as the runtime base, which
#     preserves the Bitnami Helm chart's entrypoint contract (custom scripts,
#     PGDATA layout, non-root UID 1001). Compiled extension files are COPY'd
#     in from the builder at the paths Bitnami PG expects.
#
# Extension installation paths (Bitnami PG 17):
#   Shared libs:  /opt/bitnami/postgresql/lib/
#   Control/SQL:  /opt/bitnami/postgresql/share/extension/
#
# initdb CREATE EXTENSION is handled by Helm's postgresql.primary.initdb.scripts.

# ---------------------------------------------------------------------------
# Stage 1: builder (upstream postgres:17 on Debian 12 — full headers + toolchain)
#
# Pin to -bookworm so the builder's glibc (2.36) matches the Bitnami runtime
# (also Debian 12). Without the pin, postgres:17 tracks Debian's latest
# (trixie/13, glibc 2.38), and the compiled .so files fail to load at runtime
# with "GLIBC_2.38 not found".
# ---------------------------------------------------------------------------
FROM postgres:17-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential \
      git \
      flex \
      bison \
      libreadline-dev \
      zlib1g-dev \
      pkg-config \
      postgresql-server-dev-17 \
    && rm -rf /var/lib/apt/lists/*

# --- Build pgvector ---
# Use a recent release tag; PG17 support was added in 0.7.0.
ARG PGVECTOR_VERSION=v0.8.0
RUN git clone --branch "${PGVECTOR_VERSION}" --depth 1 \
      https://github.com/pgvector/pgvector.git /build/pgvector \
    && cd /build/pgvector \
    && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
    && make install PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config

# --- Build Apache AGE for PG17 ---
# No stable PG17 release yet; pin to the latest release-candidate tag.
# Tag PG17/v1.7.0-rc0 = commit e1467f12e0b1d15dd35d3ab93f057a7112d425b8, 2025-03-xx on
# refs/heads/release/PG17/1.7.0. Update ARG + comment when a stable tag ships.
ARG AGE_REF=PG17/v1.7.0-rc0
RUN git clone --branch "${AGE_REF}" --depth 1 \
      https://github.com/apache/age.git /build/age \
    && cd /build/age \
    && make PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config \
    && make install PG_CONFIG=/usr/lib/postgresql/17/bin/pg_config

# ---------------------------------------------------------------------------
# Stage 2: runtime (Bitnami PostgreSQL 17 — preserves chart entrypoint)
#
# Bitnami removed unversioned/major-only tags from the public Docker Hub in
# August 2025 (moved to paid `bitnamisecure/*` images). The `bitnamilegacy/*`
# namespace preserves the free versioned tags that match the Helm chart's
# entrypoint contract. Pin a specific patch to keep builds reproducible.
# ---------------------------------------------------------------------------
FROM bitnamilegacy/postgresql:17.6.0-debian-12-r4

# Copy pgvector artifacts from builder.
# upstream PG17 lib path: /usr/lib/postgresql/17/lib/
# upstream PG17 share path: /usr/share/postgresql/17/extension/
# Bitnami lib path:   /opt/bitnami/postgresql/lib/
# Bitnami share path: /opt/bitnami/postgresql/share/extension/
COPY --from=builder /usr/lib/postgresql/17/lib/vector.so \
                    /opt/bitnami/postgresql/lib/vector.so
COPY --from=builder /usr/share/postgresql/17/extension/vector.control \
                    /opt/bitnami/postgresql/share/extension/vector.control
COPY --from=builder /usr/share/postgresql/17/extension/vector--*.sql \
                    /opt/bitnami/postgresql/share/extension/

# Copy Apache AGE artifacts from builder.
COPY --from=builder /usr/lib/postgresql/17/lib/age.so \
                    /opt/bitnami/postgresql/lib/age.so
COPY --from=builder /usr/share/postgresql/17/extension/age.control \
                    /opt/bitnami/postgresql/share/extension/age.control
COPY --from=builder /usr/share/postgresql/17/extension/age--*.sql \
                    /opt/bitnami/postgresql/share/extension/

# Bitnami images run as UID 1001 (non-root).
USER 1001
