"""Ingress smoke test.

Deployed to the Container Apps environment before any application code exists,
to verify three things through the real public ingress:

  1. HTTPS terminates and routes to the container on the configured target port
  2. The managed identity can pull the image from ACR
  3. Server-sent events are streamed incrementally, NOT buffered until complete

(3) is the one that matters. If the ingress buffered responses, streaming chat
would be impossible and we would want to know now, not at Block 4.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="ingress-smoke")

STARTED = time.monotonic()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "ingress-smoke",
            "port": os.getenv("PORT", "3000"),
            "uptime_s": round(time.monotonic() - STARTED, 1),
        }
    )


@app.get("/readyz")
async def readyz() -> JSONResponse:
    # Deliberately makes no provider or database call.
    return JSONResponse({"status": "ready"})


async def _events(count: int, delay: float) -> AsyncIterator[bytes]:
    """Emit `count` SSE events `delay` seconds apart.

    A client that receives event 1 at ~delay seconds and event N at ~N*delay
    seconds is being streamed. A client that receives everything at once at
    ~N*delay seconds is behind a buffering proxy.
    """
    t0 = time.monotonic()
    for i in range(1, count + 1):
        elapsed = round(time.monotonic() - t0, 3)
        yield f"event: tick\ndata: {{\"seq\": {i}, \"server_elapsed_s\": {elapsed}}}\n\n".encode()
        if i < count:
            await asyncio.sleep(delay)
    yield b"event: done\ndata: {}\n\n"


@app.get("/debug/sse")
async def debug_sse(count: int = 5, delay: float = 1.0) -> StreamingResponse:
    return StreamingResponse(
        _events(count, delay),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
