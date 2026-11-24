"""Voice endpoints.

The property that matters here is the one voice introduces and chat does not:
**a voice turn's guard chain spans several HTTP requests.** Each function call
from the realtime model is its own request, so the agent state cannot live in
process -- it is rebuilt from the audit rows and `turns.evidence` every time.

If that rebuild were wrong in the permissive direction, a refund would commit
without verification or without grounded evidence, through the voice path
only, while every chat test stayed green. These tests exist to make that
failure impossible to ship quietly.

Real embeddings are used for the retrieval step, because a zero-vector fake
cannot produce a GROUNDED decision and the grounding gate is the thing under
test. The realtime provider is stubbed: minting a session and exchanging SDP
is verified separately against the live service, and has no bearing on guard
enforcement.
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
from sqlalchemy import select

from support.contracts.enums import AgentState, Mode, TurnStatus
from support.storage.models import Message, Refund, Turn
from support.storage.session import get_sessionmaker
from tests.voice_requests import GOOD_TOPIC, event_types, start_session, voice_call

# ----------------------------------------------------------------- connect --


async def test_connect_proxies_sdp_and_never_returns_the_key(client: httpx.AsyncClient) -> None:
    sid, headers = await start_session(client)
    offer = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

    response = await client.post(
        f"/api/sessions/{sid}/voice/connect", headers=headers, json={"sdp": offer}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sdp"].startswith("v=0")
    # The ephemeral key is a bearer credential for our Azure resource. It must
    # not appear anywhere in what the browser receives.
    assert "ek_test" not in response.text


async def test_connect_rejects_an_offer_with_no_audio(client: httpx.AsyncClient) -> None:
    """Caught here rather than spending a mint on an offer Azure will refuse."""
    sid, headers = await start_session(client)
    response = await client.post(
        f"/api/sessions/{sid}/voice/connect",
        headers=headers,
        json={"sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_offer"


async def test_connect_pins_instructions_and_tools_server_side(client: httpx.AsyncClient) -> None:
    """The browser cannot choose the agent's prompt or widen its tool list."""
    sid, headers = await start_session(client)
    offer = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    await client.post(f"/api/sessions/{sid}/voice/connect", headers=headers, json={"sdp": offer})

    config = client._transport.app.state.realtime.minted[0]  # type: ignore[attr-defined]
    assert "customer support agent" in str(config["instructions"])
    assert {t["name"] for t in config["tools"]} == {  # type: ignore[index]
        "verify_identity",
        "retrieve_policy",
        "execute_mock_refund",
        "create_escalation_ticket",
    }
    assert config["audio"]["input"]["turn_detection"]["type"] == "server_vad"  # type: ignore[index]


async def test_connect_requires_the_session_credential(client: httpx.AsyncClient) -> None:
    """Neither header nor cookie. Creating the session set the cookie, and the
    cookie is a credential -- it is what lets a browser resume after a
    restart -- so it has to be cleared for this to test anything."""
    sid, _ = await start_session(client)
    client.cookies.clear()

    response = await client.post(
        f"/api/sessions/{sid}/voice/connect",
        json={"sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"},
    )
    assert response.status_code == 401


