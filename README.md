# Customer Support AI Agent

A support agent that takes a refund request by **text or voice**, verifies who
it is talking to, finds the governing policy, decides whether that policy
actually supports the request, and either issues the refund or refuses it with
the clause behind the refusal — while showing its work.

**Live demo:** https://ca-web-supportagent-hk.wittyflower-26f62e18.eastus2.azurecontainerapps.io

All data is synthetic. Refunds are mock: they write a durable receipt row and
move a balance in the demo database, and nothing leaves it.

---

## The demo in ninety seconds

Sign in as **Avery Chen** (Premium, active) and click a suggestion:

| Click | What should happen |
|---|---|
| **Refund request** | Three chained tools, a receipt, and citations to the Premium policy |
| **Already refunded** (INV-1002) | Denied — *insufficient refundable balance*, with the clause |
| **Another account's invoice** (INV-2001) | Denied — *invoice belongs to another account*. The model asked for it; the server owns the answer |
| **Out of scope** (shipping) | No policy covers it, so no answer is invented — it escalates |
| **Inspect** on any turn | The observability drawer: waterfall, similarity against τ, tokens, cost |
| **Break something** → a fault, then the same request | The failure path, handled in the open |

Sign in as **Jordan Patel** (Basic) and send the same $15 refund request: the
same wording, a different governing document, and a different outcome. That
contrast is the point of the fixture set.

**Start voice** does the same work over WebRTC, in the same transcript, with
the same cards.

---

## What is actually guaranteed

Three properties are enforced by the runtime, not by asking the model nicely.
Each is the answer to "what happens when the model is wrong?", which for a
system that moves money is the only interesting question.

### 1. Tool ordering is a state machine, not a prompt instruction

```
INIT ──verify_identity──> VERIFIED ──retrieve_policy──> EVIDENCE_GATHERED
                                                              │
                        ┌─────────────────────────────────────┤
                        ▼                                     ▼
              execute_mock_refund                  create_escalation_ticket
                        └──────────────> TERMINAL <───────────┘
```

The model may *request* anything. `ToolExecutor` checks the transition table
before dispatching, so a refund as the first call is refused by the runtime
with `tool_not_allowed`. The voice path cannot narrow the tool list per state —
a realtime session's tools are fixed when it is minted — so it is advertised
all four and relies on the same executor. Same guarantee, arrived at
differently.

### 2. No policy claim, and no action, without grounded evidence

Retrieval returns a decision, not just chunks: `GROUNDED`, `WEAK`, or
`UNSUPPORTED`, from the top similarity against a calibrated threshold. A
refund requires `GROUNDED`. A `WEAK` result produces a clarifying question;
`UNSUPPORTED` escalates.

Citations are validated against the accepted evidence **before any answer text
is released**. An answer citing something outside that set is withheld — the
customer sees a refusal, and the offending ids go to the server log, where an
operator can tell a mis-copy from a fabrication.

A ticket may not assert something the server did not do. Asked for a $15
refund on a Basic plan capped at $10, the model files an escalation with
reason `action_blocked` *before attempting anything* — a durable record of a
refusal that never happened. The executor rejects that claim unless a refusal
was recorded earlier in the same turn, so the model's recovery is to attempt
the refund, collect a real citable denial, and then escalate truthfully.
Observable live on the Basic path, and pinned by
`test_escalation_truthfulness.py`.

The model never handles a chunk key. It cites short ordinal refs (`1`..`5`)
that the server resolves against this turn's evidence. That is not cosmetic:
asking the model to transcribe `refund-policy-premium@2.0#02:d27aad6be70e`
produced a one-character error on roughly **one turn in four**, and the guard
correctly refused a well-grounded answer each time.

### 3. A refund cannot be issued twice

Every refund carries an intent id derived from
`(session, invoice, amount, client_turn_id)`, with a unique constraint behind
it. The tool looks for an existing receipt before writing, locks the session
and invoice rows in a fixed order, and on the genuine race — two callers
between lookup and flush — recovers the winner's receipt so both agree on one
outcome. Re-submitting a turn replays its stored result instead of re-running
it.

