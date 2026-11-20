"""Failure handling: what the system does when a dependency lets it down.

Each test here is one row of the failure table in the plan, and the reason
they are worth writing is that every one of them is a path where the easy
behaviour is the dishonest one -- retry forever, claim nothing happened when
money moved, or let a vendor exception escape as a dropped connection the UI
has to guess about.

The provider is scripted rather than real: a genuine 429 cannot be summoned on
demand, and the point under test is this system's reaction, not Azure's.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from support.agent.runner import TurnRunner
from support.config import get_settings
from support.contracts.enums import FaultKind, Mode, ToolName, TurnOutcome
from support.providers.azure_openai import (
    AzureOpenAIEmbeddings,
    ChatTurn,
    ToolCallRequest,
    Usage,
)
from support.providers.errors import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    translate,
)
from support.storage.models import Refund, Session, Turn
from support.storage.session import get_sessionmaker

TOPIC = "Can I get a refund for a duplicate charge, and what is the refund limit for my plan?"


class FlakyChat:
    """Fails a given number of times, then answers."""

    model = "flaky-chat"

    def __init__(self, *, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return ChatTurn(
            text='{"answer": "All sorted.", "cited_refs": []}',
            tool_calls=(),
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def aclose(self) -> None:
        return None


class RefundingChat:
    """Verify, retrieve, refund, then answer. Used to exercise commit paths."""

    model = "refunding-chat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_: object) -> ChatTurn:
        self.calls += 1
        usage = Usage(input_tokens=10, output_tokens=5)
        script = [
            (ToolName.VERIFY_IDENTITY, {}),
            (ToolName.RETRIEVE_POLICY, {"topic": TOPIC}),
            (
                ToolName.EXECUTE_MOCK_REFUND,
                {"invoice_ref": "INV-1001", "amount_minor": 1500, "reason": "duplicate_charge"},
            ),
        ]
        if self.calls <= len(script):
            tool, arguments = script[self.calls - 1]
            return ChatTurn(
                text="",
                tool_calls=(
                    ToolCallRequest(call_id=f"c{self.calls}", name=tool.value, arguments=arguments),
                ),
                usage=usage,
            )
        return ChatTurn(text='{"answer": "Done.", "cited_refs": []}', tool_calls=(), usage=usage)

    async def aclose(self) -> None:
        return None


async def _run(session_id: Any, chat: Any) -> tuple[list[Any], Turn]:
    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=session_id,
            client_turn_id=f"res-{uuid4().hex[:8]}",
            mode=Mode.CHAT,
            input_hash=uuid4().hex[:16],
        )
        db.add(turn)
        await db.commit()

        provider = AzureOpenAIEmbeddings()
        try:
            runner = TurnRunner(db, chat=chat, embeddings=provider)
            events = [
                event
                async for event in runner.run(
                    turn=turn,
                    session_id=session_id,
                    user_text="Please refund the duplicate charge on INV-1001, $15.00.",
                    history=[],
                )
            ]
        finally:
            await provider.aclose()
        await db.commit()
        return events, turn


async def _arm(session_id: Any, fault: FaultKind) -> None:
    async with get_sessionmaker()() as db:
        row = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()
        row.injected_fault = None if fault is FaultKind.NONE else fault.value
        await db.commit()


def _types(events: list[Any]) -> list[str]:
    return [event.type for event in events]


def _finished(events: list[Any]) -> Any:
    return next(event for event in events if event.type == "turn.finished")


def _error(events: list[Any]) -> Any:
    return next(event for event in events if event.type == "error")


# ------------------------------------------------- translation at the edge --


class _FakeHeaders:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, _key: str) -> str | None:
        return self.value


class _FakeResponse:
    def __init__(self, retry_after: str | None) -> None:
        self.headers = _FakeHeaders(retry_after)


def _sdk_error(
    name: str, *, status: int | None = None, retry_after: str | None = None
) -> Exception:
    exc = type(name, (Exception,), {})("boom")
    if status is not None:
        exc.status_code = status  # type: ignore[attr-defined]
    exc.response = _FakeResponse(retry_after)  # type: ignore[attr-defined]
    return exc


def test_known_sdk_failures_are_classified() -> None:
    assert isinstance(translate(_sdk_error("APITimeoutError")), ProviderTimeout)
    assert isinstance(translate(_sdk_error("RateLimitError")), ProviderRateLimited)
    assert isinstance(translate(_sdk_error("InternalServerError")), ProviderUnavailable)
    assert isinstance(translate(_sdk_error("BadRequestError", status=400)), ProviderRejected)


def test_an_unrecognised_failure_is_not_assumed_temporary() -> None:
    """Fail closed. A retry loop that cannot classify an error must not spin."""
    assert translate(_sdk_error("SomethingNew")).retryable is False


def test_the_providers_own_backoff_is_used_when_it_sends_one() -> None:
    error = translate(_sdk_error("RateLimitError", retry_after="7"))
    assert error.retry_after_s == 7.0
    # A header we cannot parse is not a reason to invent a number.
    assert translate(_sdk_error("RateLimitError", retry_after="Wed, 21 Oct")).retry_after_s is None


def test_a_safe_message_never_carries_provider_detail() -> None:
    for name in ("APITimeoutError", "RateLimitError", "InternalServerError"):
        message = translate(_sdk_error(name)).safe_message
        assert "boom" not in message
        assert name not in message


# --------------------------------------------------------- bounded retries --


async def test_a_transient_failure_is_retried_and_the_turn_succeeds(premium_session) -> None:
    chat = FlakyChat(failures=1, error=ProviderRateLimited("busy", retry_after_s=0.01))

    events, _ = await _run(premium_session.id, chat)

    assert chat.calls == 2
    assert "error" not in _types(events)
    assert _finished(events).payload.message == "All sorted."


async def test_retries_are_bounded_and_the_failure_is_reported_cleanly(
    premium_session,
) -> None:
    """The alternative is a dropped SSE connection the UI has to interpret."""
    settings = get_settings()
    chat = FlakyChat(failures=99, error=ProviderRateLimited("busy", retry_after_s=0.01))

    events, _ = await _run(premium_session.id, chat)

    assert chat.calls == settings.llm_max_retries + 1

    error = _error(events)
    assert error.payload.error_code == "llm_rate_limited"
    assert error.payload.retryable is True

    finished = _finished(events)
    assert finished.payload.outcome is TurnOutcome.RECOVERABLE_FAILURE
    # No claim survives a model that never answered.
    assert not finished.payload.message
    assert finished.payload.citations == []


async def test_a_permanent_failure_is_not_retried(premium_session) -> None:
    chat = FlakyChat(failures=99, error=ProviderRejected("refused"))

    events, _ = await _run(premium_session.id, chat)

    assert chat.calls == 1
    assert _error(events).payload.retryable is False


async def test_a_retry_that_cannot_finish_in_time_is_not_slept_through(
    premium_session,
) -> None:
    """A two-second backoff inside a one-second budget helps nobody.

    The customer gets the same failure either way; sleeping first just makes
    them wait for it, and burns the turn budget that the retry was supposed to
    be spent inside.
    """
    settings = get_settings().model_copy(update={"turn_budget_s": 1.0})
    chat = FlakyChat(failures=99, error=ProviderRateLimited("busy", retry_after_s=30.0))

    async with get_sessionmaker()() as db:
        turn = Turn(
            session_id=premium_session.id,
            client_turn_id=f"res-{uuid4().hex[:8]}",
            mode=Mode.CHAT,
            input_hash=uuid4().hex[:16],
        )
        db.add(turn)
        await db.commit()

        provider = AzureOpenAIEmbeddings()
        started = time.monotonic()
        try:
            runner = TurnRunner(db, chat=chat, embeddings=provider, settings=settings)
            events = [
                event
                async for event in runner.run(
                    turn=turn, session_id=premium_session.id, user_text="hi", history=[]
                )
            ]
        finally:
            await provider.aclose()
        elapsed = time.monotonic() - started
        await db.commit()

    assert chat.calls == 1
    assert elapsed < 2.0, "the 30s retry-after was slept through"
    error = _error(events)
    assert error.payload.error_code == "llm_rate_limited"
    # The provider's own number still reaches the customer, so the UI can say
    # when to try again rather than inviting an immediate second failure.
    assert error.payload.retry_after_s == 30.0


# ------------------------------------------------------ injected faults --


async def test_an_armed_fault_reaches_the_chat_path(premium_session) -> None:
    """It did not, before this block: `POST /fault` only affected voice.

    The timing assertion is the interesting half. An injected fault raised
    outside the retry loop would surface instantly and demonstrate a failure
    mode this system does not have -- the first version of the injector did
    exactly that. Two backoffs of 0.5s and 1.0s have to have been served.
    """
    chat = RefundingChat()
    await _arm(premium_session.id, FaultKind.LLM_TIMEOUT)
    started = time.monotonic()
    try:
        events, _ = await _run(premium_session.id, chat)
    finally:
        await _arm(premium_session.id, FaultKind.NONE)
    elapsed = time.monotonic() - started

    assert _error(events).payload.error_code == "llm_timeout"
    assert _finished(events).payload.outcome is TurnOutcome.RECOVERABLE_FAILURE
    # The fault stands in for the provider, so the provider is never reached.
    assert chat.calls == 0
    assert elapsed >= 1.4, f"the retries were skipped ({elapsed:.2f}s)"


async def test_retrieval_unavailable_blocks_the_action_and_keeps_the_turn(
    premium_session,
) -> None:
    await _arm(premium_session.id, FaultKind.RETRIEVAL_UNAVAILABLE)
    try:
        events, turn = await _run(premium_session.id, RefundingChat())
    finally:
        await _arm(premium_session.id, FaultKind.NONE)

    assert "tool.failed" in _types(events)
    # The turn still finishes, and no money moved on ungrounded evidence.
    assert "turn.finished" in _types(events)
    async with get_sessionmaker()() as db:
        receipts = (
            (await db.execute(select(Refund).where(Refund.turn_id == turn.id))).scalars().all()
        )
        assert receipts == []


async def test_a_refund_that_committed_is_reported_even_when_the_call_failed(
    premium_session,
) -> None:
    """The row that matters is the receipt, not the tool's return value."""
    await _arm(premium_session.id, FaultKind.ACTION_TIMEOUT_AFTER_COMMIT)
    try:
        events, turn = await _run(premium_session.id, RefundingChat())
    finally:
        await _arm(premium_session.id, FaultKind.NONE)

    async with get_sessionmaker()() as db:
        receipt = (
            (await db.execute(select(Refund).where(Refund.turn_id == turn.id))).scalars().one()
        )

    assert "tool.failed" in _types(events)

    committed = [event for event in events if event.type == "action.committed"]
    assert len(committed) == 1
    assert committed[0].payload.receipt_ref == receipt.receipt_ref
    # Flagged as recovered, not as a duplicate submit: nobody asked twice.
    assert committed[0].payload.reconciled is True
    assert committed[0].payload.idempotent_replay is True

    finished = _finished(events)
    assert finished.payload.outcome is TurnOutcome.ACTION_SUCCESS
    assert receipt.receipt_ref in (finished.payload.message or "")


async def test_a_turn_with_no_receipt_reconciles_to_nothing(premium_session) -> None:
    """The reconciliation must not invent a commit for an ordinary turn."""
    events, _ = await _run(premium_session.id, FlakyChat(failures=0, error=RuntimeError()))

    assert "action.committed" not in _types(events)


@pytest.mark.parametrize("fault", sorted(FaultKind, key=lambda f: f.value))
async def test_every_fault_kind_is_implemented(fault: FaultKind, premium_session) -> None:
    """A demo control that silently does nothing is worse than no control.

    Each armed fault must change the turn in some visible way -- an error
    event, a failed tool call, or a recovered receipt -- so the drop-down in
    the UI cannot offer an option that does nothing on camera.
    """
    if fault is FaultKind.NONE:
        pytest.skip("the absence of a fault")

    await _arm(premium_session.id, fault)
    try:
        events, _ = await _run(premium_session.id, RefundingChat())
    finally:
        await _arm(premium_session.id, FaultKind.NONE)

    types = _types(events)
    visible = (
        "error" in types
        or "tool.failed" in types
        or any(event.type == "action.committed" and event.payload.reconciled for event in events)
    )
    assert visible, f"{fault.value} produced no visible effect: {sorted(set(types))}"
