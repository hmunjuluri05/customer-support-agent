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

    The token is accepted from a cookie (browser) or a header (tests, curl).
    A token that does not match the requested session is rejected with 404
    rather than 403, so the endpoint does not confirm that another session
    exists.
    """
    token = cookie_token or header_token
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
