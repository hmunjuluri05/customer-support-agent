"""Seed and reset the synthetic demo fixtures.

Idempotent: safe to run repeatedly. Accounts and invoices are matched on their
stable slug / external_ref, so re-running updates in place rather than
duplicating.

    uv run python -m support.fixtures.seed            # create or update
    uv run python -m support.fixtures.seed --reset    # also clear demo activity

--reset is what the per-session demo reset control calls: it zeroes refunded
balances and removes receipts, tickets, turns and messages so a reviewer can
replay the same scenario from a clean state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text, update

from support.config import get_settings
from support.contracts.enums import ContractStatus, Tier
from support.platform_compat import configure_event_loop
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
from support.storage.session import dispose_engine, transaction


def fixtures_path() -> Path:
    """Locate fixtures/accounts.json.

    FIXTURES_DIR wins when set (the container sets it). Otherwise walk up from
    this file to the repository root, which is where it lives in a checkout.
    """
    settings = get_settings()
    if settings.fixtures_dir:
        return Path(settings.fixtures_dir) / "accounts.json"

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "fixtures" / "accounts.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate fixtures/accounts.json. Set FIXTURES_DIR explicitly.")


def load_fixtures() -> dict[str, Any]:
    path = fixtures_path()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("accounts"):
        raise ValueError(f"{path} contains no accounts")
    return data


async def reset_activity() -> dict[str, int]:
    """Clear the conversation and action history, and nothing structural.

    What goes: transcripts, turns, tool runs, metrics, receipts and tickets,
    across every session, plus the invoice balances those receipts moved.
    Balances travel with receipts deliberately -- deleting a refund while
    leaving the invoice showing it was refunded would be a database that
    disagrees with itself.

    What stays: the policy corpus and its embeddings, the demo accounts, the
    invoice rows themselves, and -- since this block -- the sessions.

    Sessions used to be deleted here, which quietly destroyed every resumable
    conversation in every browser, including other people's mid-demo. A
    control meant to clean up after a demo should not sign everyone out of it.
    The armed fault is cleared instead, because that is demo state too.
    """
    counts: dict[str, int] = {}
    async with transaction() as db:
        # turn_metrics, tool_runs and messages cascade from turns, but delete
        # explicitly so the reported counts are accurate.
        for label, stmt in (
            ("turn_metrics", delete(TurnMetric)),
            ("tool_runs", delete(ToolRun)),
            ("refunds", delete(Refund)),
            ("escalation_tickets", delete(EscalationTicket)),
            ("messages", delete(Message)),
            ("turns", delete(Turn)),
        ):
            # A DELETE yields a CursorResult, which carries rowcount; the
            # generic Result protocol does not, hence the narrowing cast.
            result = cast("CursorResult[Any]", await db.execute(stmt))
            counts[label] = result.rowcount or 0

        # Sessions survive, so whoever is mid-demo stays signed in and keeps
        # their resumable history. Only the demo state on them is cleared.
        result = cast(
            "CursorResult[Any]",
            await db.execute(
                update(Session)
                .where(Session.injected_fault.is_not(None))
                .values(injected_fault=None)
            ),
        )
        counts["faults_cleared"] = result.rowcount or 0

        # Restore refunded balances to their seeded values. seed() then re-applies
        # the fixture amounts, which is how INV-1002 gets its partial refund back.
        await db.execute(text("UPDATE invoices SET refunded_minor = 0"))
    return counts


async def seed(reset: bool = False) -> dict[str, Any]:
    data = load_fixtures()
    report: dict[str, Any] = {"accounts": [], "reset": None}

    if reset:
        report["reset"] = await reset_activity()

    async with transaction() as db:
        for spec in data["accounts"]:
            existing = (
                await db.execute(select(DemoAccount).where(DemoAccount.slug == spec["slug"]))
            ).scalar_one_or_none()

            if existing is None:
                account = DemoAccount(
                    slug=spec["slug"],
                    label=spec["label"],
                    customer_name=spec["customer_name"],
                    tier=Tier(spec["tier"]),
                    contract_status=ContractStatus(spec["contract_status"]),
                )
                db.add(account)
                await db.flush()
                action = "created"
            else:
                account = existing
                account.label = spec["label"]
                account.customer_name = spec["customer_name"]
                account.tier = Tier(spec["tier"])
                account.contract_status = ContractStatus(spec["contract_status"])
                action = "updated"

            invoice_refs: list[str] = []
            for inv in spec["invoices"]:
                found = (
                    await db.execute(
                        select(Invoice).where(Invoice.external_ref == inv["external_ref"])
                    )
                ).scalar_one_or_none()

                refunded = int(inv.get("refunded_minor", 0))
                amount = int(inv["amount_minor"])
                if refunded > amount:
                    raise ValueError(f"{inv['external_ref']}: refunded_minor exceeds amount_minor")

                if found is None:
                    db.add(
                        Invoice(
                            account_id=account.id,
                            external_ref=inv["external_ref"],
                            description=inv["description"],
                            amount_minor=amount,
                            refunded_minor=refunded,
                            currency=inv.get("currency", "USD"),
                        )
                    )
                else:
                    found.account_id = account.id
                    found.description = inv["description"]
                    found.amount_minor = amount
                    found.refunded_minor = refunded
                    found.currency = inv.get("currency", "USD")
                invoice_refs.append(inv["external_ref"])

            report["accounts"].append(
                {
                    "slug": account.slug,
                    "action": action,
                    "tier": account.tier.value,
                    "contract": account.contract_status.value,
                    "invoices": invoice_refs,
                }
            )

    return report


async def _main(reset: bool) -> int:
    try:
        report = await seed(reset=reset)
    finally:
        await dispose_engine()

    if report["reset"] is not None:
        cleared = ", ".join(f"{k}={v}" for k, v in report["reset"].items() if v)
        print(f"reset: {cleared or 'nothing to clear'}")

    for row in report["accounts"]:
        print(
            f"  {row['action']:8} {row['slug']:18} "
            f"{row['tier']:8} {row['contract']:9} "
            f"invoices={','.join(row['invoices'])}"
        )
    print(f"\n{len(report['accounts'])} demo accounts ready.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic demo fixtures.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear turns, messages, receipts and tickets, and restore invoice balances.",
    )
    args = parser.parse_args()

    configure_event_loop()
    return asyncio.run(_main(reset=args.reset))


if __name__ == "__main__":
    sys.exit(main())