The receipt row, not the tool's return value, is the record of a refund. If a
refund commits and the call reporting it then fails, the turn reconciles from
the row before composing a reply, and says the money moved.

---

## How it is built

```
Browser ──HTTPS──> Next.js (Container App, public ingress)
                     │   app/api/[...path] — streaming reverse proxy
                     ▼
                   FastAPI (Container App, INTERNAL ingress only)
                     ├── SSE turn stream          ├── Postgres 17 + pgvector
                     ├── guarded tool executor    ├── Azure OpenAI (managed identity)
                     └── WebRTC SDP proxy ────────┘        gpt-4.1-mini
                                                           text-embedding-3-small
Browser ◀══ WebRTC audio, direct ══▶ Azure OpenAI Realtime  gpt-realtime-mini
```

**The API is not publicly routable.** Its ingress is internal to the Container
Apps environment; the only way in is through the Next.js proxy, which is an
explicit route handler rather than a `next.config` rewrite because a rewrite
buffers SSE frames and streaming is the feature. The proxy also rewrites any
upstream `Location` header back onto the public origin, so the internal FQDN
cannot leak to a browser.

**Voice keeps the credential server-side.** The browser posts an SDP offer to
`/voice/connect`; the server mints an ephemeral key, spends it on the SDP
exchange, and returns only the answer. An ephemeral key is a bearer credential
for *our* Azure resource — anyone holding one could open their own session
against it, with their own instructions, billed to us. An SDP answer is not a
credential and cannot be replayed. Media still flows browser-to-Azure
directly, so this costs one request at setup and nothing per utterance.

**A voice turn spans many requests.** Each function call from the realtime
model is its own POST, seconds apart, possibly across a reconnect — so the
guard chain cannot live in process memory. It is derived from what was
durably recorded: agent state replayed from the turn's `tool_runs` through the
same transition table, identity re-resolved from the session binding, and
evidence rehydrated from a `turns.evidence` column that stores chunk *keys and
scores only*, with text re-read from the corpus so a turn cannot cite a stale
copy.

---

## Retrieval

**Corpus** — 7 versioned Markdown documents, 40 chunks. Premium and Basic
refund policies with different caps, contract eligibility, two troubleshooting
guides, and one *superseded* Premium policy that must never be retrieved.
Shipping questions are deliberately absent, so "when does my order arrive?"
has no supporting evidence anywhere.

**Chunking** — heading-aware via `markdown-it-py`. Soft target ~300 tokens
with ≤50 token overlap, applied only when a section overflows; short sections
stay whole. Eligibility rules are kept with their exceptions, because a chunk
that states a rule without its carve-out is worse than no chunk. Chunk ids
derive from `(policy_version, normalised_text)`, so reindexing is
deterministic and a citation survives a rebuild.

**Search** — filter on active version and tier ∈ {verified tier, global}
*first*, then order by cosine distance, top 5. Filtering before ranking is what
makes tier leakage structurally impossible rather than statistically unlikely.
Similarity is stated as `1 - cosine_distance` everywhere, in code and in the
UI, because "similarity 0.6" means nothing without that sentence.

**Calibration** — swept threshold × margin over 30 labelled queries in
`fixtures/eval/queries.json` (`python -m support.retrieval.calibrate`).
Selected **τ = 0.38, weak margin = 0.08**:

| Metric | Result |
|---|---|
| Authoritative doc in the evidence set (recall@k) | 21/21 — **100%** |
| Out-of-scope queries rejected | 6/6 — **100%** |
| Wrong-tier documents retrieved | **0** |
| Superseded versions retrieved | **0** |
| Supported queries accepted | 18/21 — 86% |
| Authoritative doc ranked first (top-1) | 16/21 — 76% |

Ties were broken toward the stricter threshold: an unsupported policy claim is
a worse failure than an unnecessary clarifying question. The three residual
misses are colloquial paraphrases carrying no policy vocabulary. Two are the
same sentence against both tiers — *"How much money can I get back without
talking to a manager?"*, 0.330 — and land in WEAK, which asks a clarifying
question. The third, *"Can you give me a goodwill credit as a gesture?"* at
0.280, falls below the weak band entirely and escalates to a human. Neither is
a wrong answer; both are the system declining to guess.

