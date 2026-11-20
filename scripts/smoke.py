"""Post-deploy smoke test against the public URL.

    python scripts/smoke.py https://<host>

Checks the path a reviewer actually takes, through the real ingress, rather
than a health endpoint that proves only that a process is listening:

  1. The UI is served.
  2. The demo catalogue loads through the Next.js proxy to the internal API.
  3. A session can be created.
  4. A chat turn streams typed SSE frames and reaches exactly one terminal
     event, with the model, retrieval and the database all involved.

Deliberately read-only. It asks a policy question rather than requesting a
refund, so a deploy check never moves money or leaves the demo fixtures
dirty — a smoke test that needs a cleanup step is one failed cleanup away
from a confusing demo.

Exits non-zero with the reason on the first failure.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

TIMEOUT_S = 90
QUESTION = "What does the refund policy allow for my plan?"


class SmokeFailure(Exception):
    pass


def request(url: str, *, token: str | None = None, body: dict | None = None, sse: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    # Generic client name on purpose: nothing here should identify a person.
    req.add_header("User-Agent", "support-agent-smoke/1.0")
    if token:
        req.add_header("X-Session-Token", token)
    if sse:
        req.add_header("Accept", "text/event-stream")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise SmokeFailure(f"could not reach {url}: {exc.reason}") from exc


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not condition:
        raise SmokeFailure(label)


def main(base: str) -> int:
    base = base.rstrip("/")
    print(f"smoke: {base}")

    status, _ = request(base)
    check("UI responds", status == 200, f"HTTP {status}")

    status, body = request(f"{base}/api/demo/accounts")
    check("demo catalogue loads through the proxy", status == 200, f"HTTP {status}")
    accounts = json.loads(body)["accounts"]
    check("fixtures are seeded", len(accounts) >= 3, f"{len(accounts)} accounts")

    status, body = request(f"{base}/api/sessions", body={"account_slug": "premium-active"})
    check("session created", status == 201, f"HTTP {status}")
    session = json.loads(body)
    sid, token = session["session_id"], session["session_token"]

    started = time.monotonic()
    status, raw = request(
        f"{base}/api/sessions/{sid}/turns",
        token=token,
        body={"client_turn_id": f"smoke-{int(started)}", "text": QUESTION, "mode": "chat"},
        sse=True,
    )
    elapsed = time.monotonic() - started
    check("turn streamed", status == 200, f"HTTP {status}")

    events = [json.loads(m) for m in re.findall(r"^data: (.*)$", raw, re.M)]
    kinds = [event["type"] for event in events]

    check("tools ran", "tool.completed" in kinds, f"{kinds.count('tool.completed')} completed")
    check("retrieval decided", "grounding.decided" in kinds)
    check("exactly one terminal event", kinds.count("turn.finished") == 1, f"{elapsed:.1f}s")

    finished = next(e for e in events if e["type"] == "turn.finished")
    outcome = finished["payload"]["outcome"]
    check(
        "turn ended in a success state",
        outcome in {"grounded_answer", "clarification"},
        outcome,
    )

    print(f"smoke: all checks passed in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1]))
    except SmokeFailure as failure:
        print(f"smoke: FAILED - {failure}", file=sys.stderr)
        sys.exit(1)
