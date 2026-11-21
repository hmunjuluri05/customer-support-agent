"""Shared test fixtures.

Integration tests run against the real PostgreSQL instance, because the
properties under test — row locks, unique constraints, concurrent transactions
— do not exist in SQLite and would be vacuously green there.

Each test gets a fresh session row and cleans up its own writes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update

from support.api.app import create_app
from support.api.ratelimit import RateLimiter
from support.config import get_settings
from support.contracts.enums import Tier
from support.platform_compat import configure_event_loop
from support.providers.azure_openai import AzureOpenAIEmbeddings
from support.providers.realtime import RealtimeUnavailable
from support.storage.models import (
    DemoAccount,
    EscalationTicket,
    Invoice,
    Refund,
    Session,
    ToolRun,
    Turn,
)
from support.storage.session import get_sessionmaker

configure_event_loop()

requires_db = pytest.mark.skipif(
    not os.getenv("DATABASE_URL") and not get_settings().azure_openai_endpoint,
    reason="DATABASE_URL not configured",
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator:
    """A session bound to the real database, rolled back per test."""
    async with get_sessionmaker()() as session:
        yield session


@pytest_asyncio.fixture
async def premium_session(db) -> AsyncIterator[Session]:
    """A verified session bound to the seeded premium-active account."""
    account = (
        await db.execute(select(DemoAccount).where(DemoAccount.slug == "premium-active"))
    ).scalar_one()

    row = Session(
        token_hash=f"test-{uuid4().hex}",
        demo_account_id=account.id,
        verified_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(row)
    await db.commit()

    yield row

    await _cleanup(db, row.id)


@pytest_asyncio.fixture
async def basic_session(db) -> AsyncIterator[Session]:
    account = (
        await db.execute(select(DemoAccount).where(DemoAccount.slug == "basic-active"))
    ).scalar_one()
    row = Session(
        token_hash=f"test-{uuid4().hex}",
        demo_account_id=account.id,
        verified_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(row)
    await db.commit()
    yield row
    await _cleanup(db, row.id)


@pytest_asyncio.fixture
async def inactive_session(db) -> AsyncIterator[Session]:
    account = (
        await db.execute(select(DemoAccount).where(DemoAccount.slug == "premium-inactive"))
    ).scalar_one()
    row = Session(
        token_hash=f"test-{uuid4().hex}",
        demo_account_id=account.id,
        verified_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(row)
    await db.commit()
    yield row
    await _cleanup(db, row.id)


@pytest_asyncio.fixture
async def unverified_session(db) -> AsyncIterator[Session]:
    """A session with no account bound. Nothing financial may happen here."""
    row = Session(
        token_hash=f"test-{uuid4().hex}",
        demo_account_id=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(row)
    await db.commit()
    yield row
    await _cleanup(db, row.id)


async def _cleanup(db, session_id) -> None:
    """Remove this test's writes and restore invoice balances."""
    await db.execute(delete(Refund).where(Refund.session_id == session_id))
    await db.execute(delete(EscalationTicket).where(EscalationTicket.session_id == session_id))
    turn_ids = (
        (await db.execute(select(Turn.id).where(Turn.session_id == session_id))).scalars().all()
    )
    if turn_ids:
        await db.execute(delete(ToolRun).where(ToolRun.turn_id.in_(turn_ids)))
    await db.execute(delete(Turn).where(Turn.session_id == session_id))
    await db.execute(delete(Session).where(Session.id == session_id))
    await _restore_invoices(db)
    await db.commit()


async def _restore_invoices(db) -> None:
    """Put the seeded balances back.

    A refund is a durable write, so a test that issues one leaves the fixture
    account poorer for every test that follows. Deleting the session cascades
    to the receipt but cannot un-bump `invoices.refunded_minor`, which is how
    one passing test used to make a later one fail with an insufficient
    balance -- a failure that reads as a bug in the refund guard.

    INV-1002 is seeded partially refunded on purpose, so it is restored to
    that state rather than to zero.
    """
    await db.execute(update(Invoice).values(refunded_minor=0))
    await db.execute(
        text("UPDATE invoices SET refunded_minor = 3800 WHERE external_ref = 'INV-1002'")
    )


@pytest.fixture
def tiers() -> dict[str, Tier]:
    return {"premium": Tier.PREMIUM, "basic": Tier.BASIC}


# ------------------------------------------------------- app under test --
#
# One ASGI client, shared by every test that drives the HTTP surface rather
# than the modules directly. Real embeddings: a zero-vector fake cannot
# produce a GROUNDED decision, and the grounding gate is what most of these
# tests are about. The realtime provider is stubbed -- minting a session and
# exchanging SDP is verified against the live service separately and has no
# bearing on guard enforcement.


class FakeRealtime:
    """Stands in for Azure Realtime. Records what it was asked for."""

    deployment = "fake-realtime"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.minted: list[dict[str, object]] = []
        self.offers: list[str] = []

    async def mint_client_secret(self, session_config: dict[str, object]) -> dict[str, object]:
        if self.fail:
            raise RealtimeUnavailable("The voice service refused to start a session.", 503)
        self.minted.append(session_config)
        return {"value": "ek_test", "expires_at": 1795205493}

    async def exchange_sdp(self, offer_sdp: str, *, ephemeral_key: str) -> str:
        assert ephemeral_key == "ek_test"
        self.offers.append(offer_sdp)
        return "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def embeddings() -> AsyncIterator[AzureOpenAIEmbeddings]:
    """Function-scoped on purpose.

    The credential owns an HTTP connection bound to the running event loop. A
    session-scoped provider outlives the loop each test runs on, and its
    teardown then fails with "Event loop is closed" -- an error that looks like
    a product bug and is only a fixture lifetime mismatch.
    """
    provider = AzureOpenAIEmbeddings()
    yield provider
    await provider.aclose()


@pytest_asyncio.fixture
async def client(embeddings: AzureOpenAIEmbeddings) -> AsyncIterator[httpx.AsyncClient]:
    settings = get_settings()
    app = create_app()
    app.state.settings = settings
    app.state.chat = None
    app.state.embeddings = embeddings
    app.state.realtime = FakeRealtime()
    app.state.limiter = RateLimiter(
        per_session_per_minute=settings.rate_limit_turns_per_minute_per_session,
        per_ip_per_minute=settings.rate_limit_turns_per_minute_per_ip,
        max_concurrent=settings.max_concurrent_turns,
    )

    transport = httpx.ASGITransport(app=app)
    # https, not http: the session cookie is Secure, and httpx correctly
    # refuses to send a Secure cookie over http -- so an http test client
    # silently never carries it, and anything relying on the cookie (such
    # as resuming a session) would fail for a reason that is not the code's.
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c

    async with get_sessionmaker()() as db:
        await db.execute(delete(Session).where(Session.token_hash.like("%")))
        await _restore_invoices(db)
        await db.commit()