recall@k is the metric that governs grounding, since the agent reads every
accepted chunk. top-1 is a precision signal only, and its five misses are
genuine semantic overlaps — a duplicate-charge question really is
billing-adjacent.

Reproduce with `uv run python -m support.retrieval.calibrate`; the table above
is that command's output.

---

## Observability

**Inspect** on any turn opens a drawer built from the server's audit rows, not
from what the browser happened to receive — so a frame dropped on the way to
the UI still shows the tool call that ran.

* **Waterfall** on the turn's own clock. Bars are *positioned*, not stacked,
  because the gaps are model latency and they dominate: on a typical turn,
  3.30 s of 3.88 s is the model, not the tools. A stacked bar of tool
  durations would quietly blame the tools.
* **Grounding** drawn as the actual decision rule — the UNSUPPORTED / WEAK /
  GROUNDED bands in their real positions, with this turn's top similarity
  marked, and the threshold that *this decision* was taken under rather than
  whatever is in config now.
* **Tokens and cost**, labelled an estimate with the rate card's effective
  date. Voice turns show no cost: the realtime session reports usage to the
  browser, not to this server, and `$0.00` reads as "free" rather than
  "unknown".
* **Session rollup** with latency split by mode, because a spoken turn's span
  includes the time the customer spent talking. Every percentile is printed
  with its sample count, so "p95 (2)" is visibly the slower of two turns.

### Measured latency

Seven turns against the deployment, one session:

| | p50 | p95 |
|---|---|---|
| Turn total | **3.16 s** | 6.94 s |
| Time to first tool status | **473 ms** | 4.47 s |

Both p95s are the same turn: the first after a revision starts, paying for
credential acquisition and connection setup. Warm, first visible progress is
under half a second. In-region retrieval is most of the rest of the gap — the
same `retrieve_policy` call takes 386 ms in Azure and 3.76 s from a laptop.

---

## Failure handling

Arm any of these from **Break something** in the UI. Faults are scoped to the
caller's own session, server-side — one visitor cannot break another's demo.

| Failure | Behaviour | Verified |
|---|---|---|
| Model timeout | Bounded retry, then a named error and a **Try again** control | `error(llm_timeout, retryable)` → `recoverable_failure` |
| Model 429 | Same, honouring the provider's `Retry-After` | `error(llm_rate_limited, retry_after=1.0)` |
| Retrieval unavailable | No policy claim, no action, turn still finishes | `tool.failed(retrieve_policy)` → `clarification` |
| Tool failure | Refused in the open; the model may escalate | `tool.failed` ×2 → `clarification` |
| Lost confirmation after commit | Receipt recovered from the row; reported as recovered | `tool.failed(refund)` → `action.committed(reconciled)` → `action_success` |
| SSE disconnect / refresh | Snapshot reload; a re-submitted turn replays instead of re-running | — |
| Mic denied / call dropped | Explained in place, chat keeps working, reconnect adopts the in-flight turn | `/voice/state` → `calls_made: 2`, resumes to `terminal` |

Retries are bounded twice over: by attempt count, and by whether the backoff
leaves enough of the turn budget for the retry to actually finish. A 30-second
`Retry-After` inside a 30-second budget is not slept through — the customer
would get the same failure later, and the sleep would burn the time the retry
needed. The provider's number still reaches the UI so it can say when to try
again.

Vendor exceptions are classified at the adapter boundary into three facts:
retryable, how long to wait, what the customer may be told. An unrecognised
exception is **not** retryable, because a loop that cannot classify a failure
should not assume it is temporary.

---

## Running it locally

Needs Python 3.12, Node 20+, and a reachable Postgres 17 with `pgvector`. The
model calls go to Azure OpenAI; there is no offline mode, because the
properties worth testing are the ones that involve a real embedding space.

