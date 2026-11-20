"""Typed tool failures.

Every refusal is a specific, named condition carrying a safe, user-facing
message. Nothing here ever raises a bare string, because the transport layer
turns these into `tool.failed` / `action.denied` events and the UI renders the
reason.

`safe_message` is what a customer may see. It never contains internal state,
another account's data, or a verification secret.
"""

from __future__ import annotations

from support.contracts.enums import DenialReason, ToolName


class ToolError(Exception):
    """Base class for all tool refusals and failures."""

    error_code: str = "tool_error"
    retryable: bool = False

    def __init__(self, safe_message: str) -> None:
        super().__init__(safe_message)
        self.safe_message = safe_message


class ToolNotAllowed(ToolError):
    """The runtime refused a tool that is illegal in the current state.

    This is the guard that makes tool ordering a runtime property rather than a
    prompting convention. The model may request anything; only legal calls run.
    """

    error_code = "tool_not_allowed"

    def __init__(self, tool: ToolName, state: str, allowed: frozenset[ToolName]) -> None:
        self.tool = tool
        self.state = state
        self.allowed = allowed
        super().__init__(
            "That step is not available yet. I need to complete the required checks in order first."
        )


class BudgetExhausted(ToolError):
    """The per-turn tool budget ran out."""

    error_code = "budget_exhausted"

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            "I wasn't able to finish that within the steps available for one turn. "
            "Could you restate what you need?"
        )


class ActionDenied(ToolError):
    """A validated action was refused by policy.

    Carries the structured reason so the UI can cite the governing clause
    rather than printing free text.
    """

    error_code = "action_denied"

    def __init__(self, reason: DenialReason, safe_message: str) -> None:
        self.reason = reason
        super().__init__(safe_message)


class ProviderUnavailable(ToolError):
    """A dependency failed in a way that may succeed on retry."""

    error_code = "provider_unavailable"
    retryable = True


class InjectedFault(ToolError):
    """Demo-only fault injection. Scoped to the caller's own session."""

    error_code = "injected_fault"
    retryable = True


class ToolArgumentRejected(ToolError):
    """An argument was well-formed but untrue given server state.

    Distinct from ActionDenied: nothing was refused by policy, and nothing was
    attempted. The model asserted something the server can check and is being
    asked to correct it, so this is retryable within the same turn.
    """

    error_code = "invalid_arguments"
    retryable = True
