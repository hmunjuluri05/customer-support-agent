"""Local development entrypoint.

    uv run python -m support.api

Exists to solve a Windows-only ordering problem. psycopg's async mode cannot
run on ProactorEventLoop, and two obvious fixes do not work:

* Calling configure_event_loop() in the app's lifespan hook is too late -- the
  loop already exists by then.
* Setting the event loop policy before uvicorn.run() is ignored, because
  uvicorn selects its loop through an explicit loop factory rather than the
  policy.

So the loop has to be constructed here and handed to uvicorn's Server. On
Linux SelectorEventLoop is already the default, so the factory is only
overridden on Windows and the deployed container is untouched -- its command
stays the plain `uvicorn support.api.app:app`.
"""

from __future__ import annotations

import asyncio
import os
import selectors
import sys

import uvicorn


def _loop_factory() -> asyncio.AbstractEventLoop:
    """A loop psycopg can use."""
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def main() -> None:
    config = uvicorn.Config(
        "support.api.app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        access_log=False,
        # Reload needs uvicorn's own supervisor process, which would re-create
        # the loop its own way, so it is not supported through this entrypoint.
        reload=False,
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=_loop_factory) as runner:
            runner.run(server.serve())
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