```bash
# 1. API
cd apps/api
cp .env.example .env          # fill in DATABASE_URL and AZURE_OPENAI_ENDPOINT
uv sync
uv run alembic upgrade head
uv run python -m support.retrieval.ingest     # chunk + embed the corpus
uv run python -m support.fixtures.seed        # accounts, invoices, policy rules
uv run python -m support.api                  # http://127.0.0.1:8000

# 2. Web
cd apps/web
npm ci
API_INTERNAL_URL=http://127.0.0.1:8000 npm run dev   # http://localhost:3000
```

Authentication to Azure OpenAI is `DefaultAzureCredential`: `az login` locally,
managed identity in the container. No API key is needed, and none is stored.

```bash
# Checks
cd apps/api && uv run ruff check . && uv run mypy support && uv run pytest
cd apps/web && npm run typecheck
```

`python -m support.api` exists because of a Windows-only ordering problem:
psycopg's async mode cannot run on `ProactorEventLoop`, uvicorn ignores the
event-loop policy, and setting it in the lifespan hook is too late. The loop is
built before uvicorn's server. On Linux the deployed container runs plain
`uvicorn support.api.app:app`.

---

## Infrastructure

One Terraform root, `azurerm ~> 4.0`, 18 resources, **no portal clicks**:

* 2 × Container Apps (Consumption) — Next.js public, FastAPI internal-only
* Azure Container Registry, pulled with a user-assigned managed identity
* PostgreSQL 17 Flexible Server with `pgvector` 0.8.7
* Azure OpenAI with three deployments: `gpt-4.1-mini`,
  `text-embedding-3-small`, `gpt-realtime-mini`
* Log Analytics, and RBAC so the apps' identity can call Azure OpenAI

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # subscription_id, dev_client_ip
terraform init && terraform apply
```

Terraform deliberately **ignores the container image tag**: infrastructure is
`terraform apply`, code is `az containerapp update`. Mixing them makes every
code deploy a plan review.

Four things worth knowing, all found the hard way:

1. **Postgres is in `centralus`, the apps in `eastus2`.** This subscription is
   barred from Flexible Server in five regions — a geography restriction, not
   quota. Costs ~20–25 ms per query.
2. **Ingress `transport` is pinned to `http`.** On `auto`, Container Apps
   negotiates h2c to the container, which hangs against HTTP/1.1-only servers
   (uvicorn/h11, Next.js standalone). The symptom is a healthy replica serving
   nothing, then a 504 at the 240 s idle timeout.
3. **The internal ingress 301s plain HTTP to its own HTTPS URL.** Pointing the
   proxy at `http://...internal...` leaked that hostname to the browser and
   broke every call. It uses `https://`, and the proxy sanitises `Location`.
4. **The image must be built from the repo root**, not `apps/api` — the API
   bakes `/fixtures` into the image so a cold container can seed itself.

Teardown: `scripts/azure-teardown.ps1`, or `terraform destroy`.

---

## CI/CD

Two workflows in `.github/workflows`, and no stored credential anywhere in
either of them.

**`ci.yml`** — every pull request, and the gate in front of every release.

| Job | What it runs |
|---|---|
| `static` | ruff, `ruff format --check`, mypy strict, `tsc --noEmit`, a full Next.js production build, `terraform fmt -check` + `validate` |
| `test` | All 166 tests against a `pgvector/pgvector:pg17` service container, with migrations, corpus ingestion and fixture seeding run first |

The production build is in the gate because `tsc --noEmit` does not catch a
server/client boundary violation — one has already passed a clean typecheck
and failed only at build time.

The test job authenticates to Azure OpenAI and embeds the real corpus, because
a zero-vector fake cannot produce a GROUNDED decision and the grounding gate is
what most of those tests are about. That costs a few cents per run and is the
difference between testing the system and testing a mock of it.

**`deploy.yml`** — push to `main`. Calls `ci.yml` first, then builds both
images inside ACR, resolves their digests, rolls both Container Apps forward
**by digest rather than by tag**, waits for each revision to report `Healthy`,
and finally runs `scripts/smoke.py` against the public URL. That last step
drives a real turn through the proxy, the internal API, the database,
retrieval and the model — a revision that starts but cannot answer a question
has not deployed. The run summary records both digests and the demo link.

