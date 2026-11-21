"""HTTP routes."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support.agent.prompts import SYSTEM_PROMPT
from support.agent.runner import TurnRunner
from support.api.deps import (
    BROWSER_COOKIE,
    BROWSER_COOKIE_MAX_AGE_S,
    SESSION_COOKIE,
    authorise_session,
    client_ip,
    get_db,
    new_browser_id,
    new_token,
    settings_dep,
)
from support.api.sse import SSE_HEADERS, with_heartbeat
from support.config import Settings
from support.contracts.enums import (
    AgentState,
    FaultKind,
    GroundingDecision,
    Mode,
    TurnOutcome,
    TurnStatus,
)
from support.fixtures.seed import seed
from support.storage.models import (
    DemoAccount,
    EscalationTicket,
    Invoice,
    Message,
    Refund,
    Session,
    ToolRun,
    Turn,
    TurnMetric,
)

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ schemas --


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The sign-in control picks an account. This is the ONLY way an account is
    # bound to a session: a chat message claiming a tier does nothing.
    account_slug: str | None = Field(default=None, max_length=64)


class CreateSessionResponse(BaseModel):
    session_id: UUID
    session_token: str
    account: dict[str, Any] | None
    expires_at: datetime


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_turn_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    mode: Mode = Mode.CHAT


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault: FaultKind


# ------------------------------------------------------------------ demo --


@router.get("/demo/accounts")
async def list_demo_accounts(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Seeded accounts for the sign-in control. Clearly labelled as synthetic."""
    rows = (await db.execute(select(DemoAccount).order_by(DemoAccount.slug))).scalars().all()
    out = []
    for account in rows:
        invoices = (
            (await db.execute(select(Invoice).where(Invoice.account_id == account.id)))
            .scalars()
            .all()
        )
        out.append(
            {
                "slug": account.slug,
                "label": account.label,
                "customer_name": account.customer_name,
                "tier": account.tier.value,
                "contract_status": account.contract_status.value,
                "invoices": [
                    {
                        "ref": inv.external_ref,
                        "description": inv.description,
                        "amount_minor": inv.amount_minor,
                        "refunded_minor": inv.refunded_minor,
                        "currency": inv.currency,
                    }
                    for inv in sorted(invoices, key=lambda i: i.external_ref)
                ],
            }
        )
    # `demo_mode` is reported so the browser can hide the fault-injection and
    # reset controls rather than offering buttons whose endpoints 404.
    return JSONResponse({"synthetic": True, "demo_mode": settings.demo_mode, "accounts": out})


# -------------------------------------------------------------- sessions --


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    browser_id: str | None = Cookie(default=None, alias=BROWSER_COOKIE),
) -> CreateSessionResponse:
    account = None
    if body.account_slug:
        account = (
            await db.execute(select(DemoAccount).where(DemoAccount.slug == body.account_slug))
        ).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Unknown demo account.")

    # One browser, many sessions -- typically one per demo account. The session
    # cookie names only the newest, so resumption needs this instead.
    browser = browser_id or new_browser_id()

    token, token_hash = new_token()
    row = Session(
        token_hash=token_hash,
        browser_id=browser,
        demo_account_id=account.id if account else None,
        # Binding the account is the verification event. The tool still reads
        # it from here rather than trusting anything the model says.
        verified_at=datetime.now(UTC) if account else None,
        agent_state=AgentState.INIT,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.session_ttl_s),
    )
    db.add(row)
    await db.commit()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.session_ttl_s,
        path="/",
    )

    # Re-set each time, so an active visitor's window keeps rolling forward.
    response.set_cookie(
        BROWSER_COOKIE,
        browser,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=BROWSER_COOKIE_MAX_AGE_S,
        path="/",
    )

    return CreateSessionResponse(
        session_id=row.id,
        session_token=token,
        account=(
            {
                "slug": account.slug,
                "label": account.label,
                "tier": account.tier.value,
                "contract_status": account.contract_status.value,
            }
            if account
            else None
        ),
        expires_at=row.expires_at,
    )


