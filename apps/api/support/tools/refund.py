"""The mock refund action.

This is the only tool that writes money-shaped state, so it carries the
strictest machinery:

**Idempotency.** Every user-requested action gets a stable `intent_id` derived
from (session, invoice, amount, client turn). A retry after a lost response
looks the intent up and returns the original receipt rather than committing
twice. The unique constraint on `refunds.intent_id` is the backstop for the
genuine race, where two concurrent commits pass the lookup simultaneously:
one wins, the loser catches the integrity error and re-reads the winner's
receipt. Both callers see the same receipt; the invoice moves once.

**Lock ordering.** Session row first, then invoice row, always. Two orderings
would deadlock under concurrent refunds on the same invoice.

**Validation inside the transaction.** The refundable balance is checked after
the lock is held, not before, so a concurrent refund cannot invalidate the
check between reading and writing.

Nothing reports success without a persisted receipt.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support.contracts.enums import ContractStatus, DenialReason
from support.storage.models import DemoAccount, Invoice, Refund, Session
from support.tools.errors import ActionDenied
from support.tools.rules import RefundRule, active_refund_rule


@dataclass(frozen=True)
class RefundReceipt:
    receipt_ref: str
    intent_id: str
    invoice_ref: str
    amount_minor: int
    currency: str
    policy_doc_key: str
    policy_version: str
    remaining_refundable_minor: int
    # True when this call found an existing receipt instead of committing.
    idempotent_replay: bool


def build_intent_id(
    *,
    session_id: UUID,
    invoice_ref: str,
    amount_minor: int,
    client_turn_id: str,
) -> str:
    """Stable identity for one user-requested action.

    Deliberately includes `client_turn_id`: a retry of the *same* request
    reuses it and is deduplicated, while a new, explicitly-requested partial
    refund of the same amount arrives on a new turn and so gets a new intent.
    """
    raw = f"{session_id}|{invoice_ref.upper()}|{amount_minor}|{client_turn_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _receipt_ref() -> str:
    return f"RF-{secrets.token_hex(4).upper()}"


async def _find_existing(db: AsyncSession, intent_id: str) -> Refund | None:
    return (
        await db.execute(select(Refund).where(Refund.intent_id == intent_id))
    ).scalar_one_or_none()


async def _receipt_from_row(db: AsyncSession, row: Refund, *, replay: bool) -> RefundReceipt:
    invoice = (await db.execute(select(Invoice).where(Invoice.id == row.invoice_id))).scalar_one()
    return RefundReceipt(
        receipt_ref=row.receipt_ref,
        intent_id=row.intent_id,
        invoice_ref=invoice.external_ref,
        amount_minor=row.amount_minor,
        currency=row.currency,
        policy_doc_key=row.policy_document_key,
        policy_version=row.policy_version,
        remaining_refundable_minor=invoice.amount_minor - invoice.refunded_minor,
        idempotent_replay=replay,
    )


def _validate(
    *,
    rule: RefundRule,
    account: DemoAccount,
    invoice: Invoice,
    amount_minor: int,
    reason: str,
) -> None:
    """Policy validation. Raises ActionDenied with a citable reason.

    Order matters and mirrors the corpus: contract status is checked before the
    amount, because the contract-eligibility policy applies before any
    plan-specific cap. A Premium customer with an inactive contract asking for
    $15 is denied on contract status, not on the amount.
    """
    if rule.requires_active_contract and account.contract_status is not ContractStatus.ACTIVE:
        raise ActionDenied(
            DenialReason.CONTRACT_INACTIVE,
            "That account's contract is not active, so I can't make any financial "
            "adjustment. A billing specialist can review reinstatement with you.",
        )

    if invoice.account_id != account.id:
        # Deliberately does not reveal whose invoice it is.
        raise ActionDenied(
            DenialReason.INVOICE_NOT_OWNED,
            "That invoice isn't on the verified account, so I can't refund it.",
        )

    if invoice.currency != rule.currency:
        raise ActionDenied(
            DenialReason.CURRENCY_MISMATCH,
            f"That invoice is in {invoice.currency}, and cross-currency refunds need a specialist.",
        )

    if amount_minor > rule.per_request_cap_minor:
        raise ActionDenied(
            DenialReason.EXCEEDS_TIER_CAP,
            f"${amount_minor / 100:.2f} is above the {rule.cap_display} per-request "
            f"limit on this plan, so it needs a billing specialist.",
        )

    remaining = invoice.amount_minor - invoice.refunded_minor
    if amount_minor > remaining:
        raise ActionDenied(
            DenialReason.INSUFFICIENT_REFUNDABLE_BALANCE,
            f"That invoice has ${remaining / 100:.2f} left that can be refunded, "
            f"which is less than the ${amount_minor / 100:.2f} requested.",
        )

    if rule.eligible_reasons and reason not in rule.eligible_reasons:
        allowed = ", ".join(r.replace("_", " ") for r in rule.eligible_reasons)
        raise ActionDenied(
            DenialReason.INELIGIBLE_REASON,
            f'"{reason.replace("_", " ")}" isn\'t an eligible refund reason on this '
            f"plan. Eligible reasons are: {allowed}.",
        )


async def execute_mock_refund(
    db: AsyncSession,
    *,
    session_id: UUID,
    client_turn_id: str,
    turn_id: UUID | None,
    invoice_ref: str,
    amount_minor: int,
    reason: str,
    cited_chunk_ids: list[str],
) -> RefundReceipt:
    """Commit a mock refund, or return the existing receipt for this intent.

    Must be called inside a transaction. Assumes the caller has already
    established, via the guard layer, that the session is verified and the
    evidence is GROUNDED — this function re-reads authority from the database
    regardless and never trusts arguments for it.
    """
    intent_id = build_intent_id(
        session_id=session_id,
        invoice_ref=invoice_ref,
        amount_minor=amount_minor,
        client_turn_id=client_turn_id,
    )

    # 1. Lookup before any write. This is the lost-response path: the first
    #    attempt committed but the caller never saw the reply.
    existing = await _find_existing(db, intent_id)
    if existing is not None:
        return await _receipt_from_row(db, existing, replay=True)

    # 2. Lock the session row, then the invoice row. Consistent order prevents
    #    deadlock between concurrent refunds touching the same rows.
    session = (
        await db.execute(select(Session).where(Session.id == session_id).with_for_update())
    ).scalar_one()

    if session.demo_account_id is None:
        raise ActionDenied(
            DenialReason.NOT_VERIFIED,
            "I need to verify the account before I can refund anything.",
        )

    account = (
        await db.execute(select(DemoAccount).where(DemoAccount.id == session.demo_account_id))
    ).scalar_one()

    invoice = (
        await db.execute(
            select(Invoice).where(Invoice.external_ref == invoice_ref.upper()).with_for_update()
        )
    ).scalar_one_or_none()

    if invoice is None:
        raise ActionDenied(
            DenialReason.INVOICE_NOT_OWNED,
            f"I couldn't find invoice {invoice_ref.upper()} on the verified account.",
        )

    # 3. Authority comes from the database, never from tool arguments.
    rule = active_refund_rule(account.tier)
    _validate(
        rule=rule,
        account=account,
        invoice=invoice,
        amount_minor=amount_minor,
        reason=reason,
    )

    # 4. Commit the balance change and the receipt together.
    invoice.refunded_minor += amount_minor
    receipt = Refund(
        intent_id=intent_id,
        receipt_ref=_receipt_ref(),
        session_id=session_id,
        turn_id=turn_id,
        invoice_id=invoice.id,
        amount_minor=amount_minor,
        currency=invoice.currency,
        policy_document_key=rule.doc_key,
        policy_version=rule.version,
        cited_chunk_ids={"chunk_ids": cited_chunk_ids, "at": datetime.now(UTC).isoformat()},
    )
    db.add(receipt)

    try:
        await db.flush()
    except IntegrityError:
        # The genuine race: a concurrent caller committed this same intent
        # between our lookup and our flush. The unique constraint is what makes
        # double-spending impossible; recover the winner's receipt and report
        # that, so both callers agree on one outcome.
        await db.rollback()
        winner = await _find_existing(db, intent_id)
        if winner is None:
            raise
        return await _receipt_from_row(db, winner, replay=True)

    return RefundReceipt(
        receipt_ref=receipt.receipt_ref,
        intent_id=intent_id,
        invoice_ref=invoice.external_ref,
        amount_minor=amount_minor,
        currency=invoice.currency,
        policy_doc_key=rule.doc_key,
        policy_version=rule.version,
        remaining_refundable_minor=invoice.amount_minor - invoice.refunded_minor,
        idempotent_replay=False,
    )