### No secrets, by construction

Authentication is OIDC federation (`infra/cicd.tf`). GitHub mints a
short-lived token naming this repository and this branch; Entra exchanges it
for an Azure token only if the subject matches a federated credential. There
is no service principal password to rotate, and the repository variables hold
client ids — which identify an identity rather than authenticate as one.

Two identities, because the two jobs need unrelated powers:

| Identity | Can | Cannot |
|---|---|---|
| `id-gha-ci-*` | Call Azure OpenAI for inference | Deploy, push images, read the database |
| `id-gha-deploy-*` | Push images, run an ACR build, update those two Container Apps | Call the model, touch the database or the registry's lifecycle |

A compromised deploy job cannot spend model quota; a pull request from a fork
runs under the CI subject and can never reach the deploy identity, because
that credential is scoped to `refs/heads/main`.

`az acr build` schedules a build task inside the registry, which `AcrPush`
does not cover. The usual answer is to grant Contributor on the registry,
which also hands over deleting repositories and rotating admin credentials.
Instead there is a three-action custom role (`ACR Build Runner`) with exactly
`scheduleRun`, `runs/read` and `listLogSasUrl`.

Third-party actions are pinned to commit SHAs. A tag is mutable, and these
workflows hold a token that can reach Azure.

### Wiring it up

```bash
cd infra
# terraform.tfvars
#   github_repository = "<owner>/<repo>"
terraform apply

# One command, needs the GitHub CLI and `gh auth login`:
../scripts/set-github-variables.ps1 -Repository <owner>/<repo>
```

That sets ten repository **variables** (not secrets): the two client ids, the
tenant and subscription, the resource group, registry and app names, the Azure
OpenAI endpoint and the demo URL. Leave `github_repository` unset and no
identity is created at all, so anyone can `terraform apply` this root and get
the running application without being asked for a GitHub account.

To put a human in front of releases, add an `environment:` to the deploy job
and configure required reviewers on it in repository settings. It is left off
by default only so the first pipeline run cannot fail on a missing
environment.

Manual deploys still work and are still the fastest way to ship a hotfix:

```bash
TAG=$(date +%Y%m%d-%H%M%S)
az acr build --registry <acr> --image api:$TAG --file apps/api/Dockerfile .
az containerapp update -n <api-app> -g <rg> --image <acr>.azurecr.io/api:$TAG
python scripts/smoke.py https://<demo-host>
```

---

## Trade-offs

**No Kubernetes.** AKS would mean a control plane to patch, an ingress
controller to configure, and node pools to size — for two stateless
containers. Container Apps gives managed TLS, scale-to-zero, revision-based
rollout and an internal-only ingress out of the box. The honest cost is less
control over networking and no sidecar story; neither is load-bearing here.

**No Redis.** The only shared state is sessions and turns, and both are already
in Postgres, where the refund has to be transactional anyway. Adding Redis
would buy a faster rate limiter and cross-replica state, at the price of a
second thing to run and a second source of truth about who is verified. The
rate limiter is therefore an in-process token bucket, which is **correct only
at one replica** — stated plainly below rather than hidden.

**Single replica.** The rate limiter is the reason, and the demo is the
context. Scaling out needs the buckets moved to Postgres or Redis first;
nothing else in the design assumes one process. Session state is durable, the
guard chain is rebuilt from audit rows, and both would survive a replica
restart mid-turn.

**Exact vector search, no HNSW index.** 40 chunks. An approximate index would
add build time, a tuning surface and recall risk to a sequential scan that
takes under 400 ms in-region. The right time for HNSW is when the corpus is
measured to need it.

**Answers are buffered, not token-streamed.** Citations are validated before
any text is released, and you cannot un-say a token. Early feedback comes from
tool status events instead, which arrive in under half a second — sooner than
a first token would, and more informative.

**Hand-written TypeScript mirror of the Python event contract.** Generating a
discriminated union from Pydantic's JSON Schema needs enough post-processing
that the generator becomes its own thing to maintain. The trade is only safe
if drift is caught mechanically, so a parity test compares event names, enum
members **and payload field names** between the two.

