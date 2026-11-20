# syntax=docker/dockerfile:1
#
# Built from the REPOSITORY ROOT, not from apps/api:
#
#     az acr build -r <registry> -t api:<tag> -f apps/api/Dockerfile .
#
# because the policy corpus and account fixtures live in /fixtures and are
# needed at runtime -- the demo-reset endpoint and the ingestion step both read
# them. A context rooted at apps/api could not reach them.

FROM python:3.12-slim AS builder

# uv is pinned: an unpinned installer is a moving part in an otherwise
# lockfile-reproducible build.
RUN pip install --no-cache-dir uv==0.8.11

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile in their own layer, so editing source
# does not re-resolve or re-download the environment.
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY apps/api/ ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime --
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    FIXTURES_DIR=/app/fixtures

# Non-root. uid/gid are fixed so the layer is reproducible.
RUN groupadd -g 1001 app && useradd -u 1001 -g app -m -s /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app apps/api/ /app/
COPY --chown=app:app fixtures/ /app/fixtures/

# The build context comes from a Windows checkout, where the filesystem has no
# POSIX execute bit to carry across. Without this the image builds and pushes
# cleanly and then fails at container create with
#   exec: "/app/entrypoint.sh": permission denied
# which surfaces only as ActivationFailed with no application logs at all,
# because no process ever starts. Setting the mode here makes the image
# correct regardless of the host that built it.
RUN chmod 0755 /app/entrypoint.sh

USER app

EXPOSE 8000

# Readiness is /readyz (database + pgvector). /healthz is liveness only and
# deliberately touches nothing.
ENTRYPOINT ["/app/entrypoint.sh"]