async def test_connect_reports_an_unavailable_voice_service(
    client: httpx.AsyncClient,
) -> None:
    sid, headers = await start_session(client)
    client._transport.app.state.realtime.fail = True  # type: ignore[attr-defined]

    response = await client.post(
        f"/api/sessions/{sid}/voice/connect",
        headers=headers,
        json={"sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "voice_unavailable"


# -------------------------------------------------- guards across requests --


async def test_refund_refused_when_the_chain_is_skipped(client: httpx.AsyncClient) -> None:
    """A refund as the first function call is refused, exactly as in chat."""
    sid, headers = await start_session(client)

    result = await voice_call(
        client,
        sid,
        headers,
        turn="v-skip",
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
    )

    assert "tool.failed" in event_types(result)
    assert result["agent_state"] == AgentState.INIT.value

    async with get_sessionmaker()() as db:
        refunds = (await db.execute(select(Refund))).scalars().all()
        assert refunds == []


async def test_state_survives_between_separate_requests(client: httpx.AsyncClient) -> None:
    """The chain advances across requests, rebuilt from durable state only."""
    sid, headers = await start_session(client)

    verified = await voice_call(
        client, sid, headers, turn="v-chain", name="verify_identity", call_id="c0"
    )
    assert verified["agent_state"] == AgentState.VERIFIED.value
    assert "identity.verified" in event_types(verified)

    retrieved = await voice_call(
        client,
        sid,
        headers,
        turn="v-chain",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="c1",
    )
    assert retrieved["agent_state"] == AgentState.EVIDENCE_GATHERED.value
    assert "grounding.decided" in event_types(retrieved)

    refunded = await voice_call(
        client,
        sid,
        headers,
        turn="v-chain",
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "duplicate_charge"},
        call_id="c2",
    )

    assert refunded["agent_state"] == AgentState.TERMINAL.value
    assert "action.committed" in event_types(refunded)

    async with get_sessionmaker()() as db:
        refunds = (await db.execute(select(Refund))).scalars().all()
        assert len(refunds) == 1
        assert refunds[0].amount_minor == 1500


async def test_evidence_is_rehydrated_not_remembered(client: httpx.AsyncClient) -> None:
    """The grounding gate passes from the stored row, not from process memory.

    The retrieval happens in one request and the refund in another, so if
    `turns.evidence` were not written and read back, the refund would be
    refused as ungrounded.
    """
    sid, headers = await start_session(client)
    await voice_call(client, sid, headers, turn="v-eg", name="verify_identity", call_id="c0")
    await voice_call(
        client,
        sid,
        headers,
        turn="v-eg",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="c1",
    )

    async with get_sessionmaker()() as db:
        turn = (await db.execute(select(Turn).where(Turn.client_turn_id == "v-eg"))).scalar_one()
        assert turn.mode is Mode.VOICE
        stored = turn.evidence
        assert stored is not None
        assert stored["decision"] == "grounded"
        assert len(stored["accepted"]) > 0
        # Keys and scores only: chunk text is re-read from the corpus.
        assert set(stored["accepted"][0]) == {"chunk_key", "similarity"}

    state = await client.get(f"/api/sessions/{sid}/voice/state", headers=headers)
    assert state.json()["grounded"] is True


async def test_evidence_does_not_leak_between_turns(client: httpx.AsyncClient) -> None:
    """A second spoken turn starts clean, so it cannot ride the first's grounding."""
    sid, headers = await start_session(client)
    await voice_call(client, sid, headers, turn="v-one", name="verify_identity", call_id="a0")
    await voice_call(
        client,
        sid,
        headers,
        turn="v-one",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="a1",
    )

    # New client_turn_id: a new exchange, and a new chain.
    result = await voice_call(
        client,
        sid,
        headers,
        turn="v-two",
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
        call_id="b0",
    )

    assert "tool.failed" in event_types(result)
    assert result["agent_state"] == AgentState.INIT.value


async def test_unknown_tool_is_answered_not_errored(client: httpx.AsyncClient) -> None:
    """A hallucinated tool name should not break the call."""
    sid, headers = await start_session(client)
    result = await voice_call(client, sid, headers, turn="v-unknown", name="transfer_funds")
    assert "Unknown tool" in result["output"]
    assert result["events"] == []


async def test_malformed_arguments_are_refused_not_crashed(client: httpx.AsyncClient) -> None:
    sid, headers = await start_session(client)
    response = await client.post(
        f"/api/sessions/{sid}/voice/tool",
        headers=headers,
        json={
            "client_turn_id": "v-bad",
            "call_id": "c0",
            "name": "execute_mock_refund",
            "arguments": "{not json at all",
        },
    )
    assert response.status_code == 200, response.text
    assert "tool.failed" in event_types(response.json())


async def test_sequence_is_monotonic_across_requests(client: httpx.AsyncClient) -> None:
    """One turn, several requests, one continuous sequence for the reducer."""
    sid, headers = await start_session(client)
    first = await voice_call(
        client, sid, headers, turn="v-seq", name="verify_identity", call_id="c0"
    )
    second = await voice_call(
        client,
        sid,
        headers,
        turn="v-seq",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="c1",
    )

    sequences = [e["sequence"] for e in first["events"] + second["events"]]
    assert sequences == list(range(len(sequences)))


async def test_voice_tool_requires_its_own_session(client: httpx.AsyncClient) -> None:
    """Holding session A's token must not drive session B's voice turn."""
    sid_a, _ = await start_session(client)
    _, headers_b = await start_session(client)

    response = await client.post(
        f"/api/sessions/{sid_a}/voice/tool",
        headers=headers_b,
        json={
            "client_turn_id": "v-x",
            "call_id": "c0",
            "name": "verify_identity",
            "arguments": "{}",
        },
    )
    assert response.status_code == 404


# -------------------------------------------------------------- transcript --


async def test_transcript_joins_the_shared_history(client: httpx.AsyncClient) -> None:
    """A spoken exchange must be visible to a later chat turn."""
    sid, headers = await start_session(client)

    for role, text in (("user", "Can I get a refund?"), ("assistant", "Let me check that.")):
        response = await client.post(
            f"/api/sessions/{sid}/voice/transcript",
            headers=headers,
            json={"client_turn_id": "v-t", "role": role, "text": text},
        )
        assert response.status_code == 200, response.text

    snapshot = await client.get(f"/api/sessions/{sid}", headers=headers)
    messages = snapshot.json()["messages"]

    assert [m["content"] for m in messages] == ["Can I get a refund?", "Let me check that."]
    assert {m["mode"] for m in messages} == {"voice"}


async def test_interrupted_reply_is_recorded_as_interrupted(client: httpx.AsyncClient) -> None:
    """Barge-in: the cut-off reply is shown, and flagged so it is not continued."""
    sid, headers = await start_session(client)
    await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={
            "client_turn_id": "v-i",
            "role": "assistant",
            "text": "Your refund policy allows up to—",
            "interrupted": True,
        },
    )

    async with get_sessionmaker()() as db:
        # Scoped to this session. Matching on content alone searched every row
        # in the database and failed against a shared one as soon as a second
        # run had written the same sentence.
        message = (
            await db.execute(select(Message).where(Message.session_id == UUID(sid)))
        ).scalar_one()
        assert message.interrupted is True
        assert message.mode is Mode.VOICE


