"""Server-sent event framing.

Three details that matter in practice:

* `X-Accel-Buffering: no` and `Cache-Control: no-transform` are sent because a
  buffering proxy turns a streamed turn into a single blob at the end. The
  deployed Container Apps ingress was verified not to buffer (see build plan),
  but the headers cost nothing and protect against a future proxy.
* Heartbeat comments keep an idle connection alive. The ingress enforces a
  4-minute idle timeout; a comment resets it without appearing as an event.
* Every payload is a Pydantic model dumped in JSON mode, so enums and datetimes
  serialise identically to the generated TypeScript types.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from support.contracts.events import Event

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Defensive: disables proxy buffering where the proxy honours it.
    "X-Accel-Buffering": "no",
}

HEARTBEAT_INTERVAL_S = 15.0


def format_event(event: Event) -> bytes:
    """Frame one typed event.

    The SSE `event:` field carries the discriminator, so a client can dispatch
    on it without parsing the body first.
    """
    body = event.model_dump(mode="json")
    return f"event: {body['type']}\ndata: {json.dumps(body, separators=(',', ':'))}\n\n".encode()


def format_comment(text: str) -> bytes:
    return f": {text}\n\n".encode()


async def with_heartbeat(
    events: AsyncIterator[Event],
    *,
    interval_s: float = HEARTBEAT_INTERVAL_S,
) -> AsyncIterator[bytes]:
    """Interleave heartbeat comments into an event stream.

    A turn can spend many seconds inside a single model call with nothing to
    emit. Without this the connection looks idle to every intermediary.
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[Event] | None = None

    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_anext(iterator))

            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                yield format_comment("hb")
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None

            yield format_event(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


async def _anext(iterator: AsyncIterator[Event]) -> Event:
    return await iterator.__anext__()
