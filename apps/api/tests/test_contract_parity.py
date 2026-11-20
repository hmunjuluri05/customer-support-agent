"""The browser's types must not drift from the Python contract.

apps/web/lib/events.ts is written by hand rather than generated, which is a
deliberate trade (a generated union from Pydantic's JSON Schema needs enough
post-processing that the generator becomes its own thing to maintain). The
trade is only safe if drift is caught mechanically, which is this file's job.

A mismatch here is not a style problem. If Python gains an event type or an
enum member the reducer does not know, the browser silently ignores that event
-- a tool call that never appears, or a denial reason rendered as `undefined`.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

import pytest

from support.contracts.enums import (
    ContractStatus,
    DenialReason,
    EscalationReason,
    FaultKind,
    GroundingDecision,
    Mode,
    Tier,
    ToolName,
    TurnOutcome,
)
from support.contracts.events import EVENT_CONTRACT_VERSION, Event

EVENTS_TS = Path(__file__).resolve().parents[2] / "web" / "lib" / "events.ts"

# TypeScript union alias -> the Python enum it mirrors.
ENUM_PAIRS = {
    "Tier": Tier,
    "ContractStatus": ContractStatus,
    "Mode": Mode,
    "ToolName": ToolName,
    "GroundingDecision": GroundingDecision,
    "TurnOutcome": TurnOutcome,
    "DenialReason": DenialReason,
    "EscalationReason": EscalationReason,
    "FaultKind": FaultKind,
}


@pytest.fixture(scope="module")
def typescript() -> str:
    if not EVENTS_TS.is_file():
        pytest.fail(f"Cannot find the browser contract at {EVENTS_TS}")
    return EVENTS_TS.read_text(encoding="utf-8")


def python_event_types() -> set[str]:
    """Discriminator literals of the Event union."""
    union = typing.get_args(Event)[0]
    return {model.model_fields["type"].default for model in typing.get_args(union)}


def ts_string_union(source: str, alias: str) -> set[str]:
    """Members of `export type <alias> = 'a' | 'b' ...`, up to the semicolon."""
    match = re.search(rf"export type {alias}\s*=\s*(.*?);", source, re.DOTALL)
    if match is None:
        pytest.fail(f"No `export type {alias}` in {EVENTS_TS.name}")
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_event_types_match(typescript: str) -> None:
    # StreamEventType is derived (`StreamEvent['type']`), so there is no
    # literal union to read. EVENT_TYPES is the checkable list, and it is also
    # the one that matters: it is what isStreamEvent() validates incoming
    # frames against, so an event type missing from it is an event the browser
    # discards. `satisfies readonly StreamEventType[]` makes tsc reject a
    # member that is not a real event type, so this only needs to catch the
    # other direction -- a Python event type nobody added here.
    match = re.search(r"export const EVENT_TYPES = \[(.*?)\]", typescript, re.DOTALL)
    assert match is not None, "EVENT_TYPES array missing from the browser contract"
    ts_types = set(re.findall(r"'([^']+)'", match.group(1)))

    python_types = python_event_types()

    assert ts_types == python_types, (
        f"event contract drift\n"
        f"  missing in TypeScript: {sorted(python_types - ts_types)}\n"
        f"  unknown to Python:     {sorted(ts_types - python_types)}"
    )


@pytest.mark.parametrize("alias", sorted(ENUM_PAIRS))
def test_enum_members_match(typescript: str, alias: str) -> None:
    enum_cls = ENUM_PAIRS[alias]
    ts_members = ts_string_union(typescript, alias)
    py_members = {member.value for member in enum_cls}

    assert ts_members == py_members, (
        f"{alias} drift\n"
        f"  missing in TypeScript: {sorted(py_members - ts_members)}\n"
        f"  unknown to Python:     {sorted(ts_members - py_members)}"
    )


def test_each_event_type_is_handled_by_the_reducer() -> None:
    """A known event type with no reducer case is a dropped event."""
    reducer = EVENTS_TS.parent / "reducer.ts"
    source = reducer.read_text(encoding="utf-8")
    handled = set(re.findall(r"case '([a-z.]+)':", source))

    missing = python_event_types() - handled
    assert not missing, f"reducer.ts has no case for: {sorted(missing)}"


# Events whose TypeScript payload is not written as an inline object literal,
# with the reason. Asserted explicitly below so this check cannot quietly
# shrink to covering nothing.
_NOT_INLINE: set[str] = set()


def ts_payload_fields(source: str, interface: str) -> set[str] | None:
    """Field names of `payload: { ... }` inside one exported interface.

    Returns None when the payload is not written as an inline object literal.
    Brace depth is tracked, so a field of a nested object is not mistaken for
    a payload field.
    """
    pattern = r"export interface " + interface + r" extends Envelope \{(.*?)\n\}"
    match = re.search(pattern, source, re.DOTALL)
    if match is None:
        pytest.fail(f"No `export interface {interface}` in {EVENTS_TS.name}")
    body = match.group(1)

    start = body.find("payload:")
    if start == -1:
        pytest.fail(f"{interface} has no payload")
    brace = body.find("{", start)
    if brace == -1:
        return None

    # Comments carry colons and braces of their own; strip them first.
    region = re.sub(r"/\*.*?\*/", "", body[brace:], flags=re.DOTALL)
    region = re.sub(r"//[^\n]*", "", region)

    depth = 0
    fields: set[str] = set()
    token = ""
    for char in region:
        if char == "{":
            depth += 1
            token = ""
        elif char == "}":
            depth -= 1
            token = ""
            if depth == 0:
                break
        elif char in ";\n":
            token = ""
        elif char == ":" and depth == 1:
            name = token.strip().rstrip("?")
            if name:
                fields.add(name)
            token = ""
        else:
            token += char
    return fields


def python_payload_fields(event_type: str) -> set[str]:
    union = typing.get_args(Event)[0]
    for model in typing.get_args(union):
        if model.model_fields["type"].default != event_type:
            continue
        payload = model.model_fields["payload"].annotation
        return set(payload.model_fields)
    pytest.fail(f"No Python event with type {event_type!r}")


def ts_interface_for(source: str, event_type: str) -> str:
    """The interface whose `type` literal is this event type."""
    pattern = (
        r"export interface (\w+) extends Envelope \{\s*\n\s*type: '" + re.escape(event_type) + r"';"
    )
    match = re.search(pattern, source)
    if match is None:
        pytest.fail(f"No TypeScript interface declares type '{event_type}'")
    return match.group(1)


@pytest.mark.parametrize("event_type", sorted(python_event_types()))
def test_payload_fields_match(typescript: str, event_type: str) -> None:
    """Field-level drift, which the event-name check above cannot see.

    Added after a payload gained a field on the server -- `reconciled`, which
    tells a recovered commit apart from a duplicate submit -- and nothing in
    the suite would have noticed the browser not knowing about it. A missing
    field does not break the build: it reads as `undefined`, which for a
    boolean means the UI silently takes the other branch.
    """
    interface = ts_interface_for(typescript, event_type)
    ts_fields = ts_payload_fields(typescript, interface)

    if ts_fields is None:
        assert event_type in _NOT_INLINE, (
            f"{interface}'s payload is not an inline object literal, so this test cannot "
            f"check it. Either inline it, or add '{event_type}' to _NOT_INLINE with a reason."
        )
        return

    py_fields = python_payload_fields(event_type)
    assert ts_fields == py_fields, (
        f"{event_type} payload drift"
        f"\n  missing in TypeScript: {sorted(py_fields - ts_fields)}"
        f"\n  unknown to Python:     {sorted(ts_fields - py_fields)}"
    )


def test_contract_version_matches(typescript: str) -> None:
    match = re.search(r"EVENT_CONTRACT_VERSION = (\d+)", typescript)
    assert match is not None, "EVENT_CONTRACT_VERSION missing from the browser contract"
    assert int(match.group(1)) == EVENT_CONTRACT_VERSION