async def test_assistant_transcript_closes_the_turn(client: httpx.AsyncClient) -> None:
    sid, headers = await start_session(client)
    await voice_call(client, sid, headers, turn="v-close", name="verify_identity", call_id="c0")
    await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={"client_turn_id": "v-close", "role": "assistant", "text": "All done."},
    )

    async with get_sessionmaker()() as db:
        turn = (await db.execute(select(Turn).where(Turn.client_turn_id == "v-close"))).scalar_one()
        assert turn.status is TurnStatus.FINISHED
        assert turn.finished_at is not None


async def test_filler_speech_does_not_end_a_tool_chain(client: httpx.AsyncClient) -> None:
    """The regression this was written for.

    The realtime model speaks and calls a tool in the same response -- "let me
    check that for you" arrives beside `verify_identity`. The browser wrote
    that filler as an assistant transcript, the server read any assistant
    transcript as the end of the exchange, and the next tool call in the same
    turn came back 409. Every multi-tool spoken turn died on its second call.
    """
    sid, headers = await start_session(client)

    await voice_call(client, sid, headers, turn="v-filler", name="verify_identity", call_id="c0")

    filler = await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={
            "client_turn_id": "v-filler",
            "role": "assistant",
            "text": "Let me check that for you.",
            "final": False,
        },
    )
    assert filler.status_code == 200, filler.text
    assert filler.json()["state"] == "running"

    # The chain continues: this is the call that used to 409.
    second = await client.post(
        f"/api/sessions/{sid}/voice/tool",
        headers=headers,
        json={
            "client_turn_id": "v-filler",
            "call_id": "c1",
            "name": "retrieve_policy",
            "arguments": json.dumps({"topic": GOOD_TOPIC}),
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["agent_state"] == "evidence_gathered"

    async with get_sessionmaker()() as db:
        turn = (
            await db.execute(select(Turn).where(Turn.client_turn_id == "v-filler"))
        ).scalar_one()
        assert turn.status is TurnStatus.RUNNING
        assert turn.finished_at is None


async def test_filler_speech_is_still_recorded(client: httpx.AsyncClient) -> None:
    """Not closing the turn must not mean dropping what was said."""
    sid, headers = await start_session(client)
    await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={
            "client_turn_id": "v-keep",
            "role": "assistant",
            "text": "One moment while I look that up.",
            "final": False,
        },
    )

    snapshot = await client.get(f"/api/sessions/{sid}", headers=headers)
    contents = [m["content"] for m in snapshot.json()["messages"]]
    assert "One moment while I look that up." in contents


async def test_the_last_reply_still_closes_the_turn(client: httpx.AsyncClient) -> None:
    """final defaults to True, so a reply with no tool call ends the exchange."""
    sid, headers = await start_session(client)
    await voice_call(client, sid, headers, turn="v-last", name="verify_identity", call_id="c0")
    await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={
            "client_turn_id": "v-last",
            "role": "assistant",
            "text": "Let me check that.",
            "final": False,
        },
    )
    closing = await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={"client_turn_id": "v-last", "role": "assistant", "text": "That is all done."},
    )
    assert closing.json()["state"] == "finished"

    # And a late call against a genuinely finished turn is still refused.
    late = await client.post(
        f"/api/sessions/{sid}/voice/tool",
        headers=headers,
        json={
            "client_turn_id": "v-last",
            "call_id": "c9",
            "name": "retrieve_policy",
            "arguments": json.dumps({"topic": GOOD_TOPIC}),
        },
    )
    assert late.status_code == 409


async def test_a_chain_split_across_turns_is_refused(client: httpx.AsyncClient) -> None:
    """Why the browser must keep one spoken request on one turn.

    Agent state is replayed from the tool_runs of a single turn, so a chain
    that continues under a fresh client_turn_id starts again at INIT. The
    server is right to refuse; this pins the contract the voice client relies
    on, after a VAD boundary mid-chain rotated the turn and turned a valid
    refund into "tool_not_allowed".
    """
    sid, headers = await start_session(client)

    await voice_call(client, sid, headers, turn="v-split-a", name="verify_identity", call_id="a0")
    await voice_call(
        client,
        sid,
        headers,
        turn="v-split-a",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="a1",
    )

    stranded = await voice_call(
        client,
        sid,
        headers,
        turn="v-split-b",
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "duplicate charge"},
        call_id="b0",
    )
    assert stranded["agent_state"] == AgentState.INIT.value
    assert "not available yet" in stranded["output"]

    async with get_sessionmaker()() as db:
        assert (
            await db.execute(select(Refund).where(Refund.session_id == UUID(sid)))
        ).scalar_one_or_none() is None
