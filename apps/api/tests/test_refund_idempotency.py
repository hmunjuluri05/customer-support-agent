"""Refund idempotency and policy enforcement, against real PostgreSQL.

These cannot be meaningfully faked: they test row locks, a unique constraint,
and two transactions racing. SQLite would pass them vacuously.

The headline test is `test_concurrent_same_intent_commits_once`, which is the
"simulate a duplicate request and a lost response without issuing a second
refund" requirement.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from support.contracts.enums import DenialReason
from support.storage.models import Invoice, Refund
from support.storage.session import get_sessionmaker
from support.tools.errors import ActionDenied
from support.tools.refund import build_intent_id, execute_mock_refund

# asyncio_mode = "auto" in pyproject handles async tests; an explicit
# module-level mark would also (wrongly) tag the sync tests here.

GROUNDED_CITATIONS = ["refund-policy-premium@2.0#01:deadbeefcafe"]


async def _refund(session_id, *, invoice_ref, amount_minor, turn="t1", reason="billing_error"):
    """Commit a refund in its own transaction, as the real caller does."""
    async with get_sessionmaker()() as db, db.begin():
        return await execute_mock_refund(
            db,
            session_id=session_id,
            client_turn_id=turn,
            turn_id=None,
            invoice_ref=invoice_ref,
            amount_minor=amount_minor,
            reason=reason,
            cited_chunk_ids=GROUNDED_CITATIONS,
        )


async def _count_refunds(intent_id: str) -> int:
    async with get_sessionmaker()() as db:
        return (
            await db.execute(
                select(func.count()).select_from(Refund).where(Refund.intent_id == intent_id)
            )
        ).scalar_one()


async def _refunded_minor(ref: str) -> int:
    async with get_sessionmaker()() as db:
        return (
            await db.execute(select(Invoice.refunded_minor).where(Invoice.external_ref == ref))
        ).scalar_one()


# ----------------------------------------------------------------- happy path --


async def test_premium_refund_within_cap_succeeds(premium_session) -> None:
    receipt = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500)

    assert receipt.receipt_ref.startswith("RF-")
    assert receipt.amount_minor == 1500
    assert receipt.policy_version == "2.0", "must cite the ACTIVE policy version"
    assert receipt.idempotent_replay is False
    assert await _refunded_minor("INV-1001") == 1500


# -------------------------------------------------------------- idempotency --


async def test_retry_same_intent_returns_original_receipt(premium_session) -> None:
    """The lost-response path: first call committed, caller never saw the reply."""
    first = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500)
    second = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500)

    assert second.receipt_ref == first.receipt_ref
    assert second.idempotent_replay is True
    assert await _refunded_minor("INV-1001") == 1500, "invoice must move exactly once"
    assert await _count_refunds(first.intent_id) == 1


async def test_concurrent_same_intent_commits_once(premium_session) -> None:
    """Two simultaneous commits of one intent produce one receipt.

    This is the race the unique constraint exists for: both callers pass the
    pre-flight lookup, then one loses at flush and recovers the winner's
    receipt. Both must agree, and the money must move once.
    """
    results = await asyncio.gather(
        _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500),
        _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"both callers should succeed, got {failures}"

    refs = {r.receipt_ref for r in results}
    assert len(refs) == 1, f"two receipts issued for one intent: {refs}"
    assert await _refunded_minor("INV-1001") == 1500
    assert await _count_refunds(results[0].intent_id) == 1


async def test_new_turn_creates_new_intent(premium_session) -> None:
    """An explicitly-requested second partial refund is NOT deduplicated.

    Same amount, different turn: a genuine new request must commit separately,
    or a customer could never request the same amount twice.
    """
    first = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1000, turn="t1")
    second = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1000, turn="t2")

    assert first.intent_id != second.intent_id
    assert first.receipt_ref != second.receipt_ref
    assert await _refunded_minor("INV-1001") == 2000


def test_intent_id_is_stable_and_scoped() -> None:
    from uuid import uuid4

    a, b = uuid4(), uuid4()
    base = dict(invoice_ref="INV-1001", amount_minor=1500, client_turn_id="t1")

    assert build_intent_id(session_id=a, **base) == build_intent_id(session_id=a, **base)
    assert build_intent_id(session_id=a, **base) != build_intent_id(session_id=b, **base)
    # Case-insensitive on the invoice reference, so "inv-1001" cannot bypass it.
    assert build_intent_id(session_id=a, **base) == build_intent_id(
        session_id=a, invoice_ref="inv-1001", amount_minor=1500, client_turn_id="t1"
    )


# ----------------------------------------------------------------- denials --


async def test_premium_over_cap_denied(premium_session) -> None:
    with pytest.raises(ActionDenied) as exc:
        await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=4000)

    assert exc.value.reason is DenialReason.EXCEEDS_TIER_CAP
    assert "25.00" in exc.value.safe_message
    assert await _refunded_minor("INV-1001") == 0, "nothing may be written on denial"


async def test_basic_denied_same_amount_premium_allows(basic_session) -> None:
    """The tier discrimination demo: $15 is fine on Premium, not on Basic."""
    with pytest.raises(ActionDenied) as exc:
        await _refund(basic_session.id, invoice_ref="INV-2001", amount_minor=1500)

    assert exc.value.reason is DenialReason.EXCEEDS_TIER_CAP
    assert "10.00" in exc.value.safe_message
    assert await _refunded_minor("INV-2001") == 0


async def test_inactive_contract_denied_before_amount_check(inactive_session) -> None:
    """Contract status is evaluated before the cap.

    $15 is within the Premium cap, so a denial on amount would be the wrong
    reason and would cite the wrong policy.
    """
    with pytest.raises(ActionDenied) as exc:
        await _refund(inactive_session.id, invoice_ref="INV-3001", amount_minor=1500)

    assert exc.value.reason is DenialReason.CONTRACT_INACTIVE


async def test_foreign_invoice_denied(premium_session) -> None:
    """Ownership guard: INV-2001 belongs to the Basic account."""
    with pytest.raises(ActionDenied) as exc:
        await _refund(premium_session.id, invoice_ref="INV-2001", amount_minor=500)

    assert exc.value.reason is DenialReason.INVOICE_NOT_OWNED
    assert await _refunded_minor("INV-2001") == 0


async def test_exceeding_refundable_balance_denied(premium_session) -> None:
    """INV-1002 is seeded with $38.00 of $40.00 already refunded."""
    with pytest.raises(ActionDenied) as exc:
        await _refund(premium_session.id, invoice_ref="INV-1002", amount_minor=1000)

    assert exc.value.reason is DenialReason.INSUFFICIENT_REFUNDABLE_BALANCE
    assert "2.00" in exc.value.safe_message


async def test_remaining_balance_refund_succeeds(premium_session) -> None:
    receipt = await _refund(premium_session.id, invoice_ref="INV-1002", amount_minor=200)
    assert receipt.remaining_refundable_minor == 0


async def test_unverified_session_cannot_refund(unverified_session) -> None:
    with pytest.raises(ActionDenied) as exc:
        await _refund(unverified_session.id, invoice_ref="INV-1001", amount_minor=100)

    assert exc.value.reason is DenialReason.NOT_VERIFIED


async def test_ineligible_reason_denied(basic_session) -> None:
    """Outage credits are Premium-only; Basic must refuse the reason."""
    with pytest.raises(ActionDenied) as exc:
        await _refund(
            basic_session.id,
            invoice_ref="INV-2001",
            amount_minor=500,
            reason="service_outage",
        )

    assert exc.value.reason is DenialReason.INELIGIBLE_REASON


async def test_the_losing_caller_recovers_the_winners_receipt(premium_session, monkeypatch) -> None:
    """The loser's branch, reached deterministically rather than by racing.

    `test_concurrent_same_intent_commits_once` only enters this branch when
    two callers genuinely interleave. Against a database a network away they
    usually do not: the second caller's pre-flight lookup finds the first
    caller's row and takes the happy path, so the branch goes unexercised and
    the suite stays green. CI, with Postgres in the same container, collided
    for real on the first run and found the recovery broken -- it called
    `db.rollback()` inside `async with session.begin()`, which SQLAlchemy
    refuses, so the loser raised instead of recovering.

    Blinding the pre-flight lookup once reproduces exactly that collision on
    any machine: the second caller believes nothing exists, writes, loses on
    the unique constraint, and must recover.
    """
    first = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500)

    import support.tools.refund as refund_module

    real_find_existing = refund_module._find_existing
    blinded = {"used": False}

    async def find_existing_missing_once(db, intent_id):
        if not blinded["used"]:
            blinded["used"] = True
            return None
        return await real_find_existing(db, intent_id)

    monkeypatch.setattr(refund_module, "_find_existing", find_existing_missing_once)

    second = await _refund(premium_session.id, invoice_ref="INV-1001", amount_minor=1500)

    assert blinded["used"], "the pre-flight lookup was never blinded"
    assert second.receipt_ref == first.receipt_ref
    assert second.idempotent_replay is True
    # The money is the point: the loser's balance increment was rolled back
    # with its savepoint, so the invoice moved exactly once.
    assert await _refunded_minor("INV-1001") == 1500
    assert await _count_refunds(first.intent_id) == 1