@router.get("/sessions/current")
async def get_current_sessions(
    db: AsyncSession = Depends(get_db),
    browser_id: str | None = Cookie(default=None, alias=BROWSER_COOKIE),
) -> JSONResponse:
    """Live sessions this browser created, newest first, one per demo account.

    Declared before `/sessions/{session_id}` so "current" is not parsed as a
    UUID path parameter.

    Closing a browser does not end a session: the row, its transcript and its
    receipts live for `session_ttl_s`. What does not survive is sessionStorage,
    which held the session *id* -- so the sessions sit in the database,
    reachable, and the browser cannot name them. This is the way back.

    **Keyed on the browser, never on the account.** The demo account is a
    public dropdown choice and proves nothing: resuming "the latest Jordan
    Patel session" would hand one visitor another visitor's transcript and
    receipts. The browser id is an unguessable httpOnly cookie, so what is
    being proved is "this browser created that session". The account is only
    the label shown against it.

    **Returns no token.** The session cookie stays httpOnly; the client resumes
    by calling the normal endpoints with `credentials: "include"`, which
    `authorise_session` already accepts. Handing tokens back to JavaScript
    would mean an XSS after a restart could steal credentials that are
    currently out of reach of script.
    """
    if not browser_id:
        return JSONResponse({"sessions": []})

    rows = (
        (
            await db.execute(
                select(Session)
                .where(
                    Session.browser_id == browser_id,
                    Session.expires_at > datetime.now(UTC),
                )
                .order_by(Session.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    out: list[dict[str, Any]] = []
    seen_accounts: set[UUID | None] = set()

    for row in rows:
        # Newest first, so the first session seen for an account is the one
        # worth offering. Older ones for the same account would be a list of
        # near-identical cards with no way to tell them apart.
        if row.demo_account_id in seen_accounts:
            continue

        messages = (
            await db.execute(select(func.count(Message.id)).where(Message.session_id == row.id))
        ).scalar_one()

        # An empty session is a choice between a blank transcript and a blank
        # transcript, so it is not offered -- but it still claims its account,
        # since a newer empty session means the older one was left behind.
        seen_accounts.add(row.demo_account_id)
        if messages == 0:
            continue

        account = None
        if row.demo_account_id:
            acct = (
                await db.execute(select(DemoAccount).where(DemoAccount.id == row.demo_account_id))
            ).scalar_one_or_none()
            if acct is not None:
                account = {
                    "slug": acct.slug,
                    "label": acct.label,
                    "customer_name": acct.customer_name,
                    "tier": acct.tier.value,
                    "contract_status": acct.contract_status.value,
                }

        last = (
            await db.execute(
                select(func.max(Message.created_at)).where(Message.session_id == row.id)
            )
        ).scalar_one()

        out.append(
            {
                "session_id": str(row.id),
                "account": account,
                "messages": messages,
                "last_activity": last.isoformat() if last else None,
                "expires_at": row.expires_at.isoformat(),
            }
        )

    return JSONResponse({"sessions": out})


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
    browser_id: str | None = Cookie(default=None, alias=BROWSER_COOKIE),
) -> JSONResponse:
    """Point this browser's session cookie at one of its earlier sessions.

    Listing resumable sessions is browser-scoped, but authorisation is not:
    the session cookie names exactly one session, so asking for any other --
    the Jordan Patel session while the cookie names the newer Avery Chen one
    -- was refused with "Session not found". Correct refusal, wrong question.

    Resuming therefore switches the cookie rather than widening what the
    cookie means. Authorisation stays one credential naming one session, and
    `authorise_session` is untouched.

    The cookie has to be re-issued because only token *hashes* are stored:
    there is no way to hand back a token the server cannot read. So the
    session's token is rotated here, which also invalidates the old one -- a
    deliberate consequence, since a session can be active in one place at a
    time from this UI's point of view.

    Ownership is the browser cookie, never the account: the account is a
    selectable identity and proves nothing.
    """
    if not browser_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No browser credential. Start a session first.",
        )

    row = (
        await db.execute(
            select(Session).where(
                Session.id == session_id,
                Session.browser_id == browser_id,
            )
        )
    ).scalar_one_or_none()

    # 404 rather than 403: a browser that does not own this session is not
    # told that it exists.
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Start a new one.",
        )

    token, token_hash = new_token()
    row.token_hash = token_hash
    await db.commit()

    account = None
    if row.demo_account_id:
        acct = (
            await db.execute(select(DemoAccount).where(DemoAccount.id == row.demo_account_id))
        ).scalar_one_or_none()
        if acct is not None:
            account = {
                "slug": acct.slug,
                "label": acct.label,
                "tier": acct.tier.value,
                "contract_status": acct.contract_status.value,
            }

    body = JSONResponse(
        {
            "session_id": str(row.id),
            "account": account,
            "expires_at": row.expires_at.isoformat(),
        }
    )
    # Set on the response actually being returned. FastAPI merges the injected
    # Response's headers only when the handler returns a model; return a
    # Response directly and a cookie set on the injected one is silently
    # dropped -- which looked exactly like the resume failing to take effect.
    body.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.session_ttl_s,
        path="/",
    )
    return body


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Full session snapshot.

    Used on load and after a reconnect or refresh: committed messages and
    durable receipts come back without replaying the turn.
    """
    messages = (
        (
            await db.execute(
                select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    active = (
        (
            await db.execute(
                select(Turn).where(Turn.session_id == session_id, Turn.status == TurnStatus.RUNNING)
            )
        )
        .scalars()
        .first()
    )
    receipts = (
        (await db.execute(select(Refund).where(Refund.session_id == session_id))).scalars().all()
    )
    tickets = (
        (
            await db.execute(
                select(EscalationTicket).where(EscalationTicket.session_id == session_id)
            )
        )
        .scalars()
        .all()
    )

    account = None
    if session.demo_account_id:
        acct = (
            await db.execute(select(DemoAccount).where(DemoAccount.id == session.demo_account_id))
        ).scalar_one()
        account = {
            "slug": acct.slug,
            "label": acct.label,
            "tier": acct.tier.value,
            "contract_status": acct.contract_status.value,
        }

    return JSONResponse(
        {
            "session_id": str(session_id),
            "account": account,
            "active_turn_id": str(active.id) if active else None,
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role.value,
                    "mode": m.mode.value,
                    "content": m.content,
                    "interrupted": m.interrupted,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
            "receipts": [
                {
                    "receipt_ref": r.receipt_ref,
                    "amount_minor": r.amount_minor,
                    "currency": r.currency,
                    "policy_version": r.policy_version,
                    "created_at": r.created_at.isoformat(),
                }
                for r in receipts
            ],
            "tickets": [
                {
                    "ticket_ref": t.ticket_ref,
                    "reason": t.reason.value,
                    "summary": t.summary,
                    "created_at": t.created_at.isoformat(),
                }
                for t in tickets
            ],
        }
    )


# ----------------------------------------------------------------- turns --


@router.post("/sessions/{session_id}/turns")
async def create_turn(
    session_id: UUID,
    body: TurnRequest,
    request: Request,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> StreamingResponse:
    """Run a chat turn and stream typed events over SSE."""
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

    input_hash = hashlib.sha256(body.text.strip().encode()).hexdigest()[:32]

    # One active turn per session, enforced server-side.
    running = (
        (
            await db.execute(
                select(Turn).where(Turn.session_id == session_id, Turn.status == TurnStatus.RUNNING)
            )
        )
        .scalars()
        .first()
    )
    if running is not None and running.client_turn_id != body.client_turn_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error_code": "turn_in_progress", "active_turn_id": str(running.id)},
        )

    turn = Turn(
        session_id=session_id,
        client_turn_id=body.client_turn_id,
        mode=body.mode,
        input_hash=input_hash,
    )
    db.add(turn)
    try:
        await db.commit()
    except IntegrityError:
        # Duplicate client_turn_id: a retry of a request we already have.
        await db.rollback()
        existing = (
            await db.execute(
                select(Turn).where(
                    Turn.session_id == session_id,
                    Turn.client_turn_id == body.client_turn_id,
                )
            )
        ).scalar_one()

        if existing.input_hash != input_hash:
            # Same id, different text. A client bug, not a retry -- joining it
            # would silently answer the wrong question.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error_code": "client_turn_id_reused_with_different_input"},
            ) from None

        # A genuine replay. Return the terminal state rather than re-running,
        # so a duplicate submit cannot issue a second refund.
        return StreamingResponse(
            _replay_terminal(db, existing),
            headers=SSE_HEADERS,
        )

    history = (
        (
            await db.execute(
                select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    runner = TurnRunner(
        db,
        chat=request.app.state.chat,
        embeddings=request.app.state.embeddings,
        settings=settings,
    )

    async def stream() -> Any:
        limiter.acquire()
        try:
            events = runner.run(
                turn=turn,
                session_id=session_id,
                user_text=body.text,
                history=list(history),
            )
            async for frame in with_heartbeat(events):
                yield frame
            await db.commit()
        finally:
            limiter.release()

    return StreamingResponse(stream(), headers=SSE_HEADERS)


async def _replay_terminal(db: AsyncSession, turn: Turn) -> Any:
    """Re-emit a finished turn's outcome without re-running it."""
    from support.api.sse import format_comment, format_event
    from support.contracts.events import TurnFinished, TurnFinishedPayload

    if turn.status is TurnStatus.RUNNING:
        yield format_comment("turn still running; reconnect for the snapshot")
        return

    message = (
        (
            await db.execute(
                select(Message)
                .where(Message.turn_id == turn.id, Message.role == "assistant")
                .order_by(Message.created_at.desc())
            )
        )
        .scalars()
        .first()
    )

    yield format_event(
        TurnFinished(
            session_id=turn.session_id,
            turn_id=turn.id,
            sequence=0,
            timestamp=datetime.now(UTC),
            payload=TurnFinishedPayload(
                outcome=turn.outcome,
                message=message.content if message else None,
            ),
        )
    )


