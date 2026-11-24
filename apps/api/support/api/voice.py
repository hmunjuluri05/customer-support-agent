"""Voice endpoints.

Three routes, and the whole point of them is that voice reuses the chat path's
machinery rather than growing a parallel one:

* ``/voice/connect`` proxies the WebRTC SDP exchange. The ephemeral key stays
  server-side (see support.providers.realtime for why).
* ``/voice/tool`` runs a function call from the realtime model through the
  *same* ToolExecutor, with the same guards, idempotency and audit rows. It
  returns both the string the model should see and the typed stream events the
  browser already knows how to render, so the UI shows a voice tool call the
  same way it shows a chat one.
* ``/voice/transcript`` persists what was said, so a voice exchange appears in
  the shared history and a later chat turn can refer back to it.

**Why the turn context is rebuilt from the database on every call.** A chat
turn is one request, so its TurnContext lives in memory for the duration. A
voice turn is many requests -- one per function call -- arriving over seconds,
possibly across a reconnect. Holding the context in process would make the
guard chain depend on server memory, so it is instead derived from what was
durably recorded: the agent state is replayed from this turn's successful tool
runs, and the evidence is rehydrated from `turns.evidence`. The audit trail is
the state, which also means a reconnecting browser resumes exactly where it
left off instead of silently restarting at INIT with a refund pending.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support.agent.prompts import SYSTEM_PROMPT
from support.agent.runner import tool_result_for_model
from support.agent.voice import VOICE_ADDENDUM, session_config
from support.api.deps import authorise_session, client_ip, get_db, settings_dep
from support.config import Settings
from support.contracts.enums import (
    AgentState,
    DenialReason,
    EscalationReason,
    FaultKind,
    GroundingDecision,
    MessageRole,
    Mode,
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
    Citation,
    EscalationCreated,
    EscalationCreatedPayload,
    Event,
    GroundingDecided,
    GroundingDecidedPayload,
    IdentityVerified,
    ToolCompleted,
    ToolCompletedPayload,
    ToolFailed,
    ToolFailedPayload,
    ToolStarted,
    ToolStartedPayload,
    VerifiedIdentityPayload,
)
from support.providers.realtime import RealtimeUnavailable
from support.retrieval.persistence import dump_evidence, load_evidence
from support.retrieval.search import RetrievalResult
from support.storage.models import Message, Refund, Session, ToolRun, Turn, TurnMetric
from support.tools import guards
from support.tools.errors import ToolError
from support.tools.executor import ToolExecutor, ToolOutcome, TurnContext
from support.tools.identity import verify_identity

logger = logging.getLogger("support.voice")

router = APIRouter(prefix="/api/sessions/{session_id}/voice")

# An SDP offer is a few kilobytes. This is generous and still bounds the body.
MAX_SDP_BYTES = 64 * 1024


# ------------------------------------------------------------------ schemas --


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1, max_length=MAX_SDP_BYTES)


class VoiceToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_turn_id: str = Field(min_length=1, max_length=64)
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    # Arguments arrive as the JSON string the realtime model emitted.
    arguments: str = Field(default="{}", max_length=8192)


class TranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_turn_id: str = Field(min_length=1, max_length=64)
    role: MessageRole
    text: str = Field(min_length=1, max_length=8000)
    interrupted: bool = False
    final: bool = True
    """Does this reply end the turn?

    The realtime model speaks between tool calls -- "let me check that for
    you" arrives in the same response as `verify_identity`. That filler is
    part of the transcript, but treating it as the end of the exchange closed
    the turn while the chain was still running, and the next tool call came
    back 409. The browser knows which responses carried a function call, so it
    says so here.

    It is not a trust boundary: holding a turn open buys nothing, because
    `max_tool_calls_per_turn` is enforced in the executor against the audit
    rows, not against the turn's status. Defaults to True so a caller that
    says nothing gets the old, turn-ending behaviour.
    """


# ------------------------------------------------------------------ helpers --


async def _get_or_create_turn(db: AsyncSession, session_id: UUID, client_turn_id: str) -> Turn:
    """One Turn row per spoken exchange, created by whichever request is first.

    A function call can land before the customer's transcript is final, so
    neither route may assume the other created the row.
    """
    existing = (
        await db.execute(
            select(Turn).where(Turn.session_id == session_id, Turn.client_turn_id == client_turn_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    turn = Turn(
        session_id=session_id,
        client_turn_id=client_turn_id,
        mode=Mode.VOICE,
        # Voice has no text to hash at this point. The chat duplicate-detection
        # that uses this does not apply to a spoken turn.
        input_hash=hashlib.sha256(client_turn_id.encode()).hexdigest()[:32],
    )
    db.add(turn)
    try:
        await db.commit()
    except IntegrityError:
        # Two function calls raced to create the same turn.
        await db.rollback()
        return (
            await db.execute(
                select(Turn).where(
                    Turn.session_id == session_id, Turn.client_turn_id == client_turn_id
                )
            )
        ).scalar_one()
    return turn


async def _rebuild_context(db: AsyncSession, session: Session, turn: Turn) -> TurnContext:
    """Derive this turn's context from what was durably recorded.

    State is replayed through the same transition table the live path uses, so
    there is no second definition of the ordering rules to keep in step.
    """
    ctx = TurnContext(
        session_id=session.id,
        client_turn_id=turn.client_turn_id,
        turn_id=turn.id,
    )

    runs = (
        (
            await db.execute(
                select(ToolRun).where(ToolRun.turn_id == turn.id).order_by(ToolRun.call_index)
            )
        )
        .scalars()
        .all()
    )

    # Every recorded attempt claimed an index, successful or not, so the next
    # call continues the sequence rather than colliding with an audited one.
    ctx.calls_made = len(runs)

    verified = False
    for run in runs:
        if run.status is not ToolStatus.COMPLETED:
            if run.error_code == "action_denied":
                reason = run.result_summary.get("denial_reason")
                if isinstance(reason, str):
                    try:
                        ctx.denials.append(DenialReason(reason))
                    except ValueError:
                        logger.warning("unknown denial reason in audit row: %r", reason)
            continue
        ctx.state = guards.next_state(ctx.state, run.tool)
        if run.tool is ToolName.VERIFY_IDENTITY:
            verified = True

    if verified:
        # Re-resolved through the same function the tool uses, so there is one
        # definition of what "verified" means and it reads the session binding
        # rather than anything the client sent.
        try:
            ctx.identity = await verify_identity(db, session_id=session.id)
        except ToolError as exc:
            # The binding went away (fixtures reset mid-call). Leaving identity
            # unset makes the next action fail closed.
            logger.warning("could not re-resolve identity for session=%s: %s", session.id, exc)

    ctx.evidence = await load_evidence(db, turn.evidence)
    ctx.injected_fault = (
        FaultKind(session.injected_fault) if session.injected_fault else FaultKind.NONE
    )
    return ctx


def _infer_voice_outcome(
    runs: list[ToolRun], evidence: RetrievalResult | None, *, has_receipt: bool
) -> TurnOutcome:
    """What this spoken turn actually did, read off the audit rows.

    Deliberately not the chat path's `_infer_outcome`: that one infers from
    what the model was *expected* to do next, which is available mid-stream.
    Here the turn is already over, so the honest answer is what was recorded.
    In particular an UNSUPPORTED decision with no ticket row is reported as a
    clarification, not an escalation -- labelling a turn "Escalated" when no
    ticket exists would be a claim the database does not support.
    """
    # A receipt row outranks the tool's status. A refund that committed and
    # then failed to report back -- the `action_timeout_after_commit` case --
    # leaves a FAILED run and real money moved, and the money is what the
    # customer experiences.
    if has_receipt:
        return TurnOutcome.ACTION_SUCCESS

    completed = [run for run in runs if run.status is ToolStatus.COMPLETED]
    if any(run.tool is ToolName.EXECUTE_MOCK_REFUND for run in completed):
        return TurnOutcome.ACTION_SUCCESS
    if any(run.tool is ToolName.CREATE_ESCALATION_TICKET for run in completed):
        return TurnOutcome.ESCALATION
    if any(run.error_code == "action_denied" for run in runs):
        return TurnOutcome.POLICY_DENIAL
    if evidence is not None and evidence.decision is GroundingDecision.GROUNDED:
        return TurnOutcome.GROUNDED_ANSWER
    return TurnOutcome.CLARIFICATION


async def _finalise_voice_turn(db: AsyncSession, turn: Turn, *, settings: Settings) -> None:
    """Write the outcome and the metric row for a spoken turn that just closed.

    Voice has no streaming runner accumulating these as it goes, so they are
    derived from the rows the tool calls already wrote -- which is also why the
    observability drawer can show a voice turn at all.

    Two numbers are deliberately absent rather than invented:

    * **tokens** -- the realtime session reports usage over the data channel to
      the browser, not to this server. A number we did not measure has no
      business in a cost panel.
    * **cost** -- with no token counts there is nothing to price. The per-minute
      realtime rate in config is unset by default, and pricing a turn at $0.00
      would read as "free" rather than "unknown".

    `total_ms` also means something different here and is labelled as such in
    the drawer: a spoken exchange's span includes the time the customer spent
    talking, which a typed turn's does not.
    """
    if (await db.get(TurnMetric, turn.id)) is not None:
        return

    runs = list(
        (
            await db.execute(
                select(ToolRun).where(ToolRun.turn_id == turn.id).order_by(ToolRun.call_index)
            )
        )
        .scalars()
        .all()
    )

    steps = [
        {
            "call_index": run.call_index,
            "tool": run.tool.value,
            "status": run.status.value,
            # Offsets on the turn's clock, so the drawer renders voice and chat
            # with the same waterfall component.
            "start_ms": max(int((run.started_at - turn.started_at).total_seconds() * 1000), 0),
            "ms": run.duration_ms,
        }
        for run in runs
    ]

    instructions = SYSTEM_PROMPT + VOICE_ADDENDUM
    evidence = await load_evidence(db, turn.evidence)
    receipts = (
        await db.execute(select(Refund.id).where(Refund.turn_id == turn.id).limit(1))
    ).scalar_one_or_none()
    turn.outcome = _infer_voice_outcome(runs, evidence, has_receipt=receipts is not None)

    # Read the clock the offsets are already on.
    #
    # `turn.started_at` and `tool_runs.started_at` are Postgres defaults, so
    # they are on the database's clock; `turn.finished_at` is set from this
    # process. Subtracting one from the other mixes two clocks, and the
    # waterfall caught it immediately: a few hundred milliseconds of skew put
    # the last tool call finishing *after* the turn it belongs to, which is
    # not a rendering problem but a wrong number. One clock for the span and
    # the offsets both.
    now = (await db.execute(select(func.now()))).scalar_one()
    total_ms = max(int((now - turn.started_at).total_seconds() * 1000), 0)

    db.add(
        TurnMetric(
            turn_id=turn.id,
            # Time to the first visible tool status, which is the latency a
            # caller actually perceives on this path.
            ttft_ms=steps[0]["start_ms"] if steps else None,
            total_ms=total_ms,
            tool_calls=len(runs),
            tokens_in=None,
            tokens_out=None,
            cost_estimate_usd=None,
            cost_rates_effective=settings.cost_rates_effective,
            model=settings.azure_openai_realtime_deployment,
            grounding_decision=evidence.decision if evidence is not None else None,
            top_similarity=evidence.top_similarity if evidence is not None else None,
            waterfall={
                "steps": steps,
                # A spoken turn has no per-call prompt: the instructions and the
                # tool list are pinned once, server-side, when the realtime
                # session is minted, and the browser cannot change either. That
                # is the property worth showing in the drawer, so it is recorded
                # as the prompt trace rather than leaving the panel empty.
                "prompt": [
                    {
                        "call": 0,
                        "tools_offered": sorted(tool.value for tool in ToolName),
                        "messages": [
                            {
                                "role": "system",
                                "chars": len(instructions),
                                "preview": " ".join(instructions.split())[:240] + "...",
                                "tool_calls": [],
                            }
                        ],
                        "response": None,
                        "note": (
                            "Pinned server-side at session mint; one instruction set for "
                            "the whole call, not per turn."
                        ),
                    }
                ],
                "citations": (sorted(evidence.citation_keys()) if evidence is not None else []),
                "grounding": (
                    {
                        "threshold": evidence.threshold,
                        "weak_margin": settings.grounding_weak_margin,
                    }
                    if evidence is not None
                    else None
                ),
            },
        )
    )


class _Sequencer:
    """Per-turn event sequence that survives across requests.

    Chat holds this in memory for one request; voice persists it on the turn
    row so the browser's reducer still sees one monotonic sequence per turn.
    """

    def __init__(self, turn: Turn) -> None:
        self._turn = turn
        self._next = turn.next_sequence

    def make(self, event_cls: Any, payload: Any) -> Any:
        event = event_cls(
            session_id=self._turn.session_id,
            turn_id=self._turn.id,
            sequence=self._next,
            timestamp=datetime.now(UTC),
            payload=payload,
        )
        self._next += 1
        return event

    def commit(self) -> None:
        self._turn.next_sequence = self._next


def _citations(ctx: TurnContext) -> list[Citation]:
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


def _events_for(
    ctx: TurnContext,
    tool: ToolName,
    result: ToolOutcome,
    seq: _Sequencer,
) -> list[Event]:
    """The same event vocabulary the chat path emits, for the same reducer."""
    events: list[Event] = [
        seq.make(
            ToolStarted,
            ToolStartedPayload(tool=tool, call_index=result.call_index, summary=_starting(tool)),
        )
    ]

    if result.status is ToolStatus.COMPLETED:
        events.append(
            seq.make(
                ToolCompleted,
                ToolCompletedPayload(
                    tool=tool,
                    call_index=result.call_index,
                    duration_ms=result.duration_ms,
                    summary=result.summary,
                ),
            )
        )
    else:
        events.append(
            seq.make(
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
        )
        denial = result.payload.get("denial_reason")
        if denial is not None:
            events.append(
                seq.make(
                    ActionDenied,
                    ActionDeniedPayload(
                        reason=DenialReason(str(denial)),
                        explanation=result.summary,
                        citations=_citations(ctx),
                    ),
                )
            )

    if tool is ToolName.VERIFY_IDENTITY and ctx.identity is not None:
        events.append(
            seq.make(
                IdentityVerified,
                VerifiedIdentityPayload(
                    customer_name=ctx.identity.customer_name,
                    tier=ctx.identity.tier,
                    contract_status=ctx.identity.contract_status,
                ),
            )
        )

    if tool is ToolName.RETRIEVE_POLICY and ctx.evidence is not None:
        events.append(
            seq.make(
                GroundingDecided,
                GroundingDecidedPayload(
                    decision=ctx.evidence.decision,
                    threshold=ctx.evidence.threshold,
                    top_similarity=ctx.evidence.top_similarity,
                    accepted_chunks=ctx.evidence.evidence_count,
                    citations=_citations(ctx),
                ),
            )
        )

    if tool is ToolName.EXECUTE_MOCK_REFUND and result.payload.get("receipt_ref"):
        p = result.payload
        events.append(
            seq.make(
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
        )

    if tool is ToolName.CREATE_ESCALATION_TICKET and result.payload.get("ticket_ref"):
        p = result.payload
        events.append(
            seq.make(
                EscalationCreated,
                EscalationCreatedPayload(
                    ticket_ref=str(p["ticket_ref"]),
                    intent_id=str(p["intent_id"]),
                    reason=EscalationReason(p["reason"]),
                    summary=str(p["summary"]),
                ),
            )
        )

    return events


def _starting(tool: ToolName) -> str:
    return {
        ToolName.VERIFY_IDENTITY: "Verifying account...",
        ToolName.RETRIEVE_POLICY: "Searching policy library...",
        ToolName.EXECUTE_MOCK_REFUND: "Checking refund eligibility...",
        ToolName.CREATE_ESCALATION_TICKET: "Creating escalation ticket...",
    }[tool]


# ------------------------------------------------------------------- routes --


@router.post("/connect")
async def connect(
    session_id: UUID,
    body: ConnectRequest,
    request: Request,
    session: Annotated[Session, Depends(authorise_session)],
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Exchange the browser's SDP offer for Azure's answer.

    The ephemeral key is minted and spent here. The browser receives only an
    SDP answer, which is not a credential and cannot be replayed to start
    another session against our resource.
    """
    limiter = request.app.state.limiter
    allowed, reason, retry_after = limiter.check(
        session_id=str(session_id), client_ip=client_ip(request)
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error_code": reason, "retry_after_s": round(retry_after, 1)},
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    if "m=audio" not in body.sdp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "invalid_offer", "message": "The offer has no audio track."},
        )

    realtime = request.app.state.realtime

    try:
        secret = await realtime.mint_client_secret(session_config())
        answer = await realtime.exchange_sdp(body.sdp, ephemeral_key=secret["value"])
    except RealtimeUnavailable as exc:
        logger.warning("voice connect failed for session=%s: %s", session_id, exc.safe_message)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "voice_unavailable", "message": exc.safe_message},
        ) from exc

    return JSONResponse(
        {
            "sdp": answer,
            "model": realtime.deployment,
            # The browser hangs up after this regardless, so it can show a
            # countdown rather than dropping the call without explanation.
            "expires_at": secret.get("expires_at"),
            "max_session_s": settings.max_voice_session_s,
        }
    )


