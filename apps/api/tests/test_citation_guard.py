"""A withheld answer must not be labelled as a delivered one.

This is a regression test for a live failure with two halves.

The model was asked to cite policy by copying chunk keys verbatim. On a real
turn it returned `...#02:d27aad6be70e3` where the accepted key was
`...#02:d27aad6be70e` -- the right chunk, one character too long. The citation
guard refused to release the answer, which is correct, and the customer saw
"I couldn't confirm the policy sources" for a question the evidence answered at
0.692 similarity. That is the first half, and it is fixed at the source:
citations are now short ordinal refs that the server resolves, so there is no
hash for the model to mis-type.

The second half is worse and survives the fix. The turn was recorded with
outcome `escalation` although no ticket row was ever written, so the UI showed
an "Escalated" chip with nothing to show for it -- and the generic
"recoverable failure" relabelling that runs afterwards would, left unguarded,
read the grounded evidence and call the turn a *grounded answer*: the one claim
that had just been deliberately withheld. A refusal has to stay a refusal
through both steps, whatever the evidence looked like.

Real embeddings, because the retrieval has to actually clear the threshold for
this to be the interesting case. The chat provider is scripted so the citation
it returns is chosen rather than hoped for.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select

from support.agent.runner import TurnRunner
from support.contracts.enums import (
    EscalationReason,
    GroundingDecision,
    Mode,
    ToolName,
    TurnOutcome,
)
from support.providers.azure_openai import (
    AzureOpenAIEmbeddings,
    ChatTurn,
    ToolCallRequest,
    Usage,
)
from support.storage.models import EscalationTicket, Turn, TurnMetric
from support.storage.session import get_sessionmaker

TOPIC = "Can I get a refund for a duplicate charge, and what is the refund limit for my plan?"


class ScriptedChat:
    """Plays a fixed sequence of model responses.

    Two tool calls to gather evidence, then a final reply whose citation is
    whatever the test wants to put under the guard.
    """

    model = "scripted-chat"

    def __init__(self, *, final: str) -> None:
        self.final = final
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        usage = Usage(input_tokens=10, output_tokens=5)
        if self.calls == 1:
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        call_id="c0", name=ToolName.VERIFY_IDENTITY.value, arguments={}
                    ),
                ),
                usage=usage,
            )
        if self.calls == 2:
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(
                        call_id="c1",
                        name=ToolName.RETRIEVE_POLICY.value,
                        arguments={"topic": TOPIC},
                    ),
                ),
                usage=usage,
            )
        return ChatTurn(text=self.final, tool_calls=(), usage=usage)

    async def aclose(self) -> None:
        return None


async def _run(session_id: Any, *, final: str) -> tuple[list[Any], Turn]:
    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=session_id,
            client_turn_id=f"cite-{uuid4().hex[:8]}",
            mode=Mode.CHAT,
            input_hash=uuid4().hex[:16],
        )
        db.add(turn)
        await db.commit()

        provider = AzureOpenAIEmbeddings()
        try:
            runner = TurnRunner(db, chat=ScriptedChat(final=final), embeddings=provider)
            events = [
                event
                async for event in runner.run(
                    turn=turn,
                    session_id=session_id,
                    user_text="What does the refund policy allow for my plan?",
                    history=[],
                )
            ]
        finally:
            await provider.aclose()

        await db.commit()
        return events, turn


def _finished(events: list[Any]) -> Any:
    return next(event for event in events if event.type == "turn.finished")


def _types(events: list[Any]) -> list[str]:
    return [event.type for event in events]


async def test_an_unresolvable_citation_is_not_recorded_as_an_escalation(
    premium_session,
) -> None:
    """Ref 9 of 5 chunks: the guard refuses, and nothing claims otherwise."""
    events, turn = await _run(
        premium_session.id,
        final='{"answer": "Premium refunds are capped at $500 per request.", "cited_refs": [9]}',
    )

    assert "error" in _types(events)
    error = next(event for event in events if event.type == "error")
    assert error.payload.error_code == "citation_validation_failed"

    finished = _finished(events)
    # Not escalation: no ticket was created, so the chip would be a lie.
    assert finished.payload.outcome is not TurnOutcome.ESCALATION
    # Not a grounded answer either: the evidence *was* grounded, which is
    # exactly why the relabelling step has to be told to keep its hands off.
    assert finished.payload.outcome is not TurnOutcome.GROUNDED_ANSWER
    assert finished.payload.outcome is TurnOutcome.RECOVERABLE_FAILURE
    # The withheld claim must not appear in the released text.
    assert "$500" not in (finished.payload.message or "")
    assert finished.payload.citations == []

    async with get_sessionmaker()() as db:
        tickets = (
            (await db.execute(select(EscalationTicket).where(EscalationTicket.turn_id == turn.id)))
            .scalars()
            .all()
        )
        assert tickets == []

        metric = await db.get(TurnMetric, turn.id)
        assert metric is not None
        # And the drawer reads the same refusal, not a success.
        assert metric.grounding_decision is not None


async def test_a_resolved_ref_releases_the_answer_with_its_citation(
    premium_session,
) -> None:
    """The happy path through the same resolution: ref 1 is chunk one."""
    events, _ = await _run(
        premium_session.id,
        final='{"answer": "Your plan allows refunds for duplicate charges.", "cited_refs": [1]}',
    )

    assert "error" not in _types(events)

    finished = _finished(events)
    assert finished.payload.outcome is TurnOutcome.GROUNDED_ANSWER
    assert len(finished.payload.citations) == 1
    # Resolved server-side, so the id on the wire is a real chunk key the model
    # never saw.
    assert "@" in finished.payload.citations[0].chunk_id
    assert finished.payload.citations[0].similarity > 0


# ------------------------------------------- low confidence reaches a human --


class _AnswersWithoutEscalating:
    """Verifies, retrieves something unsupported, then just answers.

    The failure mode this guards: the model declines to call the escalation
    tool, the turn is still recorded as an escalation because the evidence was
    UNSUPPORTED, and the result is a turn labelled "Escalated" with no ticket.
    """

    model = "no-escalation-chat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        usage = Usage(input_tokens=10, output_tokens=5)
        script = [
            (ToolName.VERIFY_IDENTITY, {}),
            # Nothing in the corpus covers shipping addresses.
            (
                ToolName.RETRIEVE_POLICY,
                {"topic": "Can you ship a replacement router to a new address?"},
            ),
        ]
        if self.calls <= len(script):
            tool, arguments = script[self.calls - 1]
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(call_id=f"n{self.calls}", name=tool.value, arguments=arguments),
                ),
                usage=usage,
            )
        return ChatTurn(
            text='{"answer": "I am not sure about that.", "cited_refs": []}',
            tool_calls=(),
            usage=usage,
        )

    async def aclose(self) -> None:
        return None


async def test_unsupported_evidence_reaches_a_human_even_if_the_model_does_not(
    premium_session,
) -> None:
    """ "Routing low-confidence cases to a human escalation state" is a
    guarantee, not a hope.

    Observed live: three out-of-scope phrasings produced three behaviours --
    a ticket, a clarifying question, and an "escalation" outcome with no
    ticket row anywhere. The gate itself was sound (no claim, no action), but
    the routing depended on the model choosing to call the tool.
    """
    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=premium_session.id,
            client_turn_id=f"esc-{uuid4().hex[:8]}",
            mode=Mode.CHAT,
            input_hash=uuid4().hex[:16],
        )
        db.add(turn)
        await db.commit()

        provider = AzureOpenAIEmbeddings()
        try:
            runner = TurnRunner(db, chat=_AnswersWithoutEscalating(), embeddings=provider)
            events = [
                event
                async for event in runner.run(
                    turn=turn,
                    session_id=premium_session.id,
                    user_text="Please ship a replacement router to my new address.",
                    history=[],
                )
            ]
        finally:
            await provider.aclose()
        await db.commit()

    grounding = next(e for e in events if e.type == "grounding.decided")
    assert grounding.payload.decision is GroundingDecision.UNSUPPORTED

    created = [e for e in events if e.type == "escalation.created"]
    assert len(created) == 1, "an unsupported request must reach a human"
    assert created[0].payload.reason is EscalationReason.INSUFFICIENT_EVIDENCE

    finished = next(e for e in events if e.type == "turn.finished")
    assert finished.payload.outcome is TurnOutcome.ESCALATION

    # The chip and the ticket agree, because the ticket is a real row.
    async with get_sessionmaker()() as db:
        tickets = (
            (await db.execute(select(EscalationTicket).where(EscalationTicket.turn_id == turn.id)))
            .scalars()
            .all()
        )
    assert len(tickets) == 1
    assert tickets[0].ticket_ref == created[0].payload.ticket_ref
