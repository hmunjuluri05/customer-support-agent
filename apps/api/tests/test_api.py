"""HTTP layer tests.

Covers the properties a reviewer will try to break: session isolation,
duplicate submits, oversized requests and rate limits. These are deliberately
cheap and deterministic -- no model call -- so they run on every commit.

The model-driven end-to-end turn is exercised by the runner and executor
suites; repeating it here would add ~10s and provider spend for no extra
coverage.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest_asyncio
from sqlalchemy import delete, select, text

from support.api.app import create_app
from support.api.ratelimit import RateLimiter
from support.config import get_settings
from support.providers.azure_openai import ChatTurn, EmbeddingResult, Usage
from support.storage.models import Session
from support.storage.session import get_sessionmaker


class FakeChat:
    """Deterministic chat provider.

    Returns a final answer with no tool calls, so a turn completes without a
    provider call. Required by the plan's "deterministic provider fakes in CI"
    and it keeps these tests free and repeatable.
    """

    model = "fake-chat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        reply = json.dumps({"answer": "Noted, thank you.", "cited_refs": []})
        return ChatTurn(text=reply, tool_calls=(), usage=Usage(input_tokens=10, output_tokens=5))

    async def aclose(self) -> None:
        return None


class FakeEmbeddings:
    """Fixed-width zero vectors. Never reached unless retrieval is called."""

    deployment = "fake-embeddings"

    async def embed(self, texts: object) -> EmbeddingResult:
        count = len(list(texts))  # type: ignore[arg-type]
        dim = get_settings().embedding_dimensions
        return EmbeddingResult(
            vectors=[[0.0] * dim for _ in range(count)], usage=Usage(input_tokens=1)
        )

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """App with providers stubbed out.

    Lifespan is not run: these tests never reach a provider, and constructing
    real ones would acquire Entra tokens for nothing.
    """
    settings = get_settings()
    app = create_app()
    app.state.settings = settings
    app.state.chat = FakeChat()
    app.state.embeddings = FakeEmbeddings()
    app.state.limiter = RateLimiter(
        per_session_per_minute=settings.rate_limit_turns_per_minute_per_session,
        per_ip_per_minute=settings.rate_limit_turns_per_minute_per_ip,
        max_concurrent=settings.max_concurrent_turns,
    )

    transport = httpx.ASGITransport(app=app)
    # https, not http: the session cookie is Secure, and httpx correctly
    # refuses to send a Secure cookie over http -- so an http test client
    # silently never carries it, and anything relying on the cookie (such
    # as resuming a session) would fail for a reason that is not the code's.
    started = datetime.now(UTC)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c

    # Scoped to this test's own sessions, by creation time.
    #
    # It used to be `delete(Session).where(token_hash.like("%"))` -- every
    # session in the database. Harmless against a throwaway CI container, and
    # destructive anywhere else: a local run points DATABASE_URL at a shared
    # instance and silently deletes whatever conversations were live there.
    async with get_sessionmaker()() as db:
        await db.execute(delete(Session).where(Session.created_at >= started))
        await db.commit()


async def _make_session(client: httpx.AsyncClient, slug: str | None = "premium-active"):
    body = {"account_slug": slug} if slug else {}
    response = await client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    return data["session_id"], data["session_token"]


# ------------------------------------------------------------------ health --


async def test_healthz_makes_no_dependency_call(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_database_and_pgvector(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200, response.text
    checks = response.json()["checks"]
    assert checks["database"] == "ok"
    assert checks["pgvector"] == "ok"


# -------------------------------------------------------------- demo data --


async def test_demo_accounts_are_labelled_synthetic(client: httpx.AsyncClient) -> None:
    """A reviewer must not be able to mistake fixtures for real customers."""
    response = await client.get("/api/demo/accounts")
    assert response.status_code == 200
    data = response.json()
    assert data["synthetic"] is True

    slugs = {a["slug"] for a in data["accounts"]}
    assert slugs == {"premium-active", "basic-active", "premium-inactive"}
    for account in data["accounts"]:
        assert account["label"].startswith("DEMO")


# --------------------------------------------------------------- sessions --


async def test_session_token_is_hashed_at_rest(client: httpx.AsyncClient) -> None:
    """A database dump must not yield usable credentials."""
    session_id, token = await _make_session(client)

    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    assert row.token_hash != token
    assert len(row.token_hash) == 64


async def test_account_binding_is_server_side(client: httpx.AsyncClient) -> None:
    """The only way to bind an account is the sign-in control."""
    session_id, _ = await _make_session(client, "basic-active")
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    assert row.demo_account_id is not None
    assert row.verified_at is not None


async def test_unverified_session_has_no_account(client: httpx.AsyncClient) -> None:
    session_id, _ = await _make_session(client, None)
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
    assert row.demo_account_id is None
    assert row.verified_at is None


async def test_unknown_account_slug_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/sessions", json={"account_slug": "does-not-exist"})
    assert response.status_code == 404


async def test_extra_fields_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/sessions", json={"account_slug": "premium-active", "tier": "premium"}
    )
    assert response.status_code == 422, "a client must not be able to assert a tier"


# ---------------------------------------------------------- authorisation --


async def test_no_credential_is_unauthorised(client: httpx.AsyncClient) -> None:
    """No header AND no cookie. Both have to be gone for this to mean anything.

    Creating a session sets the cookie, which is itself a credential -- that is
    what lets a browser resume after a restart. So the cookie jar is emptied
    here, or the request is not actually unauthenticated and the test asserts
    nothing.
    """
    session_id, _ = await _make_session(client)
    client.cookies.clear()

    response = await client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 401


async def test_cross_session_read_is_denied(client: httpx.AsyncClient) -> None:
    """The isolation requirement: two sessions cannot read each other."""
    session_a, _ = await _make_session(client, "premium-active")
    _, token_b = await _make_session(client, "basic-active")

    response = await client.get(f"/api/sessions/{session_a}", headers={"X-Session-Token": token_b})
    # 404 rather than 403: the response does not confirm session A exists.
    assert response.status_code == 404


async def test_own_session_read_is_allowed(client: httpx.AsyncClient) -> None:
    session_id, token = await _make_session(client)
    response = await client.get(f"/api/sessions/{session_id}", headers={"X-Session-Token": token})
    assert response.status_code == 200
    body = response.json()
    assert body["account"]["tier"] == "premium"
    assert body["messages"] == []
    assert body["receipts"] == []


async def test_garbage_token_is_denied(client: httpx.AsyncClient) -> None:
    session_id, _ = await _make_session(client)
    response = await client.get(
        f"/api/sessions/{session_id}", headers={"X-Session-Token": "not-a-real-token"}
    )
    assert response.status_code == 404


# ----------------------------------------------------------------- limits --


async def test_oversized_request_rejected(client: httpx.AsyncClient) -> None:
    session_id, token = await _make_session(client)
    settings = get_settings()
    huge = "x" * (settings.max_request_bytes + 1024)

    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        headers={"X-Session-Token": token},
        json={"client_turn_id": "big", "text": huge},
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "request_too_large"


async def test_rate_limit_returns_retry_after(client: httpx.AsyncClient) -> None:
    """Exhausting the per-session budget must give a controlled 429."""
    session_id, token = await _make_session(client)
    settings = get_settings()

    statuses = []
    for i in range(settings.rate_limit_turns_per_minute_per_session + 2):
        response = await client.post(
            f"/api/sessions/{session_id}/turns",
            headers={"X-Session-Token": token},
            json={"client_turn_id": f"rl-{i}", "text": "hello"},
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert "Retry-After" in response.headers
            detail = response.json()["detail"]
            assert detail["error_code"] in {"session_rate_limit", "ip_rate_limit"}
            break

    assert 429 in statuses, f"never rate limited: {statuses}"


# --------------------------------------------------------- demo controls --


async def test_fault_injection_is_session_scoped(client: httpx.AsyncClient) -> None:
    """One visitor must not be able to inject faults into another's session."""
    session_a, token_a = await _make_session(client, "premium-active")
    session_b, token_b = await _make_session(client, "basic-active")

    response = await client.post(
        f"/api/sessions/{session_a}/fault",
        headers={"X-Session-Token": token_a},
        json={"fault": "tool_error"},
    )
    assert response.status_code == 200

    # B cannot arm a fault on A.
    response = await client.post(
        f"/api/sessions/{session_a}/fault",
        headers={"X-Session-Token": token_b},
        json={"fault": "tool_error"},
    )
    assert response.status_code == 404

    async with get_sessionmaker()() as db:
        row_a = (await db.execute(select(Session).where(Session.id == session_a))).scalar_one()
        row_b = (await db.execute(select(Session).where(Session.id == session_b))).scalar_one()
    assert row_a.injected_fault == "tool_error"
    assert row_b.injected_fault is None