**Cost is an estimate.** Rates are configuration with an effective date, not
provider-reported usage, and every surface that shows a cost says so.

---

## Known limitations

Things a reviewer should know rather than discover.

* **Microphone capture, speech transcription and barge-in are not verified by
  me.** The browser's mic permission was in `prompt` state and a Chrome
  permission bubble cannot be dismissed programmatically, so I exercised the
  voice path with a synthetic silent track and injected text, plus a
  `recvonly` transceiver for the SDP exchange. Server-side VAD with
  `interrupt_response` is confirmed present in the minted session and the
  client handles the interrupt events — but real speech is needed to prove it,
  and that belongs in the video, not in a claim here.
* **An escalation ticket's `reason` can still be untrue.** The executor
  enforces the one case it can check — `action_blocked` requires a recorded
  refusal earlier in the same turn — but a model that switches to
  `user_requested` instead is asserting something no server state contradicts.
  Three prompt revisions did not fix it. Documented rather than papered over.
* **The in-process rate limiter is correct at one replica.** See above.
* **No auth.** Sign-in picks a synthetic account; there are no real users. The
  session token is opaque, httpOnly and session-scoped, and one session cannot
  read another's transcript, receipts or traces — that part is tested.
* **`p95` over a handful of turns is the maximum wearing a statistic's name.**
  The UI prints sample counts for exactly this reason.
* **Held-out calibration is 3 queries**, of which two are near-identical
  phrasings. Reported, not treated as an estimate.

---

## Tests

166 tests, all against a real Postgres and real embeddings, because the
properties under test — row locks, unique constraints, cosine neighbourhoods —
are vacuous against fakes.

| File | What it defends |
|---|---|
| `test_guards.py` | Every illegal transition, in both directions |
| `test_refund_idempotency.py` | Concurrent same-intent commits yield exactly one receipt |
| `test_executor.py` | Tier caps, ownership, contract state, argument validation |
| `test_api.py` | Session isolation, replay, rate limits, SSE shape |
| `test_voice.py` | The guard chain across separate HTTP requests |
| `test_citation_guard.py` | A withheld answer is never relabelled as a delivered one |
| `test_reply_parsing.py` | Raw JSON must never reach a customer |
| `test_resilience.py` | Retry bounds, error classification, post-commit reconciliation |
| `test_observability.py` | Trace shape, per-turn thresholds, nearest-rank percentiles |
| `test_contract_parity.py` | Python ↔ TypeScript drift, including payload fields |
| `test_escalation_truthfulness.py` | A ticket may not claim something the server did not do |
| `test_policy_consistency.py` | Fixture policies agree with the rules table |
| `test_call_index.py` | Two refused calls in one turn do not collide |

---

## Repository layout

```
apps/api/          FastAPI service
  support/agent/       turn runner, prompts, voice session config
  support/tools/       executor, guards, refund, identity, rules
  support/retrieval/   chunking, ingest, search, calibration, persistence
  support/contracts/   Pydantic event contract and enums
  support/api/         routes, SSE, voice, deps, rate limiting
apps/web/          Next.js 15 App Router UI
  components/          chat, voice, trace drawer, demo controls
  lib/                 typed client, SSE parser, reducer, voice session
apps/smoke/        throwaway ingress probe from day one (kept: it is why
                   `transport = "http"` is pinned)
fixtures/          synthetic accounts, policy corpus, labelled eval queries
.github/workflows/ ci.yml (lint, types, build, full test suite) and
                   deploy.yml (build, roll out by digest, smoke test)
infra/             Terraform root — every Azure resource, including the
                   OIDC identities GitHub Actions federates into
scripts/           smoke.py, the GitHub variable setter, Azure teardown
docs/build-plan.md the working plan, and a record of what each block turned up
```

`docs/build-plan.md` is worth a look if you want the reasoning rather than the
result: sections 14a–14d are a log of the defects found while verifying each
block, including the ones that were my own mistakes.
