"""The tool-ordering state machine.

This is the architectural centre of the system. The transition table below —
not a prompt, not the model's reasoning — decides which tools may run.

Both transports share it:

  * Chat runs our own bounded tool loop.
  * Voice runs the Realtime model's function-calling loop.

Because enforcement sits in the executor rather than in either loop, the two
cannot diverge, and no prompt injection or model error can reorder the chain.
The model proposes; the runtime disposes.

Reaching EVIDENCE_GATHERED requires passing through VERIFIED, which is why a
refund can never execute against an unverified session: there is no path.
"""

from __future__ import annotations

from support.contracts.enums import AgentState, ToolName

# Which tools are legal in each state.
ALLOWED: dict[AgentState, frozenset[ToolName]] = {
    AgentState.INIT: frozenset(
        {
            ToolName.VERIFY_IDENTITY,
            # Permitted unverified, but only ever returns globally-applicable
            # documents (see retrieve_policy: tier is None until verified), so
            # public troubleshooting works without a sign-in.
            ToolName.RETRIEVE_POLICY,
            ToolName.CREATE_ESCALATION_TICKET,
        }
    ),
    AgentState.VERIFIED: frozenset(
        {
            # Re-verification is harmless and idempotent.
            ToolName.VERIFY_IDENTITY,
            ToolName.RETRIEVE_POLICY,
            ToolName.CREATE_ESCALATION_TICKET,
        }
    ),
    AgentState.EVIDENCE_GATHERED: frozenset(
        {
            ToolName.RETRIEVE_POLICY,
            ToolName.EXECUTE_MOCK_REFUND,
            ToolName.CREATE_ESCALATION_TICKET,
        }
    ),
    # A turn that has reached a terminal outcome runs no further tools.
    AgentState.TERMINAL: frozenset(),
}

# State after a tool succeeds. A tool absent for a state does not transition.
_TRANSITIONS: dict[tuple[AgentState, ToolName], AgentState] = {
    (AgentState.INIT, ToolName.VERIFY_IDENTITY): AgentState.VERIFIED,
    (AgentState.VERIFIED, ToolName.VERIFY_IDENTITY): AgentState.VERIFIED,
    # Unverified retrieval gathers only global evidence, so it must NOT advance
    # to EVIDENCE_GATHERED -- that state is what authorises a refund.
    (AgentState.INIT, ToolName.RETRIEVE_POLICY): AgentState.INIT,
    (AgentState.VERIFIED, ToolName.RETRIEVE_POLICY): AgentState.EVIDENCE_GATHERED,
    (AgentState.EVIDENCE_GATHERED, ToolName.RETRIEVE_POLICY): AgentState.EVIDENCE_GATHERED,
    (AgentState.EVIDENCE_GATHERED, ToolName.EXECUTE_MOCK_REFUND): AgentState.TERMINAL,
    (AgentState.INIT, ToolName.CREATE_ESCALATION_TICKET): AgentState.TERMINAL,
    (AgentState.VERIFIED, ToolName.CREATE_ESCALATION_TICKET): AgentState.TERMINAL,
    (AgentState.EVIDENCE_GATHERED, ToolName.CREATE_ESCALATION_TICKET): AgentState.TERMINAL,
}


def is_allowed(state: AgentState, tool: ToolName) -> bool:
    return tool in ALLOWED[state]


def allowed_tools(state: AgentState) -> frozenset[ToolName]:
    """Tools the model may be offered in this state.

    Used to build the function list sent to the provider, so the model is not
    even shown a tool it could not legally call.
    """
    return ALLOWED[state]


def next_state(state: AgentState, tool: ToolName) -> AgentState:
    """State after `tool` succeeds in `state`.

    Unknown pairs leave the state unchanged rather than guessing, so adding a
    tool without a transition cannot silently grant progress toward an action.
    """
    return _TRANSITIONS.get((state, tool), state)


def requires_verification(tool: ToolName) -> bool:
    return tool is ToolName.EXECUTE_MOCK_REFUND


def is_terminal(state: AgentState) -> bool:
    return state is AgentState.TERMINAL


def reachable_states_for_refund() -> frozenset[AgentState]:
    """States from which a refund may execute. Exactly one, by construction."""
    return frozenset(
        state for state, tools in ALLOWED.items() if ToolName.EXECUTE_MOCK_REFUND in tools
    )