@router.post("/tool")
async def run_tool(
    session_id: UUID,
    body: VoiceToolRequest,
    request: Request,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Execute one function call from the realtime model.

    Everything the chat path enforces is enforced here, because it is the same
    executor: ordering guards, schema validation, the grounding gate, refund
    idempotency and the audit row. The browser is the transport for this call,
    never the authority on whether it is allowed.
    """
    try:
        tool = ToolName(body.name)
    except ValueError:
        # The model invented a tool. Tell it plainly rather than erroring the
        # request, so it can recover within the same conversation.
        return JSONResponse(
            {
                "call_id": body.call_id,
                "output": f"Unknown tool {body.name!r}.",
                "events": [],
            }
        )

    try:
        raw_args = json.loads(body.arguments or "{}")
    except json.JSONDecodeError:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}

    turn = await _get_or_create_turn(db, session_id, body.client_turn_id)
    if turn.status is not TurnStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "turn_already_finished"},
        )

    ctx = await _rebuild_context(db, session, turn)

    executor = ToolExecutor(db, embeddings=request.app.state.embeddings, settings=settings)
    result = await executor.invoke(ctx, tool, raw_args)

    # Persist the evidence so the next function call in this turn -- a separate
    # request -- can still pass the grounding gate.
    if tool is ToolName.RETRIEVE_POLICY and ctx.evidence is not None:
        turn.evidence = dump_evidence(ctx.evidence)

    seq = _Sequencer(turn)
    events = _events_for(ctx, tool, result, seq)
    seq.commit()
    await db.commit()

    return JSONResponse(
        {
            "call_id": body.call_id,
            "output": tool_result_for_model(ctx, tool, result),
            "events": [event.model_dump(mode="json") for event in events],
            "agent_state": ctx.state.value,
        }
    )


@router.post("/transcript")
async def save_transcript(
    session_id: UUID,
    body: TranscriptRequest,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Persist one side of a spoken exchange into the shared history.

    `interrupted` is carried through because an interrupted reply is shown to
    the customer but withheld from model context: continuing from text that
    was cut off mid-sentence makes the next turn incoherent.
    """
    turn = await _get_or_create_turn(db, session_id, body.client_turn_id)

    db.add(
        Message(
            session_id=session_id,
            turn_id=turn.id,
            role=body.role,
            mode=Mode.VOICE,
            content=body.text,
            interrupted=body.interrupted,
        )
    )

    if body.role is MessageRole.ASSISTANT and body.final:
        # The agent's *last* reply closes the exchange. One that came alongside
        # a function call does not -- see TranscriptRequest.final.
        turn.status = TurnStatus.FINISHED
        turn.finished_at = datetime.now(UTC)
        await _finalise_voice_turn(db, turn, settings=settings)

    await db.commit()
    return JSONResponse({"turn_id": str(turn.id), "state": turn.status.value})


@router.get("/state")
async def voice_state(
    session_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """What the server believes about an in-flight voice turn.

    Used after a reconnect: the browser can tell whether the tool chain it
    started is still part-way through rather than guessing.
    """
    running = (
        (
            await db.execute(
                select(Turn)
                .where(
                    Turn.session_id == session_id,
                    Turn.mode == Mode.VOICE,
                    Turn.status == TurnStatus.RUNNING,
                )
                .order_by(Turn.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if running is None:
        return JSONResponse({"active_turn": None, "agent_state": AgentState.INIT.value})

    ctx = await _rebuild_context(db, session, running)
    return JSONResponse(
        {
            "active_turn": {
                "turn_id": str(running.id),
                "client_turn_id": running.client_turn_id,
                "calls_made": ctx.calls_made,
            },
            "agent_state": ctx.state.value,
            "grounded": ctx.is_grounded,
        }
    )
