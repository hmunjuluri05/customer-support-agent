"""FastAPI application.

Providers are constructed once at startup and shared: each one owns a
connection pool and an Entra credential with a cached token, so building them
per request would add a token acquisition to every turn.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from support.api.ratelimit import RateLimiter
from support.api.routes import router
from support.api.voice import router as voice_router
from support.config import get_settings
from support.platform_compat import configure_event_loop
from support.providers.azure_openai import AzureOpenAIChat, AzureOpenAIEmbeddings
from support.providers.realtime import AzureRealtime
from support.storage.session import dispose_engine, get_sessionmaker

logger = logging.getLogger("support")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_event_loop()
    settings = get_settings()

    app.state.settings = settings
    app.state.chat = AzureOpenAIChat(settings)
    app.state.embeddings = AzureOpenAIEmbeddings(settings)
    app.state.realtime = AzureRealtime(settings)
    app.state.limiter = RateLimiter(
        per_session_per_minute=settings.rate_limit_turns_per_minute_per_session,
        per_ip_per_minute=settings.rate_limit_turns_per_minute_per_ip,
        max_concurrent=settings.max_concurrent_turns,
    )

    logger.info(
        "started model=%s embeddings=%s threshold=%.2f",
        settings.azure_openai_chat_deployment,
        settings.azure_openai_embedding_deployment,
        settings.grounding_threshold,
    )
    try:
        yield
    finally:
        await app.state.chat.aclose()
        await app.state.embeddings.aclose()
        await app.state.realtime.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Customer Support AI Agent",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs in the deployed demo: it is an attack surface
        # and not part of what a reviewer needs.
        docs_url="/api/docs" if settings.demo_mode else None,
        redoc_url=None,
    )

    # Empty allowlist is the deployed case: the web app proxies /api from the
    # same origin, so no cross-origin request is legitimate.
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-Session-Token"],
        )

    app.include_router(router)
    app.include_router(voice_router)

    @app.middleware("http")
    async def cap_request_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        length = request.headers.get("content-length")
        if length and int(length) > settings.max_request_bytes:
            return JSONResponse(
                {"error_code": "request_too_large", "limit_bytes": settings.max_request_bytes},
                status_code=413,
            )
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness. Deliberately makes no provider or database call."""
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness: database reachable and pgvector present.

        Makes no billable provider call -- a readiness probe that spends money
        on every poll is a bug.
        """
        checks: dict[str, str] = {}
        ok = True
        try:
            async with get_sessionmaker()() as db:
                await db.execute(text("SELECT 1"))
                extension = (
                    await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
                ).scalar_one_or_none()
                checks["database"] = "ok"
                checks["pgvector"] = "ok" if extension else "missing"
                ok = extension is not None
        except Exception as exc:  # a readiness probe must never raise
            checks["database"] = f"error: {type(exc).__name__}"
            ok = False

        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    return app


app = create_app()
