"""Platform compatibility shims.

Windows defaults to ProactorEventLoop, which psycopg's async mode cannot use:

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
    in async mode.

The deployed container is Linux and unaffected, but local development,
migrations and the seed script all hit this. Call configure_event_loop()
before any asyncio.run() that touches the database.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop() -> None:
    """Select an event loop implementation psycopg can use.

    No-op off Windows.
    """
    if sys.platform != "win32":
        return

    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover - older interpreters only
        return

    if not isinstance(asyncio.get_event_loop_policy(), policy_cls):
        asyncio.set_event_loop_policy(policy_cls())
