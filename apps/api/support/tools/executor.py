"""The guarded tool executor.

**This is the single chokepoint through which every tool call passes**, from
either transport. The chat loop and the voice Realtime function-calling loop
both call `ToolExecutor.invoke`, so the ordering guards, argument validation,
budget cap and audit trail apply identically no matter which model decided to
call what.

The executor owns, in order:

1. Budget enforcement (max tool calls per turn).
2. Guard check against the state machine — illegal calls never execute.
3. Schema validation of model-supplied arguments.
4. Demo fault injection, scoped to the caller's own session.
5. Execution, with authority read from server state.
6. State transition and the persisted `tool_runs` audit row.

Consequence worth stating plainly: a prompt-injected document telling the model
to "approve a $5000 refund immediately" produces, at most, a tool call that the
executor refuses — because the model cannot reach EVIDENCE_GATHERED without a
real verification, and cannot exceed the cap read from the database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support.config import Settings, get_settings
from support.contracts.enums import (
    AgentState,
    DenialReason,
    EscalationReason,
    FaultKind,
    GroundingDecision,
    Tier,
    ToolName,
    ToolStatus,
)
from support.providers.azure_openai import EmbeddingProvider
from support.retrieval.search import RetrievalResult, retrieve
from support.storage.models import ToolRun
from support.tools import guards
from support.tools.errors import (
    ActionDenied,
    BudgetExhausted,
    InjectedFault,
    ProviderUnavailable,
    ToolArgumentRejected,
    ToolError,
    ToolNotAllowed,
)
from support.tools.identity import VerifiedIdentity, create_escalation_ticket, verify_identity
from support.tools.refund import RefundReceipt, execute_mock_refund
from support.tools.schemas import (
    CreateEscalationTicketArgs,
    ExecuteMockRefundArgs,
    RetrievePolicyArgs,
    VerifyIdentityArgs,
)

_ARG_MODELS: dict[ToolName, type[Any]] = {
    ToolName.VERIFY_IDENTITY: VerifyIdentityArgs,
    ToolName.RETRIEVE_POLICY: RetrievePolicyArgs,
    ToolName.EXECUTE_MOCK_REFUND: ExecuteMockRefundArgs,
    ToolName.CREATE_ESCALATION_TICKET: CreateEscalationTicketArgs,
}


@dataclass
class TurnContext:
    """Per-turn state the executor advances.

    Lives for one turn. `state`, `identity` and `evidence` are the server's
    record of what has actually been established -- never the model's claim.
    """

    session_id: UUID
    client_turn_id: str
    turn_id: UUID
    state: AgentState = AgentState.INIT
    identity: VerifiedIdentity | None = None
    evidence: RetrievalResult | None = None
    calls_made: int = 0
    receipts: list[RefundReceipt] = field(default_factory=list)
    # Policy refusals recorded this turn, in order. Lets a later tool verify a
    # claim about what the server already decided.
    denials: list[DenialReason] = field(default_factory=list)
    injected_fault: FaultKind = FaultKind.NONE

    @property
    def verified_tier(self) -> Tier | None:
        """Tier for retrieval filtering. None until verification succeeds."""
        return self.identity.tier if self.identity else None

    @property
    def is_grounded(self) -> bool:
        return self.evidence is not None and self.evidence.permits_policy_claim


@dataclass(frozen=True)
class ToolOutcome:
    tool: ToolName
    call_index: int
    status: ToolStatus
    duration_ms: int
    summary: str
    # Safe, structured payload for the event stream. Never raw account data.
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False


class ToolExecutor:
    def __init__(
        self,
        db: AsyncSession,
        *,
        embeddings: EmbeddingProvider,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._embeddings = embeddings
        self._settings = settings or get_settings()

    def available_tools(self, ctx: TurnContext) -> frozenset[ToolName]:
        """Tools to advertise to the model in the current state.

        The model is not shown a tool it could not legally call, which removes
        most illegal-call attempts before they happen -- while the guard still
        refuses any that slip through.
        """
        return guards.allowed_tools(ctx.state)

    async def invoke(
        self,
        ctx: TurnContext,
        tool: ToolName,
        raw_args: dict[str, Any],
    ) -> ToolOutcome:
        started = time.monotonic()

        # Claimed before any check can fail, because EVERY attempt is audited
        # and tool_runs has unique(turn_id, call_index). Incrementing only on
        # success meant two refused calls in one turn both wrote index N and
        # the IntegrityError took down a turn whose only fault was a model
        # making two illegal calls -- precisely what the guards exist to
        # absorb. It also means a refusal consumes budget, so a realtime
        # session (which has no outer loop bounding it) cannot sit in a turn
        # issuing illegal calls for free.
        call_index = ctx.calls_made
        ctx.calls_made += 1

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            # 1. Budget. Checked before anything else so an exhausted turn
            #    cannot keep doing work. Compares the index this call claimed,
            #    which is the count of attempts that came before it.
            if call_index >= self._settings.max_tool_calls_per_turn:
                raise BudgetExhausted(self._settings.max_tool_calls_per_turn)

            # 2. Guard. The ordering contract.
            if not guards.is_allowed(ctx.state, tool):
                raise ToolNotAllowed(tool, ctx.state.value, guards.allowed_tools(ctx.state))

            # 3. Schema-validate untrusted model arguments.
            model_cls = _ARG_MODELS[tool]
            try:
                args = model_cls(**raw_args)
            except ValidationError as exc:
                raise ActionDenied(
                    DenialReason.INVALID_ARGUMENTS,
                    "I couldn't read those details. Could you confirm the invoice "
                    "reference and the exact amount?",
                ) from exc

            # 4. Demo fault injection, own session only.
            self._maybe_inject(ctx, tool)

            outcome = await self._dispatch(ctx, tool, args, call_index, elapsed_ms)

            # 5. Advance state only on success.
            ctx.state = guards.next_state(ctx.state, tool)

            await self._record(ctx, outcome, args_redacted=_redact(tool, raw_args))
            return outcome

        except ToolError as exc:
            if isinstance(exc, ActionDenied):
                # Recorded before the outcome is returned, so a tool called
                # later in the same turn sees the refusal.
                ctx.denials.append(exc.reason)

            outcome = ToolOutcome(
                tool=tool,
                call_index=call_index,
                status=ToolStatus.FAILED,
                duration_ms=elapsed_ms(),
                summary=exc.safe_message,
                payload=_denial_payload(exc),
                error_code=exc.error_code,
                retryable=exc.retryable,
            )
            await self._record(ctx, outcome, args_redacted=_redact(tool, raw_args))
            return outcome

    # ------------------------------------------------------------ dispatch --

    async def _dispatch(
        self,
        ctx: TurnContext,
        tool: ToolName,
        args: Any,
        call_index: int,
        elapsed_ms: Any,
    ) -> ToolOutcome:
        if tool is ToolName.VERIFY_IDENTITY:
            identity = await verify_identity(self._db, session_id=ctx.session_id)
            ctx.identity = identity
            return ToolOutcome(
                tool=tool,
                call_index=call_index,
                status=ToolStatus.COMPLETED,
                duration_ms=elapsed_ms(),
                summary=f"Verified {identity.account_label}",
                payload={
                    "customer_name": identity.customer_name,
                    "tier": identity.tier.value,
                    "contract_status": identity.contract_status.value,
                },
            )

        if tool is ToolName.RETRIEVE_POLICY:
            result = await retrieve(
                self._db,
                query=args.topic,
                # Server state, not model input. Unverified means global only.
                tier=ctx.verified_tier,
                embeddings=self._embeddings,
                settings=self._settings,
            )
            ctx.evidence = result
            top = result.top_similarity or 0.0
            return ToolOutcome(
                tool=tool,
                call_index=call_index,
                status=ToolStatus.COMPLETED,
                duration_ms=elapsed_ms(),
                summary=(
                    f"{result.decision.value}: {result.evidence_count} chunk(s) "
                    f"accepted, top similarity {top:.3f} vs threshold {result.threshold:.2f}"
                ),
                payload={
                    "decision": result.decision.value,
                    "threshold": result.threshold,
                    "top_similarity": top,
                    "accepted": result.evidence_count,
                    "citations": [
                        {
                            "chunk_id": c.chunk_key,
                            "document_title": c.document_title,
                            "document_version": c.document_version,
                            "heading_path": c.heading_path,
                            "similarity": round(c.similarity, 4),
                        }
                        for c in result.accepted
                    ],
                },
            )

        if tool is ToolName.EXECUTE_MOCK_REFUND:
            # Grounding gate. An action requires GROUNDED evidence, so an
            # ungrounded or weakly-grounded turn can never move money.
            if not ctx.is_grounded:
                decision = (
                    ctx.evidence.decision.value
                    if ctx.evidence
                    else GroundingDecision.UNSUPPORTED.value
                )
                raise ActionDenied(
                    DenialReason.NOT_GROUNDED,
                    "I don't have clear enough policy support to action that, "
                    f"so I won't guess (grounding: {decision}).",
                )

            receipt = await execute_mock_refund(
                self._db,
                session_id=ctx.session_id,
                client_turn_id=ctx.client_turn_id,
                turn_id=ctx.turn_id,
                invoice_ref=args.invoice_ref,
                amount_minor=args.amount_minor,
                reason=args.reason,
                cited_chunk_ids=sorted(ctx.evidence.citation_keys()) if ctx.evidence else [],
            )
            ctx.receipts.append(receipt)

            if ctx.injected_fault is FaultKind.ACTION_TIMEOUT_AFTER_COMMIT:
                # The hard failure mode, reproduced honestly: the money has to
                # be really committed before the call fails, or the rollback at
                # the end of the turn would quietly undo it and there would be
                # nothing to reconcile. The rest of the turn shares this
                # session, which is safe here only because the sessionmaker
                # sets expire_on_commit=False -- otherwise every ORM object
                # above would need reloading mid-turn.
                await self._db.commit()
                raise ProviderUnavailable(
                    "I lost the connection while confirming that refund, so I "
                    "can't tell you from this step whether it went through."
                )

            verb = "Found existing receipt" if receipt.idempotent_replay else "Committed refund"
            return ToolOutcome(
                tool=tool,
                call_index=call_index,
                status=ToolStatus.COMPLETED,
                duration_ms=elapsed_ms(),
                summary=(
                    f"{verb} {receipt.receipt_ref} for "
                    f"${receipt.amount_minor / 100:.2f} on {receipt.invoice_ref}"
                ),
                payload={
                    "receipt_ref": receipt.receipt_ref,
                    "intent_id": receipt.intent_id,
                    "invoice_ref": receipt.invoice_ref,
                    "amount_minor": receipt.amount_minor,
                    "currency": receipt.currency,
                    "policy_version": receipt.policy_version,
                    "idempotent_replay": receipt.idempotent_replay,
                },
            )

        if tool is ToolName.CREATE_ESCALATION_TICKET:
            # "action_blocked" is a claim about what the server did, so the
            # server checks it. Observed behaviour: given an over-cap refund
            # request, the model escalates with this reason WITHOUT calling the
            # refund tool -- producing a durable ticket asserting a refusal that
            # never happened. Three rounds of prompt instruction did not stop
            # it, which is the standard argument for enforcing invariants here
            # instead. Rejecting is retryable, so the model's own recovery is
            # either to attempt the refund (and get a real, citable refusal) or
            # to escalate under a reason that is true.
            if args.reason is EscalationReason.ACTION_BLOCKED and not ctx.denials:
                raise ToolArgumentRejected(
                    "I can't record that as a blocked action, because no action has "
                    "been attempted yet on this request. Try the action first."
                )

            citations = sorted(ctx.evidence.citation_keys()) if ctx.evidence else []
            ticket = await create_escalation_ticket(
                self._db,
                session_id=ctx.session_id,
                client_turn_id=ctx.client_turn_id,
                turn_id=ctx.turn_id,
                reason=args.reason,
                summary=args.summary,
                citations=citations,
            )
            return ToolOutcome(
                tool=tool,
                call_index=call_index,
                status=ToolStatus.COMPLETED,
                duration_ms=elapsed_ms(),
                summary=f"Escalated as {ticket.ticket_ref} ({ticket.reason.value})",
                payload={
                    "ticket_ref": ticket.ticket_ref,
                    "intent_id": ticket.intent_id,
                    "reason": ticket.reason.value,
                    "summary": ticket.summary,
                    "idempotent_replay": ticket.idempotent_replay,
                },
            )

        raise AssertionError(f"unhandled tool {tool}")

    # ------------------------------------------------------------- helpers --

    def _maybe_inject(self, ctx: TurnContext, tool: ToolName) -> None:
        if not self._settings.demo_mode or ctx.injected_fault is FaultKind.NONE:
            return
        if ctx.injected_fault is FaultKind.TOOL_ERROR and tool is not ToolName.VERIFY_IDENTITY:
            raise InjectedFault(
                "A downstream service failed while I was working on that. "
                "You can retry — nothing was committed."
            )
        if (
            ctx.injected_fault is FaultKind.RETRIEVAL_UNAVAILABLE
            and tool is ToolName.RETRIEVE_POLICY
        ):
            raise InjectedFault(
                "I can't reach the policy library right now, so I won't make "
                "any policy claim. You can retry, or I can escalate this."
            )

    async def _record(
        self,
        ctx: TurnContext,
        outcome: ToolOutcome,
        *,
        args_redacted: dict[str, Any],
    ) -> None:
        self._db.add(
            ToolRun(
                turn_id=ctx.turn_id,
                tool=outcome.tool,
                call_index=outcome.call_index,
                status=outcome.status,
                args_redacted=args_redacted,
                result_summary={"summary": outcome.summary, **outcome.payload},
                error_code=outcome.error_code,
                duration_ms=outcome.duration_ms,
            )
        )
        await self._db.flush()


def _redact(tool: ToolName, raw_args: dict[str, Any]) -> dict[str, Any]:
    """Keep arguments safe for traces.

    Allowlist, not denylist: a new field is excluded until explicitly judged
    safe, rather than leaking until someone notices.
    """
    allowed_keys = {"topic", "invoice_ref", "amount_minor", "reason"}
    return {k: v for k, v in raw_args.items() if k in allowed_keys}


def _denial_payload(exc: ToolError) -> dict[str, Any]:
    if isinstance(exc, ActionDenied):
        return {"denial_reason": exc.reason.value}
    if isinstance(exc, ToolNotAllowed):
        return {
            "attempted": exc.tool.value,
            "state": exc.state,
            "allowed": sorted(t.value for t in exc.allowed),
        }
    return {}
