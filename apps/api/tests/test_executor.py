"""Executor integration tests.

These exercise the real chokepoint: guard check, argument validation, budget,
dispatch, state transition and audit row, against the real database and real
embeddings.

The important ones are the two attack shapes:

  * `test_refund_refused_out_of_order` -- the model asks for a refund first.
  * `test_unverified_cannot_escalate_via_global_retrieval` -- the model tries
    to reach an action by retrieving a global document instead of verifying.

Both must be refused by the runtime, not by the model's good behaviour.
"""

from __future__ import annotations

from uuid import uuid4

import pytest_asyncio
from sqlalchemy import func, select

from support.contracts.enums import (
    AgentState,
    EscalationReason,
    Mode,
    ToolName,
    ToolStatus,
)
from support.providers.azure_openai import AzureOpenAIEmbeddings
from support.storage.models import Invoice, Refund, ToolRun, Turn
from support.storage.session import get_sessionmaker
from support.tools.executor import ToolExecutor, TurnContext


@pytest_asyncio.fixture(loop_scope="session")
async def embeddings():
    """Real embeddings: these are integration tests, and the calls are cheap.

    Closed explicitly, since the credential owns an aiohttp session.
    """
    provider = AzureOpenAIEmbeddings()
    yield provider
    await provider.aclose()


async def _make_turn(db, session_id) -> Turn:
    turn = Turn(
        session_id=session_id,
        client_turn_id=f"turn-{uuid4().hex[:8]}",
        mode=Mode.CHAT,
        input_hash="test",
    )
    db.add(turn)
    await db.commit()
    return turn


async def _ctx(db, session_id) -> TurnContext:
    turn = await _make_turn(db, session_id)
    return TurnContext(
        session_id=session_id,
        client_turn_id=turn.client_turn_id,
        turn_id=turn.id,
    )


# --------------------------------------------------------------- happy path --


async def test_full_refund_chain_in_order(premium_session, embeddings) -> None:
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        assert ctx.state is AgentState.INIT

        verified = await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
        assert verified.status is ToolStatus.COMPLETED
        assert verified.payload["tier"] == "premium"
        assert ctx.state is AgentState.VERIFIED

        retrieved = await ex.invoke(
            ctx, ToolName.RETRIEVE_POLICY, {"topic": "refund limit on my plan"}
        )
        assert retrieved.status is ToolStatus.COMPLETED
        assert ctx.state is AgentState.EVIDENCE_GATHERED
        assert ctx.is_grounded, retrieved.summary

        refunded = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
        )
        assert refunded.status is ToolStatus.COMPLETED
        assert refunded.payload["receipt_ref"].startswith("RF-")
        assert ctx.state is AgentState.TERMINAL
        await db.commit()

    # Exactly three audit rows, in order.
    async with get_sessionmaker()() as db:
        runs = (
            (
                await db.execute(
                    select(ToolRun)
                    .where(ToolRun.turn_id == ctx.turn_id)
                    .order_by(ToolRun.call_index)
                )
            )
            .scalars()
            .all()
        )
        assert [r.tool for r in runs] == [
            ToolName.VERIFY_IDENTITY,
            ToolName.RETRIEVE_POLICY,
            ToolName.EXECUTE_MOCK_REFUND,
        ]


# ------------------------------------------------------------------ guards --


