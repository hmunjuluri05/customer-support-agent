"""Realtime session configuration for the voice path.

The voice agent is the same agent: same system prompt, same four tools, same
executor behind them. What differs is the transport and the fact that replies
are spoken, so the prompt gains a short addendum about speech and the tool
schemas are converted to the realtime format.

Two deliberate differences from the chat loop:

* **All four tools are advertised, not just the legal ones.** The chat loop
  narrows the tool list per state (`available_tools`), which it can do because
  it rebuilds the request on every step. A realtime session fixes its tool list
  once at mint time, and the state advances mid-conversation. Advertising all
  four and letting the executor refuse the illegal ones is the same guarantee
  arrived at differently -- and it exercises the guard rather than hiding it.
* **No structured output envelope.** There is nothing to parse: spoken replies
  are audio, and citations are rendered from the evidence the server recorded
  for the turn rather than from anything the model formats.
"""

from __future__ import annotations

from typing import Any

from support.agent.prompts import SYSTEM_PROMPT, tool_definitions
from support.contracts.enums import ToolName

VOICE_ADDENDUM = """

You are speaking this reply aloud over a phone-quality connection.

- Keep replies to one or two sentences. Say the single most useful thing first.
- Never read out a chunk id, a URL, or an intent hash. Name the document
  instead: "the Premium refund policy".
- Read amounts as money: "fifteen dollars", not "1500".
- Read a reference like RF-1A2B3C as separate characters, slowly.
- If the customer interrupts you, stop and listen. Do not finish the sentence.
- Never guess an invoice reference. Ask the customer to read it back.
"""

# Server-side VAD: Azure detects speech start and end, which is what makes
# barge-in work without us doing onset detection on raw PCM.
TURN_DETECTION: dict[str, Any] = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 600,
    # The model starts answering as soon as the customer stops, and an
    # interruption truncates whatever is playing.
    "create_response": True,
    "interrupt_response": True,
}


def realtime_tools() -> list[dict[str, Any]]:
    """The four tools, flattened into the realtime schema.

    Chat completions nests under `function`; realtime does not. Converting from
    one source keeps the two paths from drifting into different descriptions of
    the same tool.
    """
    tools: list[dict[str, Any]] = []
    for spec in tool_definitions(frozenset(ToolName)):
        function = spec["function"]
        assert isinstance(function, dict)
        tools.append(
            {
                "type": "function",
                "name": function["name"],
                "description": function["description"],
                "parameters": function["parameters"],
            }
        )
    return tools


def session_config(*, voice: str = "alloy") -> dict[str, Any]:
    """The configuration pinned at mint time, before the browser connects."""
    return {
        "instructions": SYSTEM_PROMPT + VOICE_ADDENDUM,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                # Transcribing the customer's own speech is what lets a voice
                # turn be persisted as text and shown in the shared transcript.
                "transcription": {"model": "whisper-1"},
                "turn_detection": TURN_DETECTION,
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
                "speed": 1.0,
            },
        },
        "tools": realtime_tools(),
        "tool_choice": "auto",
    }
