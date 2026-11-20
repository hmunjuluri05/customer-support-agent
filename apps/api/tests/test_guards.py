"""State machine guard tests.

These are pure: no database, no provider, no model. They assert the ordering
contract itself, which is the property the whole safety argument rests on.
"""

from __future__ import annotations

import pytest

from support.contracts.enums import AgentState, ToolName
from support.tools import guards


def test_refund_reachable_from_exactly_one_state() -> None:
    """The refund must have exactly one legal entry point.

    If this ever grows a second state, the "cannot refund without verified
    evidence" argument needs re-deriving from scratch.
    """
    assert guards.reachable_states_for_refund() == frozenset({AgentState.EVIDENCE_GATHERED})


def test_refund_not_allowed_before_evidence() -> None:
    for state in (AgentState.INIT, AgentState.VERIFIED, AgentState.TERMINAL):
        assert not guards.is_allowed(state, ToolName.EXECUTE_MOCK_REFUND), state


def test_unverified_retrieval_does_not_advance_to_evidence() -> None:
    """The core of the privilege-escalation defence.

    Retrieval is permitted while unverified so public troubleshooting works,
    but it must NOT reach EVIDENCE_GATHERED -- otherwise an unverified session
    could retrieve a global document and then attempt a refund.
    """
    assert guards.is_allowed(AgentState.INIT, ToolName.RETRIEVE_POLICY)
    assert guards.next_state(AgentState.INIT, ToolName.RETRIEVE_POLICY) is AgentState.INIT


def test_evidence_requires_passing_through_verified() -> None:
    """No path to EVIDENCE_GATHERED skips VERIFIED."""
    predecessors = [
        state
        for state in AgentState
        for tool in ToolName
        if guards.is_allowed(state, tool)
        and guards.next_state(state, tool) is AgentState.EVIDENCE_GATHERED
        and state is not AgentState.EVIDENCE_GATHERED
    ]
    assert predecessors == [AgentState.VERIFIED]


def test_verification_advances_state() -> None:
    assert guards.next_state(AgentState.INIT, ToolName.VERIFY_IDENTITY) is AgentState.VERIFIED


def test_happy_path_sequence() -> None:
    """The three-call refund chain, in order."""
    state = AgentState.INIT

    assert guards.is_allowed(state, ToolName.VERIFY_IDENTITY)
    state = guards.next_state(state, ToolName.VERIFY_IDENTITY)
    assert state is AgentState.VERIFIED

    assert guards.is_allowed(state, ToolName.RETRIEVE_POLICY)
    state = guards.next_state(state, ToolName.RETRIEVE_POLICY)
    assert state is AgentState.EVIDENCE_GATHERED

    assert guards.is_allowed(state, ToolName.EXECUTE_MOCK_REFUND)
    state = guards.next_state(state, ToolName.EXECUTE_MOCK_REFUND)
    assert state is AgentState.TERMINAL


def test_terminal_state_runs_no_tools() -> None:
    assert guards.allowed_tools(AgentState.TERMINAL) == frozenset()
    for tool in ToolName:
        assert not guards.is_allowed(AgentState.TERMINAL, tool), tool


def test_escalation_available_from_every_non_terminal_state() -> None:
    """Escalation must always be reachable: there is no dead end."""
    for state in (AgentState.INIT, AgentState.VERIFIED, AgentState.EVIDENCE_GATHERED):
        assert guards.is_allowed(state, ToolName.CREATE_ESCALATION_TICKET), state
        assert guards.next_state(state, ToolName.CREATE_ESCALATION_TICKET) is AgentState.TERMINAL


def test_unknown_transition_does_not_advance() -> None:
    """An unmapped pair must not grant progress.

    Adding a tool without a transition should leave the state alone rather
    than defaulting to something permissive.
    """
    assert guards.next_state(AgentState.TERMINAL, ToolName.VERIFY_IDENTITY) is AgentState.TERMINAL


@pytest.mark.parametrize("state", list(AgentState))
def test_allowed_table_is_total(state: AgentState) -> None:
    """Every state has an explicit entry; no KeyError at runtime."""
    assert isinstance(guards.allowed_tools(state), frozenset)
