# Customer Support Agent

A support agent that takes a refund request by **text or voice**, verifies who
it is talking to, finds the governing policy, decides whether that policy
actually supports the request, and either issues the refund or refuses it with
the clause behind the refusal — while showing its work.

The interesting question in a system that moves money is not "can the model do
this?" but **"what happens when the model is wrong?"** Tool ordering, the
grounding gate and refund idempotency are enforced by the runtime, not by the
prompt.

> **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the design, with diagrams.

Running at
<https://ca-web-supportagent-hk.wittyflower-26f62e18.eastus2.azurecontainerapps.io>.

---

## What it does

- **Streaming chat** over SSE, with live per-tool status as each call runs.
- **Live voice** over WebRTC to Azure OpenAI Realtime — waveform, partial and
  final transcripts, barge-in — sharing one history with text.
- **Chained tool calls** through a typed state machine: verify identity and
  entitlements, retrieve tier-scoped policy, then act or escalate.
- **Grounded answers only.** Retrieval returns a decision, not just rows, and
  citations are validated before any text is released.
- **Idempotent actions.** A refund cannot be issued twice, including under
  concurrency or a lost response.
- **A trace for every turn** — prompt, tool waterfall, similarity against the
  threshold, tokens and estimated cost.

## Stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 15 (App Router), TypeScript, no UI framework |
| Backend | FastAPI, Pydantic v2, SQLAlchemy 2 async, `mypy --strict` |
| Data | PostgreSQL 17 + pgvector |
| Models | Azure OpenAI — chat, embeddings, realtime — via managed identity |
| Infrastructure | Azure Container Apps, Terraform, GitHub Actions (OIDC) |

## Running it

Needs Python 3.12, Node 20+, and PostgreSQL 17 with `pgvector`. Model calls go
to Azure OpenAI; authentication is `DefaultAzureCredential`, so `az login`
locally and managed identity in the container. No API key is required, and
none is stored.

```bash
# API
cd apps/api
cp .env.example .env          # DATABASE_URL and AZURE_OPENAI_ENDPOINT
uv sync
uv run alembic upgrade head
uv run python -m support.retrieval.ingest   # chunk + embed the policy corpus
uv run python -m support.fixtures.seed      # sample accounts and invoices
uv run python -m support.api                # http://127.0.0.1:8000

# Web
cd apps/web
npm ci
API_INTERNAL_URL=http://127.0.0.1:8000 npm run dev   # http://localhost:3000
```

```bash
# Checks
cd apps/api && uv run ruff check . && uv run mypy support && uv run pytest
cd apps/web && npm run typecheck && npm run build
```

Infrastructure is one Terraform root — every Azure resource, including the
identities GitHub Actions federates into:

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
terraform apply
```

Push to the default branch runs the full suite, builds both images, rolls them
out by digest and verifies the result through the public URL.

## Layout

```
apps/api/            FastAPI service
  support/agent/       turn runner, prompts, realtime session config
  support/tools/       executor, guards, refund, identity, rules
  support/retrieval/   chunking, ingest, search, calibration
  support/contracts/   typed event contract and enums
  support/api/         routes, SSE, voice, auth, rate limiting
apps/web/            Next.js UI
  components/          chat, voice, trace drawer
  lib/                 typed client, SSE parser, reducer, voice session
fixtures/            sample accounts, policy corpus, labelled eval set
infra/               Terraform
scripts/             smoke test, secret audit, teardown
.github/workflows/   CI gate and deployment
```

## Tests

175 tests, run against real PostgreSQL and real embeddings — row locks, unique
constraints under concurrency and cosine neighbourhoods are all vacuously
green against fakes. They cover the state machine, refund idempotency, policy
enforcement, session isolation, the citation guard, retry and recovery
behaviour, and drift between the Python and TypeScript event contracts.

```bash
python scripts/audit-secrets.py   # no secret in any commit, no source hidden
```
