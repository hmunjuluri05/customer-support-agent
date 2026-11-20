"""Enumerations shared by the agent, storage and transport layers.

These are the vocabulary of the whole system. They are exported to TypeScript
via JSON Schema, so the browser reducer and the Python runtime cannot drift.
"""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    """Subscription tier. Determines which policy documents are retrievable."""

    BASIC = "basic"
    PREMIUM = "premium"


class ContractStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Mode(StrEnum):
    """Which transport produced a turn. History is shared across both."""

    CHAT = "chat"
    VOICE = "voice"


class AgentState(StrEnum):
    """States of the bounded tool loop.

    The runtime, not the model, decides which tools are legal in each state.
    See support.tools.guards for the transition table.
    """

    INIT = "init"
    VERIFIED = "verified"
    EVIDENCE_GATHERED = "evidence_gathered"
    TERMINAL = "terminal"


class ToolName(StrEnum):
    VERIFY_IDENTITY = "verify_identity"
    RETRIEVE_POLICY = "retrieve_policy"
    EXECUTE_MOCK_REFUND = "execute_mock_refund"
    CREATE_ESCALATION_TICKET = "create_escalation_ticket"


class ToolStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


class GroundingDecision(StrEnum):
    """Outcome of comparing retrieval similarity against the threshold.

    Only GROUNDED permits a policy claim or an action. WEAK and UNSUPPORTED
    fail closed.
    """

    GROUNDED = "grounded"
    WEAK = "weak"
    UNSUPPORTED = "unsupported"


class TurnStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TurnOutcome(StrEnum):
    """Structured terminal outcomes. Every turn ends in exactly one."""

    GROUNDED_ANSWER = "grounded_answer"
    CLARIFICATION = "clarification"
    POLICY_DENIAL = "policy_denial"
    ACTION_SUCCESS = "action_success"
    ESCALATION = "escalation"
    CANCELLED = "cancelled"
    RECOVERABLE_FAILURE = "recoverable_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"


class EscalationReason(StrEnum):
    UNSUPPORTED_TOPIC = "unsupported_topic"
    CONFLICTING_POLICY = "conflicting_policy"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ACTION_BLOCKED = "action_blocked"
    USER_REQUESTED = "user_requested"
    TOOL_FAILURE = "tool_failure"


class DenialReason(StrEnum):
    """Why a refund was refused. Each maps to a cited policy clause."""

    EXCEEDS_TIER_CAP = "exceeds_tier_cap"
    CONTRACT_INACTIVE = "contract_inactive"
    INVOICE_NOT_OWNED = "invoice_not_owned"
    INSUFFICIENT_REFUNDABLE_BALANCE = "insufficient_refundable_balance"
    CURRENCY_MISMATCH = "currency_mismatch"
    INELIGIBLE_REASON = "ineligible_reason"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_VERIFIED = "not_verified"
    NOT_GROUNDED = "not_grounded"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class FaultKind(StrEnum):
    """Demo-only fault injection, scoped to the caller's own session."""

    NONE = "none"
    TOOL_ERROR = "tool_error"
    LLM_TIMEOUT = "llm_timeout"
    RATE_LIMIT = "rate_limit"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
    ACTION_TIMEOUT_AFTER_COMMIT = "action_timeout_after_commit"
