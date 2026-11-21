"""The numbers behind the observability drawer.

The drawer is only worth having if what it shows is measured rather than
decorative, so these tests pin down the three ways that can quietly stop being
true:

* **A spoken turn is traceable at all.** Voice has no streaming runner to
  accumulate metrics, so its metric row is derived from the audit rows when the
  exchange closes. Without that, the drawer would be blank for exactly the
  half of the demo that is hardest to believe.
* **Absent numbers stay absent.** The realtime path reports token usage to the
  browser, not to this server. A $0.00 estimate reads as "free"; the drawer has
  to show nothing instead.
* **The threshold shown is the one the decision was taken under**, not
  whatever is in config when someone opens the drawer later.

Real embeddings, because a GROUNDED decision is what the trace is reporting
on. The realtime provider is stubbed -- see tests.test_voice.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from support.agent.runner import TurnRunner
from support.api.routes import _percentile
from support.contracts.enums import Mode, ToolName, TurnOutcome
from support.providers.azure_openai import (
    AzureOpenAIEmbeddings,
    ChatTurn,
    ToolCallRequest,
    Usage,
)
from support.storage.models import Turn, TurnMetric
from support.storage.session import get_sessionmaker
from tests.voice_requests import GOOD_TOPIC, start_session, voice_call


async def _spoken_refund(client: httpx.AsyncClient, sid: str, headers: dict, turn: str) -> None:
    """The full chained voice turn, then the reply that closes it."""
    await voice_call(client, sid, headers, turn=turn, name="verify_identity", call_id=f"{turn}-0")
    await voice_call(
        client,
        sid,
        headers,
        turn=turn,
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id=f"{turn}-1",
    )
    await voice_call(
        client,
        sid,
        headers,
        turn=turn,
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "duplicate_charge"},
        call_id=f"{turn}-2",
    )
    response = await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={"client_turn_id": turn, "role": "assistant", "text": "All set, receipt on its way."},
    )
    assert response.status_code == 200, response.text


async def _trace(client: httpx.AsyncClient, sid: str, headers: dict, turn: str) -> dict[str, Any]:
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Turn).where(Turn.client_turn_id == turn))).scalar_one()
        turn_id = row.id

    response = await client.get(f"/api/sessions/{sid}/turns/{turn_id}/trace", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------- per turn --


async def test_spoken_turn_is_traceable(client: httpx.AsyncClient) -> None:
    """Three chained calls across three requests, readable afterwards as one turn."""
    sid, headers = await start_session(client)
    await _spoken_refund(client, sid, headers, "v-trace")

    trace = await _trace(client, sid, headers, "v-trace")

    assert trace["mode"] == Mode.VOICE.value
    # Read off the rows, not inferred from what the model was asked to do: a
    # refund row exists, so the turn says a refund happened.
    assert trace["outcome"] == TurnOutcome.ACTION_SUCCESS.value
    assert [run["tool"] for run in trace["tool_runs"]] == [
        "verify_identity",
        "retrieve_policy",
        "execute_mock_refund",
    ]

    metrics = trace["metrics"]
    assert metrics is not None
    assert metrics["tool_calls"] == 3
    assert metrics["grounding_decision"] == "grounded"

    steps = metrics["waterfall"]["steps"]
    assert len(steps) == 3
    # Offsets, not durations: the drawer needs them to show the gaps between
    # calls, and they must be non-decreasing or the waterfall reads backwards.
    offsets = [step["start_ms"] for step in steps]
    assert offsets == sorted(offsets)
    assert [step["call_index"] for step in steps] == [0, 1, 2]
    # Each bar has to fit inside the turn it is drawn in. No tolerance here on
    # purpose: the first version of this assertion allowed 50ms of slack and
    # still failed by 320, which is what exposed the span and the offsets being
    # read from two different clocks.
    for step in steps:
        assert step["start_ms"] + (step["ms"] or 0) <= metrics["total_ms"]


async def test_voice_trace_leaves_cost_blank_rather_than_zero(client: httpx.AsyncClient) -> None:
    """A number nobody measured has no business in a cost panel."""
    sid, headers = await start_session(client)
    await _spoken_refund(client, sid, headers, "v-cost")

    metrics = (await _trace(client, sid, headers, "v-cost"))["metrics"]

    assert metrics["tokens_in"] is None
    assert metrics["tokens_out"] is None
    assert metrics["cost_estimate_usd"] is None
    # Still labelled an estimate, so the UI never has to decide that for itself.
    assert metrics["cost_is_estimate"] is True


async def test_trace_reports_the_threshold_the_decision_used(client: httpx.AsyncClient) -> None:
    sid, headers = await start_session(client)
    await _spoken_refund(client, sid, headers, "v-tau")

    metrics = (await _trace(client, sid, headers, "v-tau"))["metrics"]

    assert metrics["grounding_threshold_recorded"] is True
    assert metrics["grounding_threshold"] == pytest.approx(
        metrics["waterfall"]["grounding"]["threshold"]
    )
    assert metrics["top_similarity"] >= metrics["grounding_threshold"]


async def test_trace_marks_a_threshold_it_had_to_fall_back_to(client: httpx.AsyncClient) -> None:
    """A row written before thresholds were recorded must not borrow today's.

    It still has to render, so the current configured value is used -- and
    flagged, because showing an old decision against a retuned threshold as
    though it had been measured there would be a quiet lie.
    """
    sid, headers = await start_session(client)

    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=UUID(sid),
            client_turn_id="legacy-row",
            mode=Mode.CHAT,
            input_hash="legacy",
        )
        db.add(turn)
        await db.flush()
        db.add(
            TurnMetric(
                turn_id=turn.id,
                ttft_ms=120,
                total_ms=900,
                tool_calls=1,
                model="gpt-test",
                # The shape persisted before this change: no "grounding" key.
                waterfall={"steps": [{"tool": "retrieve_policy", "status": "completed", "ms": 80}]},
            )
        )
        await db.commit()
        turn_id = turn.id

    response = await client.get(f"/api/sessions/{sid}/turns/{turn_id}/trace", headers=headers)
    assert response.status_code == 200, response.text
    metrics = response.json()["metrics"]

    assert metrics["grounding_threshold_recorded"] is False
    assert metrics["grounding_threshold"] > 0


async def test_a_denied_voice_turn_is_not_reported_as_a_success(
    client: httpx.AsyncClient,
) -> None:
    """INV-2001 belongs to the Basic account; a Premium session may not touch it."""
    sid, headers = await start_session(client)
    await voice_call(client, sid, headers, turn="v-deny", name="verify_identity", call_id="d0")
    await voice_call(
        client,
        sid,
        headers,
        turn="v-deny",
        name="retrieve_policy",
        arguments={"topic": GOOD_TOPIC},
        call_id="d1",
    )
    denied = await voice_call(
        client,
        sid,
        headers,
        turn="v-deny",
        name="execute_mock_refund",
        arguments={"invoice_ref": "INV-2001", "amount_minor": 500, "reason": "duplicate_charge"},
        call_id="d2",
    )
    assert "action.denied" in [event["type"] for event in denied["events"]]

    await client.post(
        f"/api/sessions/{sid}/voice/transcript",
        headers=headers,
        json={"client_turn_id": "v-deny", "role": "assistant", "text": "I cannot refund that."},
    )

    trace = await _trace(client, sid, headers, "v-deny")
    assert trace["outcome"] == TurnOutcome.POLICY_DENIAL.value


# ---------------------------------------------------------- session rollup --


async def test_session_metrics_rolls_up_finished_turns(client: httpx.AsyncClient) -> None:
    sid, headers = await start_session(client)
    await _spoken_refund(client, sid, headers, "v-roll")

    response = await client.get(f"/api/sessions/{sid}/metrics", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["turns"] == 1
    assert body["turns_by_mode"]["voice"] == 1
    assert body["tool_calls"] == 3
    assert body["grounding"]["grounded"] == 1
    assert body["outcomes"][TurnOutcome.ACTION_SUCCESS.value] == 1

    # Nothing reported usage, so there is no total to present as one.
    assert body["turns_priced"] == 0
    assert body["cost_estimate_usd"] is None

    # Latency is split by mode on purpose: a spoken turn's span includes the
    # time the customer spent talking, so pooling the two medians would
    # describe nothing.
    assert body["latency"]["voice"]["total"]["samples"] == 1
    assert body["latency"]["chat"]["total"]["samples"] == 0
    assert body["latency"]["chat"]["total"]["p50_ms"] is None


async def test_session_metrics_never_count_another_session(client: httpx.AsyncClient) -> None:
    """The same isolation the transcript and receipts get."""
    first, first_headers = await start_session(client)
    await _spoken_refund(client, first, first_headers, "v-mine")

    second, second_headers = await start_session(client, slug="basic-active")

    body = (await client.get(f"/api/sessions/{second}/metrics", headers=second_headers)).json()
    assert body["turns"] == 0
    assert body["tool_calls"] == 0


async def test_metrics_of_another_session_are_refused(client: httpx.AsyncClient) -> None:
    first, _ = await start_session(client)
    _, other_headers = await start_session(client, slug="basic-active")

    response = await client.get(f"/api/sessions/{first}/metrics", headers=other_headers)
    assert response.status_code in (401, 403, 404)


# ------------------------------------------------------------ percentiles --


def test_percentile_is_nearest_rank() -> None:
    """No interpolation: every value returned is a sample that was measured.

    Over four turns a p95 is the maximum, and saying so is the point -- the UI
    prints the sample count beside it rather than implying a distribution.
    """
    assert _percentile([], 0.5) is None
    assert _percentile([7], 0.95) == 7
    assert _percentile([10, 20, 30, 40], 0.5) == 20
    assert _percentile([10, 20, 30, 40], 0.95) == 40
    assert _percentile([40, 10, 30, 20], 0.5) == 20


def test_percentile_never_indexes_past_the_end() -> None:
    for size in range(1, 12):
        values = list(range(size))
        for quantile in (0.0, 0.5, 0.9, 0.95, 1.0):
            assert _percentile(values, quantile) in values


# -------------------------------------------------------- prompt tracing --


class _ScriptedChat:
    """Verify, then answer. Enough to produce a two-call prompt trace."""

    model = "scripted-chat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        usage = Usage(input_tokens=11, output_tokens=7)
        if self.calls == 1:
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        call_id="p0", name=ToolName.VERIFY_IDENTITY.value, arguments={}
                    ),
                ),
                usage=usage,
            )
        return ChatTurn(text='{"answer": "All set.", "cited_refs": []}', tool_calls=(), usage=usage)

    async def aclose(self) -> None:
        return None


async def _chat_turn(session_id: Any) -> Turn:
    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=session_id,
            client_turn_id=f"pt-{uuid4().hex[:8]}",
            mode=Mode.CHAT,
            input_hash=uuid4().hex[:16],
        )
        db.add(turn)
        await db.commit()

        provider = AzureOpenAIEmbeddings()
        try:
            runner = TurnRunner(db, chat=_ScriptedChat(), embeddings=provider)
            async for _ in runner.run(
                turn=turn,
                session_id=session_id,
                user_text="Can you check my account?",
                history=[],
            ):
                pass
        finally:
            await provider.aclose()
        await db.commit()
        return turn


async def test_the_drawer_can_show_what_the_model_was_given(
    client: httpx.AsyncClient,
) -> None:
    """ "Prompt traces" is a named requirement, and it is per call for a reason.

    A tool-using loop's input grows: the second call carries the first call's
    result. One combined "the prompt" would hide the only part worth seeing.
    """
    sid, headers = await start_session(client)
    turn = await _chat_turn(UUID(sid))

    response = await client.get(f"/api/sessions/{sid}/turns/{turn.id}/trace", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    steps = body["metrics"]["prompt"]
    assert len(steps) == 2, "one entry per model call"
    assert [step["call"] for step in steps] == [0, 1]

    # The loop offers only what is legal in the current state, and the trace
    # shows it. From INIT that is verification, retrieval (global policies
    # only, unverified) and escalation -- a customer may ask for a human
    # before anything else happens. The action is what must be absent: no
    # refund tool exists to call before evidence has been gathered.
    assert "execute_mock_refund" not in steps[0]["tools_offered"]
    assert "verify_identity" in steps[0]["tools_offered"]

    # The input grows, which is the thing this view exists to show.
    assert len(steps[1]["messages"]) > len(steps[0]["messages"])
    assert steps[0]["response"]["tool_calls"] == ["verify_identity"]
    assert steps[1]["response"]["tool_calls"] == []

    assert "customer support agent" in body["system_prompt"].lower()


async def test_the_prompt_trace_carries_no_session_credential(
    client: httpx.AsyncClient,
) -> None:
    """The trace is served to the browser, so it is a disclosure surface.

    The session token authenticates every later request; a trace that echoed
    it back would turn an observability panel into a credential leak.
    """
    sid, headers = await start_session(client)
    token = headers["X-Session-Token"]
    turn = await _chat_turn(UUID(sid))

    response = await client.get(f"/api/sessions/{sid}/turns/{turn.id}/trace", headers=headers)
    raw = response.text

    assert token not in raw
    assert "token_hash" not in raw
    # Previews are bounded, so a trace cannot become a second copy of the
    # transcript sitting in the database.
    for step in response.json()["metrics"]["prompt"]:
        for message in step["messages"]:
            assert len(message["preview"]) <= 243
