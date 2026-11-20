"""Typed stream event contract.

Every event the browser receives — over SSE for chat, over the data channel for
voice — is one of these. `Event` is a discriminated union on `type`, so the
TypeScript reducer gets exhaustive narrowing for free.

Invariants:
  * `sequence` is monotonic per turn, starting at 0.
  * Exactly one `turn.finished` terminates a turn.
  * No `answer.delta` is emitted for a policy-bearing turn before
    `grounding.decided`, and never before citations are validated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support.contracts.enums import (
    ContractStatus,
    DenialReason,
    EscalationReason,
    GroundingDecision,
    Tier,
    ToolName,
    TurnOutcome,
)

EVENT_CONTRACT_VERSION = 1


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------ payloads --


class TurnStartedPayload(_Base):
    mode: str
    client_turn_id: str


class TranscriptPayload(_Base):
    """Voice transcripts. Partials are visually distinct and never become turns."""

    text: str
    is_final: bool


class ToolStartedPayload(_Base):
    tool: ToolName
    call_index: int = Field(ge=0, description="0-based index within this turn's tool budget")
    # Redacted summary only. Never raw arguments, never a verification code.
    summary: str


class ToolCompletedPayload(_Base):
    tool: ToolName
    call_index: int
    duration_ms: int
    summary: str


class ToolFailedPayload(_Base):
    tool: ToolName
    call_index: int
    duration_ms: int
    error_code: str
    retryable: bool
    summary: str


class VerifiedIdentityPayload(_Base):
    """Emitted after verify_identity. Resolved from session state, not model input."""

    customer_name: str
    tier: Tier
    contract_status: ContractStatus


class Citation(_Base):
    chunk_id: str
    document_title: str
    document_version: str
    heading_path: str
    similarity: float


class GroundingDecidedPayload(_Base):
    decision: GroundingDecision
    threshold: float
    top_similarity: float | None
    accepted_chunks: int
    citations: list[Citation]


class AnswerDeltaPayload(_Base):
    text: str


class ActionCommittedPayload(_Base):
    """A refund receipt. Only ever emitted from a durable, persisted row."""

    receipt_ref: str
    intent_id: str
    invoice_ref: str
    amount_minor: int
    currency: str
    policy_version: str
    idempotent_replay: bool = Field(
        default=False,
        description="True when returned from an existing receipt rather than a fresh commit",
    )
    reconciled: bool = Field(
        default=False,
        description=(
            "True when the turn recovered this receipt from the database after the call "
            "that committed it failed to report back. Distinct from a duplicate submit: "
            "nobody asked twice, the confirmation was lost."
        ),
    )


class ActionDeniedPayload(_Base):
    reason: DenialReason
    explanation: str
    citations: list[Citation]


class EscalationCreatedPayload(_Base):
    ticket_ref: str
    intent_id: str
    reason: EscalationReason
    summary: str


class TurnMetricsPayload(_Base):
    """Feeds the observability drawer. Cost is always an estimate."""

    ttft_ms: int | None
    total_ms: int
    tool_calls: int
    tokens_in: int | None
    tokens_out: int | None
    cost_estimate_usd: float | None
    cost_rates_effective: str | None
    model: str


class TurnFinishedPayload(_Base):
    outcome: TurnOutcome
    message: str | None
    # Validated sources for the released answer. Every entry has been checked
    # against the accepted evidence, so the UI can render these as citations
    # without re-verifying.
    citations: list[Citation] = Field(default_factory=list)


class ErrorPayload(_Base):
    error_code: str
    message: str
    retryable: bool
    retry_after_s: float | None = None


# -------------------------------------------------------------------- events --


class _Envelope(_Base):
    version: int = EVENT_CONTRACT_VERSION
    session_id: UUID
    turn_id: UUID
    sequence: int = Field(ge=0)
    timestamp: datetime


class TurnStarted(_Envelope):
    type: Literal["turn.started"] = "turn.started"
    payload: TurnStartedPayload


class TranscriptPartial(_Envelope):
    type: Literal["transcript.partial"] = "transcript.partial"
    payload: TranscriptPayload


class TranscriptFinal(_Envelope):
    type: Literal["transcript.final"] = "transcript.final"
    payload: TranscriptPayload


class ToolStarted(_Envelope):
    type: Literal["tool.started"] = "tool.started"
    payload: ToolStartedPayload


class ToolCompleted(_Envelope):
    type: Literal["tool.completed"] = "tool.completed"
    payload: ToolCompletedPayload


class ToolFailed(_Envelope):
    type: Literal["tool.failed"] = "tool.failed"
    payload: ToolFailedPayload


class IdentityVerified(_Envelope):
    type: Literal["identity.verified"] = "identity.verified"
    payload: VerifiedIdentityPayload


class GroundingDecided(_Envelope):
    type: Literal["grounding.decided"] = "grounding.decided"
    payload: GroundingDecidedPayload


class AnswerDelta(_Envelope):
    type: Literal["answer.delta"] = "answer.delta"
    payload: AnswerDeltaPayload


class ActionCommitted(_Envelope):
    type: Literal["action.committed"] = "action.committed"
    payload: ActionCommittedPayload


class ActionDenied(_Envelope):
    type: Literal["action.denied"] = "action.denied"
    payload: ActionDeniedPayload


class EscalationCreated(_Envelope):
    type: Literal["escalation.created"] = "escalation.created"
    payload: EscalationCreatedPayload


class TurnMetrics(_Envelope):
    type: Literal["turn.metrics"] = "turn.metrics"
    payload: TurnMetricsPayload


class TurnFinished(_Envelope):
    type: Literal["turn.finished"] = "turn.finished"
    payload: TurnFinishedPayload


class StreamError(_Envelope):
    type: Literal["error"] = "error"
    payload: ErrorPayload


Event = Annotated[
    TurnStarted
    | TranscriptPartial
    | TranscriptFinal
    | ToolStarted
    | ToolCompleted
    | ToolFailed
    | IdentityVerified
    | GroundingDecided
    | AnswerDelta
    | ActionCommitted
    | ActionDenied
    | EscalationCreated
    | TurnMetrics
    | TurnFinished
    | StreamError,
    Field(discriminator="type"),
]


class EventStream(BaseModel):
    """Wrapper existing only so the union is reachable from JSON Schema export."""

    event: Event