async def test_unknown_fault_kind_rejected(client: httpx.AsyncClient) -> None:
    session_id, token = await _make_session(client)
    response = await client.post(
        f"/api/sessions/{session_id}/fault",
        headers={"X-Session-Token": token},
        json={"fault": "delete_everything"},
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- stream --


async def test_sse_turn_emits_ordered_envelope(client: httpx.AsyncClient) -> None:
    """A full turn over SSE, with the fake provider.

    Asserts the stream contract: correct framing, a monotonic per-turn
    sequence, and exactly one terminal event.
    """
    session_id, token = await _make_session(client)

    async with client.stream(
        "POST",
        f"/api/sessions/{session_id}/turns",
        headers={"X-Session-Token": token},
        json={"client_turn_id": "sse-1", "text": "hello there"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"

        types: list[str] = []
        sequences: list[int] = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                types.append(line[7:].strip())
            elif line.startswith("data:"):
                sequences.append(json.loads(line[6:])["sequence"])

    assert types[0] == "turn.started"
    assert types[-1] == "turn.finished"
    assert types.count("turn.finished") == 1, "exactly one terminal event"
    assert sequences == sorted(sequences), "sequence must be monotonic"
    assert sequences == list(range(len(sequences))), "sequence must start at 0 and not skip"


async def test_duplicate_submit_replays_without_rerunning(client: httpx.AsyncClient) -> None:
    """A repeated client_turn_id must not run the turn twice.

    This is the "simulate a duplicate request" requirement at the HTTP layer:
    the second submit replays the terminal outcome instead of re-entering the
    agent, so no second action can be taken.
    """
    session_id, token = await _make_session(client)
    chat = client._transport.app.state.chat  # type: ignore[attr-defined]

    body = {"client_turn_id": "dup-1", "text": "hello there"}
    first = await client.post(
        f"/api/sessions/{session_id}/turns", headers={"X-Session-Token": token}, json=body
    )
    assert first.status_code == 200
    calls_after_first = chat.calls

    second = await client.post(
        f"/api/sessions/{session_id}/turns", headers={"X-Session-Token": token}, json=body
    )
    assert second.status_code == 200
    assert "turn.finished" in second.text
    assert chat.calls == calls_after_first, "the replay must not call the model again"


async def test_same_turn_id_different_text_is_conflict(client: httpx.AsyncClient) -> None:
    """Reusing an id with new text is a client bug, not a retry.

    Joining it would answer the wrong question against the original turn.
    """
    session_id, token = await _make_session(client)
    await client.post(
        f"/api/sessions/{session_id}/turns",
        headers={"X-Session-Token": token},
        json={"client_turn_id": "conflict-1", "text": "first question"},
    )
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        headers={"X-Session-Token": token},
        json={"client_turn_id": "conflict-1", "text": "a completely different question"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "client_turn_id_reused_with_different_input"


# ------------------------------------------------------------ resumption --
#
# The property under test throughout: resumption is scoped to the BROWSER, not
# to the demo account. The account is a public dropdown choice, so keying on it
# would let any visitor pick "Jordan Patel" and read the previous visitor's
# transcript, receipts and traces. The browser id is an unguessable httpOnly
# cookie; the account is only the label shown against it.


async def _say_something(client: httpx.AsyncClient, slug: str, text: str) -> str:
    """Create a session for `slug` and give it a transcript worth resuming."""
    created = await client.post("/api/sessions", json={"account_slug": slug})
    sid = created.json()["session_id"]
    await client.post(
        f"/api/sessions/{sid}/turns",
        headers={"X-Session-Token": created.json()["session_token"]},
        json={"client_turn_id": f"r-{sid[:8]}", "text": text, "mode": "chat"},
    )
    return str(sid)


async def test_no_cookie_means_nothing_to_resume(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/sessions/current")
    assert response.status_code == 200
    assert response.json()["sessions"] == []


async def test_a_browser_is_offered_its_own_sessions_one_per_account(
    client: httpx.AsyncClient,
) -> None:
    """Two accounts in one browser, both resumable.

    The session cookie names only the newest session, which is why signing in
    as a second account used to make the first unreachable even though it was
    still in the database and still the same visitor's.
    """
    jordan = await _say_something(client, "basic-active", "Jordan was here.")
    avery = await _say_something(client, "premium-active", "Avery was here.")

    sessions = (await client.get("/api/sessions/current")).json()["sessions"]

    by_slug = {s["account"]["slug"]: s for s in sessions}
    assert by_slug.keys() == {"basic-active", "premium-active"}
    assert by_slug["basic-active"]["session_id"] == jordan
    assert by_slug["premium-active"]["session_id"] == avery
    assert by_slug["basic-active"]["messages"] >= 1
    # Newest first, so the picker leads with what was last touched.
    assert sessions[0]["session_id"] == avery


async def test_another_browser_sees_none_of_them(client: httpx.AsyncClient) -> None:
    """The isolation question: someone else opening the demo link.

    Clearing the cookie jar is what a different browser, a different machine or
    a private window looks like to the server -- no browser id, so no claim on
    anything created before.
    """
    mine = await _say_something(client, "basic-active", "My transcript.")
    assert len((await client.get("/api/sessions/current")).json()["sessions"]) == 1

    client.cookies.clear()  # a different visitor arrives

    assert (await client.get("/api/sessions/current")).json()["sessions"] == []

    # And signing in as the SAME demo account does not inherit the transcript:
    # picking "Jordan Patel" is not a claim of ownership.
    theirs = await _say_something(client, "basic-active", "Their transcript.")
    sessions = (await client.get("/api/sessions/current")).json()["sessions"]

    assert [s["session_id"] for s in sessions] == [theirs]
    assert mine not in [s["session_id"] for s in sessions]

    # Nor can they read it directly, even holding the id.
    assert (await client.get(f"/api/sessions/{mine}")).status_code == 404


async def test_an_empty_session_is_not_offered(client: httpx.AsyncClient) -> None:
    """Resuming a blank transcript is a choice between nothing and nothing."""
    await client.post("/api/sessions", json={"account_slug": "premium-active"})

    assert (await client.get("/api/sessions/current")).json()["sessions"] == []


async def test_an_expired_session_is_not_offered(client: httpx.AsyncClient) -> None:
    """The row outlives the credential. Expiry ends the offer, not the data."""
    sid = await _say_something(client, "premium-active", "Still here?")
    assert len((await client.get("/api/sessions/current")).json()["sessions"]) == 1

    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == UUID(sid)))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    assert (await client.get("/api/sessions/current")).json()["sessions"] == []


async def test_resuming_reads_the_transcript_back_with_the_cookie_alone(
    client: httpx.AsyncClient,
) -> None:
    """No token is handed to JavaScript to make resumption work."""
    sid = await _say_something(client, "premium-active", "Remember this.")

    snapshot = await client.get(f"/api/sessions/{sid}")  # cookie only, no header

    assert snapshot.status_code == 200
    assert any("Remember this." in m["content"] for m in snapshot.json()["messages"])


# ----------------------------------------------------------------- reset --


async def test_reset_clears_conversations_but_keeps_the_session(
    client: httpx.AsyncClient,
) -> None:
    """Reset is for demo state, not for signing everyone out.

    It used to delete every session row, which destroyed every resumable
    conversation in every browser -- including other people's, mid-demo. A
    control for tidying up after a demo should not end the demo.
    """
    created = await client.post("/api/sessions", json={"account_slug": "premium-active"})
    sid = created.json()["session_id"]
    headers = {"X-Session-Token": created.json()["session_token"]}
    await client.post(
        f"/api/sessions/{sid}/turns",
        headers=headers,
        json={"client_turn_id": "reset-1", "text": "Before the reset.", "mode": "chat"},
    )
    await client.post(f"/api/sessions/{sid}/fault", headers=headers, json={"fault": "tool_error"})

    # Something structural to prove survives, and something stateful to prove does not.
    async with get_sessionmaker()() as db:
        await db.execute(
            text("UPDATE invoices SET refunded_minor = 999 WHERE external_ref = 'INV-1001'")
        )
        await db.commit()

    assert (await client.post(f"/api/sessions/{sid}/reset", headers=headers)).status_code == 200

    # The session is still signed in and still readable.
    snapshot = await client.get(f"/api/sessions/{sid}", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["messages"] == [], "the transcript should be gone"

    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == UUID(sid)))).scalar_one()
        assert row.injected_fault is None, "an armed fault is demo state too"

        # Structural data is untouched; only the balance the receipts moved is.
        balance = (
            await db.execute(
                text("SELECT refunded_minor FROM invoices WHERE external_ref = 'INV-1001'")
            )
        ).scalar_one()
        assert balance == 0, "fixture balance restored"

        seeded = (
            await db.execute(
                text("SELECT refunded_minor FROM invoices WHERE external_ref = 'INV-1002'")
            )
        ).scalar_one()
        assert seeded == 3800, "INV-1002 is seeded partially refunded, and stays that way"

        chunks = (await db.execute(text("SELECT count(*) FROM policy_chunks"))).scalar_one()
        assert chunks > 0, "the policy corpus is not demo state"


async def test_resuming_an_older_session_works(client: httpx.AsyncClient) -> None:
    """The case that was broken: resume the one that is NOT the newest.

    Listing is browser-scoped, but authorisation is not -- the session cookie
    names exactly one session. Asking for the Jordan session while the cookie
    named the newer Avery one was refused with "Session not found", which is
    the correct answer to the wrong question. Resuming switches the cookie.
    """
    older = await _say_something(client, "basic-active", "The older session.")
    newer = await _say_something(client, "premium-active", "The newer session.")

    # Before resuming, the cookie names the newer one and the older is unreachable.
    assert (await client.get(f"/api/sessions/{newer}")).status_code == 200
    assert (await client.get(f"/api/sessions/{older}")).status_code == 404

    switched = await client.post(f"/api/sessions/{older}/resume")
    assert switched.status_code == 200, switched.text
    assert switched.json()["session_id"] == older
    assert switched.json()["account"]["slug"] == "basic-active"

    snapshot = await client.get(f"/api/sessions/{older}")
    assert snapshot.status_code == 200
    assert any("The older session." in m["content"] for m in snapshot.json()["messages"])

    # The cookie now names the older one, so the newer is the unreachable one.
    assert (await client.get(f"/api/sessions/{newer}")).status_code == 404


async def test_another_browser_cannot_resume_a_session_it_does_not_own(
    client: httpx.AsyncClient,
) -> None:
    """Resumption must not become a way to claim someone else's session."""
    mine = await _say_something(client, "premium-active", "Mine.")

    client.cookies.clear()  # a different browser
    await _say_something(client, "basic-active", "Theirs.")

    stolen = await client.post(f"/api/sessions/{mine}/resume")

    assert stolen.status_code == 404
    assert (await client.get(f"/api/sessions/{mine}")).status_code == 404


async def test_resuming_needs_a_browser_credential(client: httpx.AsyncClient) -> None:
    sid = await _say_something(client, "premium-active", "Something.")
    client.cookies.clear()

    assert (await client.post(f"/api/sessions/{sid}/resume")).status_code == 401