@router.post("/sessions/{session_id}/turns/{turn_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_turn(
    session_id: UUID,
    turn_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Idempotent cancel.

    Writes the durable flag before anything else observes it, so a committed
    action racing a cancel is still reported from its receipt.
    """
    turn = (
        await db.execute(select(Turn).where(Turn.id == turn_id, Turn.session_id == session_id))
    ).scalar_one_or_none()
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn not found.")

    if turn.status is TurnStatus.RUNNING:
        turn.cancel_requested = True
        turn.status = TurnStatus.CANCELLED
        turn.finished_at = datetime.now(UTC)
        await db.commit()

    receipts = (await db.execute(select(Refund).where(Refund.turn_id == turn_id))).scalars().all()
    return JSONResponse(
        {
            "turn_id": str(turn_id),
            "status": turn.status.value,
            # A refund that committed before the cancel landed is still real.
            "committed_receipts": [r.receipt_ref for r in receipts],
        }
    )


@router.get("/sessions/{session_id}/turns/{turn_id}/trace")
async def get_trace(
    session_id: UUID,
    turn_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Redacted per-turn projection for the observability drawer."""
    turn = (
        await db.execute(select(Turn).where(Turn.id == turn_id, Turn.session_id == session_id))
    ).scalar_one_or_none()
    if turn is None:
        raise HTTPException(status_code=404, detail="Turn not found.")

    runs = (
        (
            await db.execute(
                select(ToolRun).where(ToolRun.turn_id == turn_id).order_by(ToolRun.call_index)
            )
        )
        .scalars()
        .all()
    )
    metric = (
        await db.execute(select(TurnMetric).where(TurnMetric.turn_id == turn_id))
    ).scalar_one_or_none()

    # The threshold a decision was taken under is recorded with the turn. Only
    # a row written before that was stored falls back to the current config,
    # and the drawer marks a fallback so a retuned threshold is never shown as
    # though the old decision had been measured against it.
    detail = metric.waterfall if metric and isinstance(metric.waterfall, dict) else {}
    recorded = detail.get("grounding") if isinstance(detail.get("grounding"), dict) else None
    prompt_steps = detail.get("prompt") if isinstance(detail.get("prompt"), list) else []

    return JSONResponse(
        {
            "turn_id": str(turn_id),
            "mode": turn.mode.value,
            "status": turn.status.value,
            "outcome": turn.outcome.value if turn.outcome else None,
            # Static, identical for every turn, and carried here so the drawer
            # can show the instructions the guardrails are written into without
            # a second round trip. It contains no customer data.
            "system_prompt": SYSTEM_PROMPT,
            "tool_runs": [
                {
                    "tool": r.tool.value,
                    "call_index": r.call_index,
                    "status": r.status.value,
                    "duration_ms": r.duration_ms,
                    "args": r.args_redacted,
                    "result": r.result_summary,
                    "error_code": r.error_code,
                }
                for r in runs
            ],
            "metrics": (
                {
                    "ttft_ms": metric.ttft_ms,
                    "total_ms": metric.total_ms,
                    "tool_calls": metric.tool_calls,
                    "tokens_in": metric.tokens_in,
                    "tokens_out": metric.tokens_out,
                    "cost_estimate_usd": metric.cost_estimate_usd,
                    "cost_rates_effective": metric.cost_rates_effective,
                    "cost_is_estimate": True,
                    "model": metric.model,
                    "grounding_decision": (
                        metric.grounding_decision.value if metric.grounding_decision else None
                    ),
                    "top_similarity": metric.top_similarity,
                    "grounding_threshold": (
                        recorded["threshold"] if recorded else settings.grounding_threshold
                    ),
                    "grounding_weak_margin": (
                        recorded["weak_margin"] if recorded else settings.grounding_weak_margin
                    ),
                    "grounding_threshold_recorded": recorded is not None,
                    "waterfall": metric.waterfall,
                    # What the model was actually given, per call. Message
                    # bodies are previews: the trace is a window onto the turn,
                    # not a second copy of the transcript.
                    "prompt": prompt_steps,
                }
                if metric
                else None
            ),
        }
    )


def _percentile(values: list[int], q: float) -> int | None:
    """Nearest-rank percentile: the smallest sample at or above the quantile.

    No interpolation. Over the handful of turns a demo session produces,
    interpolating between two samples would dress a guess up as a measurement,
    and every percentile this returns is reported next to its sample count so
    a "p95" over four turns is visibly the maximum under another name.
    """
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _latency(values: list[int]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


@router.get("/sessions/{session_id}/metrics")
async def session_metrics(
    session_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Rollup across this session's finished turns.

    Latency is split by mode rather than pooled. A typed turn's `total_ms` is
    the agent's own work; a spoken turn's span includes the time the customer
    spent talking. One median over both would be a number describing nothing.

    Token and cost totals sum only the turns that reported them, and
    `turns_priced` says how many that was -- voice turns contribute no usage
    (the realtime session reports it to the browser, not here), so a total
    presented as the session's whole spend would understate it.
    """
    rows = (
        await db.execute(
            select(TurnMetric, Turn)
            .join(Turn, Turn.id == TurnMetric.turn_id)
            .where(Turn.session_id == session_id)
            .order_by(Turn.started_at)
        )
    ).all()

    by_mode: dict[str, list[int]] = {Mode.CHAT.value: [], Mode.VOICE.value: []}
    ttft_by_mode: dict[str, list[int]] = {Mode.CHAT.value: [], Mode.VOICE.value: []}
    grounding = {decision.value: 0 for decision in GroundingDecision}
    outcomes = {outcome.value: 0 for outcome in TurnOutcome}
    modes = {mode.value: 0 for mode in Mode}

    tokens_in = tokens_out = tool_calls = 0
    cost_total = 0.0
    turns_priced = 0

    for metric, turn in rows:
        modes[turn.mode.value] += 1
        tool_calls += metric.tool_calls
        by_mode[turn.mode.value].append(metric.total_ms)
        if metric.ttft_ms is not None:
            ttft_by_mode[turn.mode.value].append(metric.ttft_ms)
        if metric.tokens_in is not None:
            tokens_in += metric.tokens_in
        if metric.tokens_out is not None:
            tokens_out += metric.tokens_out
        if metric.cost_estimate_usd is not None:
            cost_total += metric.cost_estimate_usd
            turns_priced += 1
        if metric.grounding_decision is not None:
            grounding[metric.grounding_decision.value] += 1
        if turn.outcome is not None:
            outcomes[turn.outcome.value] += 1

    return JSONResponse(
        {
            "session_id": str(session_id),
            "turns": len(rows),
            "turns_by_mode": modes,
            "tool_calls": tool_calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_estimate_usd": round(cost_total, 6) if turns_priced else None,
            "cost_rates_effective": settings.cost_rates_effective,
            "cost_is_estimate": True,
            "turns_priced": turns_priced,
            "latency": {
                mode: {"total": _latency(by_mode[mode]), "ttft": _latency(ttft_by_mode[mode])}
                for mode in by_mode
            },
            "grounding": grounding,
            "outcomes": {key: value for key, value in outcomes.items() if value},
        }
    )


# ------------------------------------------------------------ demo controls --


@router.post("/sessions/{session_id}/reset")
async def reset_demo(
    session_id: UUID,
    session: Annotated[Session, Depends(authorise_session)],
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not available.")
    report = await seed(reset=True)
    return JSONResponse({"reset": report["reset"], "accounts": len(report["accounts"])})


@router.post("/sessions/{session_id}/fault")
async def inject_fault(
    session_id: UUID,
    body: FaultRequest,
    session: Annotated[Session, Depends(authorise_session)],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> JSONResponse:
    """Arm a fault for the caller's OWN session only.

    Scoped deliberately: a public demo where one visitor could inject failures
    into another's session would be worse than having no fault injection.
    """
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not available.")

    session.injected_fault = None if body.fault is FaultKind.NONE else body.fault.value
    await db.commit()
    return JSONResponse({"session_id": str(session_id), "fault": body.fault.value})