async def test_refund_refused_out_of_order(premium_session, embeddings) -> None:
    """A refund requested as the first action is refused by the runtime."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        outcome = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
        )

        assert outcome.status is ToolStatus.FAILED
        assert outcome.error_code == "tool_not_allowed"
        assert ctx.state is AgentState.INIT, "a refused call must not advance state"
        await db.commit()

    async with get_sessionmaker()() as db:
        committed = (await db.execute(select(func.count()).select_from(Refund))).scalar_one()
        refunded = (
            await db.execute(
                select(Invoice.refunded_minor).where(Invoice.external_ref == "INV-1001")
            )
        ).scalar_one()
    assert committed == 0
    assert refunded == 0


async def test_unverified_cannot_escalate_via_global_retrieval(
    unverified_session, embeddings
) -> None:
    """The privilege-escalation path.

    An unverified session may retrieve global troubleshooting content, but that
    must not advance to EVIDENCE_GATHERED, so the refund remains unreachable.
    """
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, unverified_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        retrieved = await ex.invoke(
            ctx, ToolName.RETRIEVE_POLICY, {"topic": "I cannot sign in to my account"}
        )
        assert retrieved.status is ToolStatus.COMPLETED
        assert ctx.state is AgentState.INIT, "unverified retrieval must not grant evidence"
        assert ctx.verified_tier is None

        outcome = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {"invoice_ref": "INV-1001", "amount_minor": 100, "reason": "billing_error"},
        )
        assert outcome.status is ToolStatus.FAILED
        assert outcome.error_code == "tool_not_allowed"
        await db.commit()


async def test_available_tools_hides_refund_until_evidence(premium_session, embeddings) -> None:
    """The model is never offered a tool it could not legally call."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        assert ToolName.EXECUTE_MOCK_REFUND not in ex.available_tools(ctx)
        await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
        assert ToolName.EXECUTE_MOCK_REFUND not in ex.available_tools(ctx)
        await ex.invoke(ctx, ToolName.RETRIEVE_POLICY, {"topic": "refund cap"})
        assert ToolName.EXECUTE_MOCK_REFUND in ex.available_tools(ctx)
        await db.commit()


async def test_budget_exhaustion(premium_session, embeddings) -> None:
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        ctx.calls_made = ex._settings.max_tool_calls_per_turn

        outcome = await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
        assert outcome.status is ToolStatus.FAILED
        assert outcome.error_code == "budget_exhausted"
        await db.commit()


async def test_invalid_arguments_rejected(premium_session, embeddings) -> None:
    """Hallucinated or malformed arguments fail validation, not execution."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
        await ex.invoke(ctx, ToolName.RETRIEVE_POLICY, {"topic": "refund cap"})

        # Negative amount, and an extra field the model invented.
        outcome = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {
                "invoice_ref": "INV-1001",
                "amount_minor": -5000,
                "reason": "billing_error",
                "approved_by_manager": True,
            },
        )
        assert outcome.status is ToolStatus.FAILED
        assert outcome.payload["denial_reason"] == "invalid_arguments"
        await db.commit()


async def test_redaction_keeps_only_allowlisted_args(premium_session, embeddings) -> None:
    """Traces must not capture whatever the model happened to send."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        await ex.invoke(
            ctx,
            ToolName.RETRIEVE_POLICY,
            {"topic": "refund cap", "verification_code": "SECRET-1234"},
        )
        await db.commit()

    async with get_sessionmaker()() as db:
        run = (
            (await db.execute(select(ToolRun).where(ToolRun.turn_id == ctx.turn_id)))
            .scalars()
            .first()
        )
        assert run is not None
        assert "verification_code" not in run.args_redacted
        assert "SECRET-1234" not in str(run.args_redacted)


# -------------------------------------------------------------- escalation --


async def test_escalation_terminates_turn(premium_session, embeddings) -> None:
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        outcome = await ex.invoke(
            ctx,
            ToolName.CREATE_ESCALATION_TICKET,
            {
                "reason": EscalationReason.UNSUPPORTED_TOPIC.value,
                "summary": "Customer asked about shipping hardware to a new address.",
            },
        )
        assert outcome.status is ToolStatus.COMPLETED
        assert outcome.payload["ticket_ref"].startswith("ESC-")
        assert ctx.state is AgentState.TERMINAL

        # No further tools after a terminal outcome.
        after = await ex.invoke(ctx, ToolName.VERIFY_IDENTITY, {})
        assert after.status is ToolStatus.FAILED
        assert after.error_code == "tool_not_allowed"
        await db.commit()
