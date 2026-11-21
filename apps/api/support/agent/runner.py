"""The shared turn runner.

Both transports drive this. Chat consumes the event stream over SSE; voice
reuses the same tool layer through the Realtime function-calling loop. Every
tool call goes through `ToolExecutor`, so the ordering guards apply identically.

**Why the answer is buffered rather than token-streamed.** The requirement is
that a policy-bearing turn releases text only after its citations are
validated, and citations cannot be validated until the message is complete.
Streaming raw tokens would mean publishing a policy claim before knowing
whether its cited sources exist. So the final message is buffered, validated,
and then emitted in chunks. Early feedback comes from the tool status events,
which fire well before the model finishes — "time to first visible status" is
the metric that matters, and it is a few hundred milliseconds.

Terminal outcome is always exactly one `turn.finished`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support.agent.prompts import (
    REPLY_SCHEMA,
    SYSTEM_PROMPT,
    render_evidence,
    tool_definitions,
)
from support.config import Settings, get_settings
from support.contracts.enums import (
    DenialReason,
    EscalationReason,
    FaultKind,
    GroundingDecision,
    MessageRole,
    ToolName,
    ToolStatus,
    TurnOutcome,
    TurnStatus,
)
from support.contracts.events import (
    ActionCommitted,
    ActionCommittedPayload,
    ActionDenied,
    ActionDeniedPayload,
    AnswerDelta,
    AnswerDeltaPayload,
    Citation,
    ErrorPayload,
    EscalationCreated,
    EscalationCreatedPayload,
    Event,
    GroundingDecided,
    GroundingDecidedPayload,
    IdentityVerified,
    StreamError,
    ToolCompleted,
    ToolCompletedPayload,
    ToolFailed,
    ToolFailedPayload,
    ToolStarted,
    ToolStartedPayload,
    TurnFinished,
    TurnFinishedPayload,
    TurnMetrics,
    TurnMetricsPayload,
    TurnStarted,
    TurnStartedPayload,
    VerifiedIdentityPayload,
)
from support.providers.azure_openai import (
    AzureOpenAIChat,
    ChatTurn,
    EmbeddingProvider,
    Usage,
)
from support.providers.errors import ProviderError, ProviderRateLimited, ProviderTimeout
from support.retrieval.search import RetrievalResult, validate_citations
from support.storage.models import Invoice, Message, Refund, Session, Turn, TurnMetric
from support.tools.executor import ToolExecutor, ToolOutcome, TurnContext

# Chunk size for releasing the validated answer.
_DELTA_WORDS = 6

# A retry needs room to actually run, not just to start. Sleeping until the
# turn deadline and then issuing a call that is cancelled mid-flight spends the
# customer's time to deliver the same failure.
_MIN_RETRY_HEADROOM_S = 2.0

logger = logging.getLogger("support.agent")

# Recovers the answer from a JSON envelope that would not parse. Non-greedy up
# to the first unescaped quote, so an escaped quote inside the answer survives.
_ANSWER_FIELD_RE = re.compile(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"')

# Recovers citation refs from the same partial envelope.
_REFS_FIELD_RE = re.compile(r'"cited_refs"\s*:\s*\[([^\]]*)\]')


@dataclass
class _Accumulator:
    usage_in: int = 0
    usage_out: int = 0
    tool_calls: int = 0
    ttft_ms: int | None = None
    waterfall: list[dict[str, Any]] = field(default_factory=list)
    prompt_steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, usage: Usage) -> None:
        self.usage_in += usage.input_tokens or 0
        self.usage_out += usage.output_tokens or 0


class TurnRunner:
    def __init__(
        self,
        db: AsyncSession,
        *,
        chat: AzureOpenAIChat,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._chat = chat
        self._settings = settings or get_settings()
        self._executor = ToolExecutor(db, embeddings=embeddings, settings=self._settings)

    async def run(
        self,
        *,
        turn: Turn,
        session_id: UUID,
        user_text: str,
        history: list[Message],
    ) -> AsyncIterator[Event]:
        started = time.monotonic()
        seq = _Sequencer(session_id=session_id, turn_id=turn.id)
        acc = _Accumulator()

        ctx = TurnContext(
            session_id=session_id,
            client_turn_id=turn.client_turn_id,
            turn_id=turn.id,
            injected_fault=await self._armed_fault(session_id),
        )

        yield seq.make(
            TurnStarted,
            TurnStartedPayload(mode=turn.mode.value, client_turn_id=turn.client_turn_id),
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for msg in history[-12:]:
            if msg.interrupted:
                # An interrupted draft is shown in the UI but withheld from model
                # context, so the model never continues from cut-off text.
                continue
            messages.append({"role": msg.role.value, "content": msg.content})
        messages.append({"role": "user", "content": user_text})

        outcome = TurnOutcome.RECOVERABLE_FAILURE
        final_text = ""
        cited_refs: list[int] = []
        grounding_emitted = False
        commit_reported = False

        deadline = started + self._settings.turn_budget_s

        try:
            async with asyncio.timeout(self._settings.turn_budget_s):
                for _ in range(self._settings.max_tool_calls_per_turn + 1):
                    available = self._executor.available_tools(ctx)
                    acc.prompt_steps.append(
                        _prompt_step(len(acc.prompt_steps), messages, available)
                    )
                    response = await self._complete(
                        ctx,
                        messages=messages,
                        tools=tool_definitions(available),
                        deadline=deadline,
                    )
                    acc.add(response.usage)
                    _record_response(acc.prompt_steps[-1], response)

                    if not response.tool_calls:
                        final_text, cited_refs = _parse_reply(response.text)
                        break

                    assistant_entry: dict[str, Any] = {
                        "role": "assistant",
                        "content": response.text or None,
                        "tool_calls": [
                            {
                                "id": c.call_id,
                                "type": "function",
                                "function": {
                                    "name": c.name,
                                    "arguments": _json_dumps(c.arguments),
                                },
                            }
                            for c in response.tool_calls
                        ],
                    }
                    messages.append(assistant_entry)

                    for call in response.tool_calls:
                        try:
                            tool = ToolName(call.name)
                        except ValueError:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.call_id,
                                    "content": f"Unknown tool {call.name!r}.",
                                }
                            )
                            continue

                        if acc.ttft_ms is None:
                            acc.ttft_ms = int((time.monotonic() - started) * 1000)

                        yield seq.make(
                            ToolStarted,
                            ToolStartedPayload(
                                tool=tool,
                                call_index=ctx.calls_made,
                                summary=_starting_summary(tool),
                            ),
                        )

                        step_start_ms = int((time.monotonic() - started) * 1000)
                        result = await self._executor.invoke(ctx, tool, call.arguments)
                        acc.tool_calls += 1
                        # start_ms is an offset on the turn's own clock, not a
                        # duration. The drawer needs offsets to show the gaps
                        # between calls, which are model latency and are usually
                        # the larger share of the turn -- a stacked bar of tool
                        # durations alone would imply the tools were the cost.
                        acc.waterfall.append(
                            {
                                "call_index": result.call_index,
                                "tool": tool.value,
                                "status": result.status.value,
                                "start_ms": step_start_ms,
                                "ms": result.duration_ms,
                            }
                        )

                        if result.status is ToolStatus.COMPLETED:
                            yield seq.make(
                                ToolCompleted,
                                ToolCompletedPayload(
                                    tool=tool,
                                    call_index=result.call_index,
                                    duration_ms=result.duration_ms,
                                    summary=result.summary,
                                ),
                            )
                        else:
                            yield seq.make(
                                ToolFailed,
                                ToolFailedPayload(
                                    tool=tool,
                                    call_index=result.call_index,
                                    duration_ms=result.duration_ms,
                                    error_code=result.error_code or "tool_error",
                                    retryable=result.retryable,
                                    summary=result.summary,
                                ),
                            )

                            # A policy refusal is not the same kind of event as a
                            # tool malfunction: it is a decision, it has a reason
                            # from the enum, and it is backed by the same evidence
                            # the answer would have cited. It gets its own event so
                            # the UI can show the reason and the clause behind it
                            # rather than an error code.
                            denial = result.payload.get("denial_reason")
                            if denial is not None:
                                yield seq.make(
                                    ActionDenied,
                                    ActionDeniedPayload(
                                        reason=DenialReason(str(denial)),
                                        explanation=result.summary,
                                        citations=_evidence_citations(ctx),
                                    ),
                                )
                                outcome = TurnOutcome.POLICY_DENIAL

                        # Side events derived from server state, not model text.
                        if tool is ToolName.VERIFY_IDENTITY and ctx.identity:
                            yield seq.make(
                                IdentityVerified,
                                VerifiedIdentityPayload(
                                    customer_name=ctx.identity.customer_name,
                                    tier=ctx.identity.tier,
                                    contract_status=ctx.identity.contract_status,
                                ),
                            )

                        if (
                            tool is ToolName.RETRIEVE_POLICY
                            and ctx.evidence
                            and not grounding_emitted
                        ):
                            grounding_emitted = True
                            yield seq.make(
                                GroundingDecided,
                                _grounding_payload(ctx),
                            )

                        if tool is ToolName.EXECUTE_MOCK_REFUND and result.payload.get(
                            "receipt_ref"
                        ):
                            p = result.payload
                            yield seq.make(
                                ActionCommitted,
                                ActionCommittedPayload(
                                    receipt_ref=str(p["receipt_ref"]),
                                    intent_id=str(p["intent_id"]),
                                    invoice_ref=str(p["invoice_ref"]),
                                    amount_minor=int(p["amount_minor"]),
                                    currency=str(p["currency"]),
                                    policy_version=str(p["policy_version"]),
                                    idempotent_replay=bool(p["idempotent_replay"]),
                                ),
                            )
                            commit_reported = True
                            outcome = TurnOutcome.ACTION_SUCCESS

                        if tool is ToolName.CREATE_ESCALATION_TICKET and result.payload.get(
                            "ticket_ref"
                        ):
                            p = result.payload
                            yield seq.make(
                                EscalationCreated,
                                EscalationCreatedPayload(
                                    ticket_ref=str(p["ticket_ref"]),
                                    intent_id=str(p["intent_id"]),
                                    reason=EscalationReason(p["reason"]),
                                    summary=str(p["summary"]),
                                ),
                            )
                            outcome = TurnOutcome.ESCALATION

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.call_id,
                                "content": tool_result_for_model(ctx, tool, result),
                            }
                        )
                else:
                    outcome = TurnOutcome.BUDGET_EXHAUSTED
                    final_text = (
                        "I wasn't able to finish that within one turn. "
                        "Could you tell me the invoice reference and amount again?"
                    )

        except TimeoutError:
            yield seq.make(
                StreamError,
                ErrorPayload(
                    error_code="turn_timeout",
                    message="That took longer than expected. Nothing was committed — "
                    "you can try again.",
                    retryable=True,
                ),
            )
            final_text = ""
            outcome = TurnOutcome.RECOVERABLE_FAILURE

        except ProviderError as exc:
            # The provider gave up, or gave up being worth retrying. The turn
            # ends with a named reason and no claim: a model that never
            # answered has not said anything about the policy.
            logger.warning(
                "provider failure turn=%s code=%s retryable=%s",
                turn.id,
                exc.error_code,
                exc.retryable,
            )
            yield seq.make(
                StreamError,
                ErrorPayload(
                    error_code=exc.error_code,
                    message=exc.safe_message,
                    retryable=exc.retryable,
                    retry_after_s=exc.retry_after_s,
                ),
            )
            final_text = ""
            outcome = TurnOutcome.RECOVERABLE_FAILURE

        # Money first, words second. If a refund committed but the call that
        # committed it never reported back, the receipt is recovered here and
        # the turn says so -- before any text is composed around the idea that
        # nothing happened.
        if not commit_reported:
            for event in await self._reconcile_committed_actions(ctx, seq):
                yield event
                outcome = TurnOutcome.ACTION_SUCCESS
                final_text = (
                    "That refund did go through — the confirmation came back late, "
                    "so I checked the record. Your receipt reference is "
                    f"{event.payload.receipt_ref}."
                )

        # Citations: resolve the refs, then validate before releasing any text.
        cited = _resolve_refs(cited_refs, ctx.evidence)
        display_text = final_text
        citations: list[Citation] = []
        citations_refused = False

        if ctx.evidence is not None and cited:
            ok, invalid = validate_citations(cited, ctx.evidence)
            if not ok:
                # The model cited something outside the accepted evidence, so
                # the claim is not released.
                #
                # The ids go to the server log and NOT to the customer: the
                # count is all a customer can act on, while the ids are what an
                # operator needs to tell a mis-copied id apart from a
                # fabricated one. Seen in production roughly one turn in four
                # on an otherwise well-grounded query, so it needs to be
                # diagnosable rather than just counted.
                logger.warning(
                    "citation validation failed turn=%s invalid=%s accepted=%s",
                    turn.id,
                    sorted(invalid),
                    sorted(ctx.evidence.citation_keys()),
                )
                yield seq.make(
                    StreamError,
                    ErrorPayload(
                        error_code="citation_validation_failed",
                        message=(
                            "I couldn't verify the policy sources for that answer, so "
                            f"I won't state it ({len(invalid)} unverifiable citation(s))."
                        ),
                        # Asking again usually works: this is a per-reply slip,
                        # not a property of the question.
                        retryable=True,
                    ),
                )
                display_text = (
                    "I couldn't confirm the policy sources for that, so I'd rather "
                    "not guess. I can pass this to a billing specialist."
                )
                # Not ESCALATION. No ticket row exists -- the reply *offers* a
                # specialist, it does not reach one -- and an "Escalated" chip
                # above an answer with no ticket reference is a claim the
                # database does not support. The same rule the voice path
                # follows when it reads an outcome back off the audit rows.
                outcome = TurnOutcome.RECOVERABLE_FAILURE
                # And it must stay that: the inference below would read the
                # grounded evidence and relabel this turn a grounded answer,
                # which is precisely the answer that was just withheld.
                citations_refused = True
            else:
                by_key = {c.chunk_key: c for c in ctx.evidence.accepted}
                citations = [
                    Citation(
                        chunk_id=key,
                        document_title=by_key[key].document_title,
                        document_version=by_key[key].document_version,
                        heading_path=by_key[key].heading_path,
                        similarity=round(by_key[key].similarity, 4),
                    )
                    for key in dict.fromkeys(cited)
                    if key in by_key
                ]

        if outcome is TurnOutcome.RECOVERABLE_FAILURE and display_text and not citations_refused:
            outcome = _infer_outcome(ctx)

        for chunk in _chunk_words(display_text, _DELTA_WORDS):
            if acc.ttft_ms is None:
                acc.ttft_ms = int((time.monotonic() - started) * 1000)
            yield seq.make(AnswerDelta, AnswerDeltaPayload(text=chunk))

        if ctx.evidence is not None and not grounding_emitted:
            yield seq.make(GroundingDecided, _grounding_payload(ctx))

        total_ms = int((time.monotonic() - started) * 1000)
        cost = self._estimate_cost(acc)

        yield seq.make(
            TurnMetrics,
            TurnMetricsPayload(
                ttft_ms=acc.ttft_ms,
                total_ms=total_ms,
                tool_calls=acc.tool_calls,
                tokens_in=acc.usage_in or None,
                tokens_out=acc.usage_out or None,
                cost_estimate_usd=cost,
                cost_rates_effective=self._settings.cost_rates_effective,
                model=self._chat.model,
            ),
        )

        await self._persist(
            turn=turn,
            session_id=session_id,
            user_text=user_text,
            answer=display_text,
            outcome=outcome,
            acc=acc,
            total_ms=total_ms,
            cost=cost,
            ctx=ctx,
            citations=citations,
        )

        yield seq.make(
            TurnFinished,
            TurnFinishedPayload(
                outcome=outcome,
                message=display_text or None,
                citations=citations,
            ),
        )

    # ------------------------------------------------------------- helpers --

    async def _armed_fault(self, session_id: UUID) -> FaultKind:
        """Whatever fault this session armed for itself, if any.

        Read here rather than passed in because the chat path builds its own
        TurnContext. It was missing: `POST /fault` wrote the row, the voice
        path honoured it, and chat silently ignored it -- the demo control
        did nothing on the mode it would most often be demonstrated in.
        """
        if not self._settings.demo_mode:
            return FaultKind.NONE
        armed = (
            await self._db.execute(select(Session.injected_fault).where(Session.id == session_id))
        ).scalar_one_or_none()
        if not armed:
            return FaultKind.NONE
        try:
            return FaultKind(armed)
        except ValueError:
            logger.warning("unknown fault armed on session=%s: %r", session_id, armed)
            return FaultKind.NONE

    async def _complete(
        self,
        ctx: TurnContext,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
    ) -> ChatTurn:
        """One model call, retried while that is both allowed and useful.

        Two bounds, not one. The attempt count stops a permanent failure from
        spinning, and the turn deadline stops a retry that cannot finish: there
        is no point sleeping two seconds for a 429 when the turn has one second
        left, because the customer gets the same failure either way, later. In
        that case the error is raised immediately with the provider's own
        retry-after still attached, so the UI can tell them when to try again.
        """
        attempt = 0
        while True:
            try:
                # Inside the try on purpose: an injected provider failure has
                # to travel the same retry path a real one does, or the demo
                # would be showing a failure mode the system does not have.
                _maybe_inject_llm(ctx, self._settings)
                return await self._chat.complete(
                    messages=messages,
                    tools=tools,
                    response_format=REPLY_SCHEMA,
                )
            except ProviderError as exc:
                attempt += 1
                if not exc.retryable or attempt > self._settings.llm_max_retries:
                    raise
                delay = exc.retry_after_s
                if delay is None:
                    delay = self._settings.llm_retry_base_s * (2 ** (attempt - 1))
                if time.monotonic() + delay + _MIN_RETRY_HEADROOM_S > deadline:
                    raise
                logger.info(
                    "retrying after %s in %.2fs (attempt %d)", exc.error_code, delay, attempt
                )
                await asyncio.sleep(delay)

    async def _reconcile_committed_actions(
        self,
        ctx: TurnContext,
        seq: _Sequencer,
    ) -> list[Any]:
        """Report money that moved even though the call reporting it failed.

        The receipt row is the record of a refund, not the tool's return value.
        If a refund committed and then the confirmation was lost -- a timeout
        after the write, which `action_timeout_after_commit` injects -- the
        turn would otherwise end saying nothing happened while the customer's
        balance says otherwise.

        Reconciling inside the turn rather than leaving a "pending" card is a
        deliberate departure from the original plan: the server can resolve the
        question immediately and completely, and showing a customer an unknown
        money state we could have resolved is worse than a brief delay.
        """
        rows = (
            (await self._db.execute(select(Refund).where(Refund.turn_id == ctx.turn_id)))
            .scalars()
            .all()
        )
        events: list[Any] = []
        for row in rows:
            invoice_ref = (
                await self._db.execute(
                    select(Invoice.external_ref).where(Invoice.id == row.invoice_id)
                )
            ).scalar_one()
            events.append(
                seq.make(
                    ActionCommitted,
                    ActionCommittedPayload(
                        receipt_ref=row.receipt_ref,
                        intent_id=row.intent_id,
                        invoice_ref=invoice_ref,
                        amount_minor=row.amount_minor,
                        currency=row.currency,
                        policy_version=row.policy_version,
                        idempotent_replay=True,
                        reconciled=True,
                    ),
                )
            )
        return events

    def _estimate_cost(self, acc: _Accumulator) -> float | None:
        if not acc.usage_in and not acc.usage_out:
            return None
        s = self._settings
        return round(
            acc.usage_in / 1000 * s.cost_per_1k_input_tokens_usd
            + acc.usage_out / 1000 * s.cost_per_1k_output_tokens_usd,
            6,
        )

    async def _persist(
        self,
        *,
        turn: Turn,
        session_id: UUID,
        user_text: str,
        answer: str,
        outcome: TurnOutcome,
        acc: _Accumulator,
        total_ms: int,
        cost: float | None,
        ctx: TurnContext,
        citations: list[Citation],
    ) -> None:
        self._db.add(
            Message(
                session_id=session_id,
                turn_id=turn.id,
                role=MessageRole.USER,
                mode=turn.mode,
                content=user_text,
            )
        )
        if answer:
            self._db.add(
                Message(
                    session_id=session_id,
                    turn_id=turn.id,
                    role=MessageRole.ASSISTANT,
                    mode=turn.mode,
                    content=answer,
                )
            )

        turn.status = TurnStatus.FINISHED
        turn.outcome = outcome
        turn.finished_at = datetime.now(UTC)

        self._db.add(
            TurnMetric(
                turn_id=turn.id,
                ttft_ms=acc.ttft_ms,
                total_ms=total_ms,
                tool_calls=acc.tool_calls,
                tokens_in=acc.usage_in or None,
                tokens_out=acc.usage_out or None,
                cost_estimate_usd=cost,
                cost_rates_effective=self._settings.cost_rates_effective,
                model=self._chat.model,
                grounding_decision=ctx.evidence.decision if ctx.evidence else None,
                top_similarity=ctx.evidence.top_similarity if ctx.evidence else None,
                waterfall={
                    "steps": acc.waterfall,
                    # The prompt trace lives beside the timing trace: both
                    # answer "what actually happened in this turn", and both
                    # are read by the same drawer.
                    "prompt": acc.prompt_steps,
                    "citations": [c.chunk_id for c in citations],
                    # Recorded per turn rather than read from config when the
                    # drawer renders: retuning the threshold must not silently
                    # rewrite the history of decisions taken under the old one.
                    "grounding": (
                        {
                            "threshold": ctx.evidence.threshold,
                            "weak_margin": self._settings.grounding_weak_margin,
                        }
                        if ctx.evidence is not None
                        else None
                    ),
                },
            )
        )
        await self._db.flush()


class _Sequencer:
    """Monotonic per-turn sequence numbers."""

    def __init__(self, *, session_id: UUID, turn_id: UUID) -> None:
        self._session_id = session_id
        self._turn_id = turn_id
        self._next = 0

    def make(self, event_cls: Any, payload: Any) -> Any:
        event = event_cls(
            session_id=self._session_id,
            turn_id=self._turn_id,
            sequence=self._next,
            timestamp=datetime.now(UTC),
            payload=payload,
        )
        self._next += 1
        return event


def _evidence_citations(ctx: TurnContext) -> list[Citation]:
    """Citations for the accepted evidence of this turn.

    Used for a denial, where the customer is owed the clause that refused them
    and the model has no say in which sources are shown.
    """
    if ctx.evidence is None:
        return []
    return [
        Citation(
            chunk_id=c.chunk_key,
            document_title=c.document_title,
            document_version=c.document_version,
            heading_path=c.heading_path,
            similarity=round(c.similarity, 4),
        )
        for c in ctx.evidence.accepted
    ]


def _grounding_payload(ctx: TurnContext) -> GroundingDecidedPayload:
    assert ctx.evidence is not None
    ev = ctx.evidence
    return GroundingDecidedPayload(
        decision=ev.decision,
        threshold=ev.threshold,
        top_similarity=ev.top_similarity,
        accepted_chunks=ev.evidence_count,
        citations=_evidence_citations(ctx),
    )


def _infer_outcome(ctx: TurnContext) -> TurnOutcome:
    if ctx.evidence is None:
        return TurnOutcome.CLARIFICATION
    if ctx.evidence.decision is GroundingDecision.GROUNDED:
        return TurnOutcome.GROUNDED_ANSWER
    if ctx.evidence.decision is GroundingDecision.WEAK:
        return TurnOutcome.CLARIFICATION
    return TurnOutcome.ESCALATION


def _starting_summary(tool: ToolName) -> str:
    return {
        ToolName.VERIFY_IDENTITY: "Verifying account...",
        ToolName.RETRIEVE_POLICY: "Searching policy library...",
        ToolName.EXECUTE_MOCK_REFUND: "Checking refund eligibility...",
        ToolName.CREATE_ESCALATION_TICKET: "Creating escalation ticket...",
    }[tool]


def tool_result_for_model(ctx: TurnContext, tool: ToolName, result: ToolOutcome) -> str:
    """What the model sees back from a tool.

    Retrieval returns the evidence envelope, which states that policy text is
    reference data rather than instructions.
    """
    if tool is ToolName.RETRIEVE_POLICY and ctx.evidence is not None:
        return f"{result.summary}\n\n{render_evidence(ctx.evidence)}"
    return result.summary


def _parse_reply(raw: str) -> tuple[str, list[int]]:
    """Read the structured reply.

    Falls back to treating the content as prose if it is not valid JSON, so a
    provider that ignores response_format degrades to a plain answer with
    best-effort citation recovery rather than failing the turn.

    Uses raw_decode rather than json.loads because the observed failure is not
    invalid JSON -- it is *two* complete objects concatenated in one message,
    which the service returned despite `strict: true` on the json_schema. A
    plain loads() raises on that, the prose fallback then streams the literal
    `{"answer": ...}` to the customer, and the reply reads as a bug. Taking the
    first complete value and discarding any trailing bytes keeps a provider
    quirk from reaching the transcript.
    """
    text = (raw or "").strip()
    if not text:
        return "", []
    try:
        parsed, _end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return _prose_fallback(text)
    if not isinstance(parsed, dict):
        return _prose_fallback(text)
    answer = str(parsed.get("answer", "")).strip()
    refs = _coerce_refs(parsed.get("cited_refs", []))
    if not answer:
        # A well-formed envelope with an empty answer is useless to the
        # customer; prefer recovering prose over releasing nothing.
        return _prose_fallback(text)
    return answer, refs


# How much of each message the trace keeps. Enough to see what the model was
# actually working from, short enough that a trace is not a second copy of the
# transcript sitting in the database.
_PROMPT_PREVIEW_CHARS = 240


def _prompt_step(
    index: int, messages: list[dict[str, Any]], available: frozenset[ToolName]
) -> dict[str, Any]:
    """What this model call is about to be given.

    Recorded per call rather than once per turn, because the interesting part
    of a tool-using loop is how the input grows: the second call carries the
    first call's result, and the call that finally answers carries the policy
    evidence. A single "the prompt" would hide exactly that.

    Message bodies are previewed, not stored whole. Everything here is already
    visible to this customer in their own transcript -- the system prompt is
    static, the user text is theirs, and tool results reach the trace already
    redacted by the executor. The verification code never enters model context
    in the first place, so it cannot appear here.
    """
    return {
        "call": index,
        "tools_offered": sorted(tool.value for tool in available),
        "messages": [
            {
                "role": str(message.get("role", "?")),
                "chars": len(str(message.get("content") or "")),
                "preview": _preview(str(message.get("content") or "")),
                # The assistant turn that requested tools has no content, so
                # name them or the step reads as an empty message.
                "tool_calls": [
                    str(call.get("function", {}).get("name", "?"))
                    for call in message.get("tool_calls") or []
                ],
            }
            for message in messages
        ],
    }


def _record_response(step: dict[str, Any], response: ChatTurn) -> None:
    """What came back, attached to the call that asked for it."""
    step["response"] = {
        "tool_calls": [call.name for call in response.tool_calls],
        "text_chars": len(response.text or ""),
        "preview": _preview(response.text or ""),
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
    }


def _preview(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _PROMPT_PREVIEW_CHARS:
        return collapsed
    return collapsed[:_PROMPT_PREVIEW_CHARS] + "..."


def _maybe_inject_llm(ctx: TurnContext, settings: Settings) -> None:
    """Demo-only model-side faults, armed by the caller on their own session.

    Raised as the same typed errors the adapter produces for a real provider
    failure, so the retry policy, the event contract and the UI are all
    exercised by the demo rather than approximated by it. Every attempt fails
    while the fault is armed, which is what makes "bounded retry, then a clean
    message" visible instead of theoretical.
    """
    if not settings.demo_mode:
        return
    if ctx.injected_fault is FaultKind.LLM_TIMEOUT:
        raise ProviderTimeout("The model took too long to respond. Nothing was committed.")
    if ctx.injected_fault is FaultKind.RATE_LIMIT:
        raise ProviderRateLimited(
            "The model service is rate limiting us right now. Nothing was committed.",
            retry_after_s=1.0,
        )


def _resolve_refs(refs: list[int], evidence: RetrievalResult | None) -> list[str]:
    """Turn the model's ref numbers into chunk keys.

    The mapping is this turn's accepted evidence in order, which is the same
    order it was rendered in, so the model never handles a key and cannot
    invent one. A ref outside the range is not silently dropped: it is passed
    through as an unmistakable non-key so the citation guard refuses the answer
    exactly as it would for a fabricated id. Dropping it instead would release
    a policy claim whose stated source does not exist.
    """
    if evidence is None:
        return []
    accepted = evidence.accepted
    out: list[str] = []
    for ref in refs:
        if 1 <= ref <= len(accepted):
            out.append(accepted[ref - 1].chunk_key)
        else:
            out.append(f"ref:{ref} (outside 1..{len(accepted)})")
    return out


def _coerce_refs(value: Any) -> list[int]:
    """Accept the refs the schema promises, and tolerate near-misses.

    `strict: true` should deliver integers, but the provider that once returned
    two envelopes in one message has earned a parser that does not crash on
    `"2"` or `2.0`. Anything that is not a whole number is dropped rather than
    guessed at, and an out-of-range ref is refused later along with the rest of
    the citation checking.
    """
    if not isinstance(value, list):
        return []
    refs: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            refs.append(item)
        elif isinstance(item, float) and item.is_integer():
            refs.append(int(item))
        elif isinstance(item, str):
            digits = item.strip().strip("[]#")
            if digits.isdigit():
                refs.append(int(digits))
    return refs


def _prose_fallback(text: str) -> tuple[str, list[int]]:
    """Last resort when the reply is not a usable JSON envelope.

    Any JSON-looking wrapper is stripped before release. Refs are recovered by
    pattern from a truncated envelope, and still go through the same
    server-side range check as structured ones -- a ref this turn's evidence
    does not have is refused regardless of how it was parsed.
    """
    answer = text
    match = _ANSWER_FIELD_RE.search(text)
    if match is not None:
        try:
            answer = str(json.loads(f'"{match.group(1)}"'))
        except json.JSONDecodeError:
            answer = match.group(1)
    elif text.startswith("{"):
        # JSON that cannot even be pattern-matched is withheld rather than
        # shown raw; the turn still carries its tool results and events.
        answer = ""

    refs: list[int] = []
    found = _REFS_FIELD_RE.search(text)
    if found is not None:
        refs = _coerce_refs([part for part in found.group(1).split(",") if part.strip()])
    return answer.strip(), refs


def _chunk_words(text: str, size: int) -> list[str]:
    if not text:
        return []
    words = text.split(" ")
    return [
        " ".join(words[i : i + size]) + (" " if i + size < len(words) else "")
        for i in range(0, len(words), size)
    ]


def _json_dumps(value: Any) -> str:
    return str(json.dumps(value))
