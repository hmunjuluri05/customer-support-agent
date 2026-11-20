"""Request helpers for the voice HTTP surface.

A plain module rather than conftest fixtures: these are functions the tests
call, not state they need set up, and importing them by name keeps it obvious
where a helper came from.
"""

from __future__ import annotations

import json

import httpx

GOOD_TOPIC = "Can I get a refund for a duplicate charge, and what is the refund limit for my plan?"


async def start_session(
    client: httpx.AsyncClient, slug: str = "premium-active"
) -> tuple[str, dict]:
    response = await client.post("/api/sessions", json={"account_slug": slug})
    assert response.status_code == 201, response.text
    data = response.json()
    return data["session_id"], {"X-Session-Token": data["session_token"]}


async def voice_call(
    client: httpx.AsyncClient,
    sid: str,
    headers: dict,
    *,
    turn: str,
    name: str,
    arguments: dict | None = None,
    call_id: str = "call-1",
) -> dict:
    response = await client.post(
        f"/api/sessions/{sid}/voice/tool",
        headers=headers,
        json={
            "client_turn_id": turn,
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def event_types(payload: dict) -> list[str]:
    return [e["type"] for e in payload["events"]]
