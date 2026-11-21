"""Every recorded tool attempt must claim its own call_index.

`tool_runs` has unique(turn_id, call_index), and the executor writes an audit
row for refused calls as well as successful ones. So if a refusal does not
advance the index, two refusals in one turn collide on insert and take the
whole turn down with an IntegrityError -- a crash caused by the model making
two illegal calls, which is exactly the thing the guards exist to tolerate.

This matters most on the voice path. The chat loop bounds itself by iterating
at most `max_tool_calls_per_turn + 1` times, so a model looping on illegal
calls is contained by the loop rather than by the budget. A realtime session
has no such outer loop: it is advertised all four tools and calls whichever it
likes, whenever it likes.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from support.contracts.enums import AgentState, Mode, ToolName, ToolStatus
from support.storage.models import ToolRun, Turn
from support.storage.session import get_sessionmaker
from support.tools.executor import ToolExecutor, TurnContext


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


async def test_two_consecutive_guard_refusals_do_not_collide(premium_session, embeddings) -> None:
    """Two illegal calls in a row: both audited, neither crashes the turn."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)

        first = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
        )
        # Refund again, still from INIT. Escalation would NOT do here: asking
        # for a human is legal from INIT, so it would succeed and prove
        # nothing about two refusals in a row.
        second = await ex.invoke(
            ctx,
            ToolName.EXECUTE_MOCK_REFUND,
            {"invoice_ref": "INV-1002", "amount_minor": 500, "reason": "billing_error"},
        )

        assert first.status is ToolStatus.FAILED
        assert second.status is ToolStatus.FAILED
        assert first.call_index != second.call_index
        # State never advanced, so both were refused by the guard.
        assert ctx.state is AgentState.INIT

        await db.commit()

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
        assert [r.call_index for r in runs] == [0, 1]


async def test_refused_calls_consume_budget(premium_session, embeddings) -> None:
    """Otherwise a model could make unlimited illegal calls inside one turn."""
    async with get_sessionmaker()() as db:
        ctx = await _ctx(db, premium_session.id)
        ex = ToolExecutor(db, embeddings=embeddings)
        limit = ex._settings.max_tool_calls_per_turn

        codes: list[str | None] = []
        for _ in range(limit + 2):
            outcome = await ex.invoke(
                ctx,
                ToolName.EXECUTE_MOCK_REFUND,
                {"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "billing_error"},
            )
            codes.append(outcome.error_code)

        assert codes[:limit] == ["tool_not_allowed"] * limit
        # Past the budget the executor stops even trying the guard.
        assert codes[limit:] == ["budget_exhausted", "budget_exhausted"]
        await db.commit()

    async with get_sessionmaker()() as db:
        indexes = (
            (
                await db.execute(
                    select(ToolRun.call_index)
                    .where(ToolRun.turn_id == ctx.turn_id)
                    .order_by(ToolRun.call_index)
                )
            )
            .scalars()
            .all()
        )
        assert indexes == list(range(limit + 2)), "every attempt needs its own index"
