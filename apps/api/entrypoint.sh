#!/bin/sh
# Container entrypoint: bring the database to the state the app expects, then
# serve.
#
# Running migrations here rather than as a separate job is a deliberate choice
# for a single-replica demo (replicas are pinned to 1 in infra/variables.tf).
# With several replicas this would race, and the right shape would be a
# Container Apps job that runs to completion before the revision takes traffic.
#
# All three steps are idempotent:
#   * alembic stops at head when already there
#   * ingestion skips a document whose checksum is unchanged, so it makes no
#     embedding calls on a normal restart
#   * seeding upserts accounts and invoices by their stable refs
#
# The effect is that a deploy against an empty database provisions itself,
# and a restart against a populated one costs a few hundred milliseconds.
set -eu

echo "entrypoint: applying migrations"
alembic upgrade head

echo "entrypoint: ingesting policy corpus"
python -m support.retrieval.ingest

echo "entrypoint: seeding demo fixtures"
python -m support.fixtures.seed

echo "entrypoint: starting uvicorn"
# No --reload, no multiple workers: rate limiting and the one-active-turn rule
# are in-process, which is only correct with a single worker.
exec uvicorn support.api.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --no-access-log \
    --timeout-keep-alive 75
