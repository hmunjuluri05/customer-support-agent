"""An escalation ticket may not assert something the server did not do.

Background. Given an over-cap refund request ($15.00 on a Basic plan capped at
$10.00), the live model declines to call the refund tool at all and escalates
instead -- with reason `action_blocked`. That ticket is a durable record
claiming an action was refused when no action was ever attempted, and it was
produced after three successive prompt revisions telling the model to call the
tool and let the server decide.

Prompt instruction is therefore not where this belongs. The executor owns it:
`action_blocked` requires a refusal recorded earlier in the same turn.

Rejection is retryable on purpose. The model's own recovery is either to
attempt the refund -- which yields a real refusal with the clause behind it --
or to escalate under a reason that is actually true.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select

from support.contracts.enums import (
    AgentState,
    DenialReason,
    EscalationReason,
    Mode,
    ToolName,
    ToolStatus,
)
from support.providers.azure_openai import AzureOpenAIEmbeddings
from support.storage.models import EscalationTicket, Turn
from support.storage.session import get_sessionmaker
from support.tools.executor import ToolExecutor, TurnContext


@pytest_asyncio.fixture(loop_scope="session")
async def embeddings():
    provider = AzureOpenAIEmbeddings()
    yield provider
    await provider.aclose()


async def _ctx(db, session_id) -> TurnContext:
    turn = Turn(
        session_id=session_id,
        client_turn_id=f"turn-{uuid4().hex[:8]}",
        mode=Mode.CHAT,
        input_hash="test",
    )
    db.add(turn)
    await db.commit()
    return TurnContext(
        session_id=session_id,
        client_turn_id=turn.client_turn_id,
        turn_id=turn.id,
    )


async def _verify_and_retrieve(ex: ToolExecutor, ctx: TurnContext) -> None:
    await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
    await ex.invoke(
        ctx,
        ToolName.RETRIEVE_POLICY,
        {"topic": "Can I get a refund for a duplicate charge, and what is the refund limit?"},
    )
    assert ctx.state is AgentState.EVIDENCE_GATHERED


async def test_action_blocked_rejected_when_nothing_was_attempted(
    basic_session, embeddings
) -> None:
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, basic_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        await _verify_and_retrieve(ex, ctx)

        outcome = await ex.invoke(
            ctx,
            ToolName.CREATE_ESCALATION_TICKET,
            {
                "reason": EscalationReason.ACTION_BLOCKED.value,
                "summary": "Customer asked for $15.00 but the Basic cap is $10.00.",
            },
        )

        assert outcome.status is ToolStatus.FAILED
        assert outcome.error_code == "invalid_arguments"
        # Retryable, so the model can correct itself inside the same turn.
        assert outcome.retryable is True
        await db.commit()

    # No ticket was written: the refusal happens before the insert.
    async with get_sessionmaker()() as db:
        tickets = (
            (
                await db.execute(
                    select(EscalationTicket).where(EscalationTicket.turn_id == ctx.turn_id)
                )
            )
            .scalars()
            .all()
        )
        assert tickets == []


async def test_action_blocked_allowed_after_a_real_refusal(basic_session, embeddings) -> None:
    """The honest sequence: attempt, be refused with a reason, then escalate."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, basic_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        await _verify_and_retrieve(ex, ctx)

        refund = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            # $15.00 against the $10.00 Basic cap.
            {"invoice_ref": "INV-2001", "amount_minor": 1500, "reason": "duplicate_charge"},
        )
        assert refund.status is ToolStatus.FAILED
        assert refund.error_code == "action_denied"
        assert refund.payload["denial_reason"] == DenialReason.EXCEEDS_TIER_CAP.value
        # The refusal is on the context, which is what unlocks the claim.
        assert ctx.denials == [DenialReason.EXCEEDS_TIER_CAP]

        escalated = await ex.invoke(
            ctx,
            ToolName.CREATE_ESCALATION_TICKET,
            {
                "reason": EscalationReason.ACTION_BLOCKED.value,
                "summary": "Refund of $15.00 refused by the Basic plan cap.",
            },
        )
        assert escalated.status is ToolStatus.COMPLETED
        assert escalated.payload["ticket_ref"].startswith("ESC-")
        await db.commit()


async def test_other_escalation_reasons_need_no_prior_refusal(basic_session, embeddings) -> None:
    """A customer asking for a human must never be blocked by this check."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, basic_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        await _verify_and_retrieve(ex, ctx)

        outcome = await ex.invoke(
            ctx,
            ToolName.CREATE_ESCALATION_TICKET,
            {
                "reason": EscalationReason.USER_REQUESTED.value,
                "summary": "Customer asked to speak to a person.",
            },
        )

        assert outcome.status is ToolStatus.COMPLETED
        await db.commit()
