"""Identity verification and escalation tickets."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support.contracts.enums import ContractStatus, DenialReason, EscalationReason, Tier
from support.storage.models import DemoAccount, EscalationTicket, Session
from support.tools.errors import ActionDenied, ToolError


@dataclass(frozen=True)
class VerifiedIdentity:
    customer_id: UUID
    customer_name: str
    tier: Tier
    contract_status: ContractStatus
    account_label: str


async def verify_identity(db: AsyncSession, *, session_id: UUID) -> VerifiedIdentity:
    """Resolve the account bound to this session.

    Takes no model-supplied arguments. The binding is created server-side by
    the UI sign-in control, so the demo verification secret never reaches model
    context or a trace, and a message claiming an account proves nothing.
    """
    session = (await db.execute(select(Session).where(Session.id == session_id))).scalar_one()

    if session.demo_account_id is None:
        raise ActionDenied(
            DenialReason.NOT_VERIFIED,
            "I'll need you to pick a demo account first so I can verify it. "
            "Use the account selector above the conversation.",
        )

    account = (
        await db.execute(select(DemoAccount).where(DemoAccount.id == session.demo_account_id))
    ).scalar_one()

    return VerifiedIdentity(
        customer_id=account.id,
        customer_name=account.customer_name,
        tier=account.tier,
        contract_status=account.contract_status,
        account_label=account.label,
    )


@dataclass(frozen=True)
class Ticket:
    ticket_ref: str
    intent_id: str
    reason: EscalationReason
    summary: str
    idempotent_replay: bool


def build_ticket_intent(*, session_id: UUID, client_turn_id: str) -> str:
    return f"esc:{session_id}:{client_turn_id}"


async def create_escalation_ticket(
    db: AsyncSession,
    *,
    session_id: UUID,
    client_turn_id: str,
    turn_id: UUID | None,
    reason: EscalationReason,
    summary: str,
    citations: list[str],
) -> Ticket:
    """Create a mock escalation ticket.

    Idempotent on (session, turn), so a retried turn does not open a second
    ticket. A reference is only ever returned from a persisted row -- there is
    no code path that invents a ticket ID.
    """
    intent_id = build_ticket_intent(session_id=session_id, client_turn_id=client_turn_id)

    existing = (
        await db.execute(select(EscalationTicket).where(EscalationTicket.intent_id == intent_id))
    ).scalar_one_or_none()
    if existing is not None:
        return Ticket(
            ticket_ref=existing.ticket_ref,
            intent_id=intent_id,
            reason=existing.reason,
            summary=existing.summary,
            idempotent_replay=True,
        )

    ticket = EscalationTicket(
        intent_id=intent_id,
        ticket_ref=f"ESC-{secrets.token_hex(3).upper()}",
        session_id=session_id,
        turn_id=turn_id,
        reason=reason,
        summary=summary,
        citations={"chunk_ids": citations},
    )
    db.add(ticket)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(EscalationTicket).where(EscalationTicket.intent_id == intent_id)
            )
        ).scalar_one_or_none()
        if winner is None:
            raise
        return Ticket(
            ticket_ref=winner.ticket_ref,
            intent_id=intent_id,
            reason=winner.reason,
            summary=winner.summary,
            idempotent_replay=True,
        )

    return Ticket(
        ticket_ref=ticket.ticket_ref,
        intent_id=intent_id,
        reason=reason,
        summary=summary,
        idempotent_replay=False,
    )


class TicketCreationFailed(ToolError):
    """Ticket creation failed.

    Surfaced so the UI can say escalation is needed and offer a retry, rather
    than fabricating a reference.
    """

    error_code = "ticket_creation_failed"
    retryable = True
