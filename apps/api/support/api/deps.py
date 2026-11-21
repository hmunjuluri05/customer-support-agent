"""Request dependencies: session credentials, authorisation, providers.

Session identity is an opaque random token. Only its SHA-256 hash is stored, so
a database dump does not yield usable credentials.

Authorisation is checked on every session-scoped route: holding a token for
session A must never read session B's transcript, traces or receipts. That is
the isolation requirement, and it is enforced here rather than per-route.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support.config import Settings, get_settings
from support.storage.models import Session
from support.storage.session import get_sessionmaker

SESSION_COOKIE = "support_session"

# Identifies the browser, not the person and not the account.
#
# Long-lived and opaque, so a visitor returning after a restart can be offered
# the sessions they created -- including one per demo account, which a single
# session cookie cannot express because it names only the newest.
BROWSER_COOKIE = "support_browser"

# Long enough to outlive any plausible demo gap, short enough that an abandoned
# browser does not carry a resumable identity indefinitely.
BROWSER_COOKIE_MAX_AGE_S = 30 * 24 * 60 * 60


def new_browser_id() -> str:
    """An unguessable browser identifier.

    Unguessable matters: it is the only thing standing between a visitor and
    someone else's transcript, because the demo account is a public choice.
    """
    return secrets.token_urlsafe(24)


def new_token() -> tuple[str, str]:
    """Returns (plaintext token, storage hash)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def settings_dep() -> Settings:
    return get_settings()


def client_ip(request: Request) -> str:
    """Caller IP for rate limiting.

    Trusts X-Forwarded-For's first hop because the app only ever runs behind
    the Container Apps ingress, which sets it. There is no path for a client to
    reach the container directly.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def authorise_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    cookie_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    header_token: str | None = Header(default=None, alias="X-Session-Token"),
) -> Session:
    """Resolve and authorise the session named in the path.

    The token is accepted from a header (tests, curl, the web client) or a
    cookie (a browser that has lost its token, after a restart). A token that
    does not match the requested session is rejected with 404 rather than 403,
    so the endpoint does not confirm that another session exists.

    **Header first.** The header is an explicit credential; the cookie is
    ambient authority the browser attaches to every request. Preferring the
    cookie means a caller presenting a deliberate credential gets a different
    one used instead -- so a request carrying another session's token would be
    authorised as the cookie's session rather than refused. Three isolation
    tests only passed because the test client was on http and never sent the
    Secure cookie at all; making them realistic exposed the ordering.
    """
    token = header_token or cookie_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No session credential. Create a session first.",
        )

    row = (
        await db.execute(select(Session).where(Session.token_hash == hash_token(token)))
    ).scalar_one_or_none()

    if row is None or row.id != session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    if row.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Start a new one.",
        )

    return row
