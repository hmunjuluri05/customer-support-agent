"""SQLAlchemy 2 models.

Money is always an integer in minor units (cents). There is no float anywhere
near a currency amount.

The uniqueness constraints are load-bearing, not decorative:
  * turns(session_id, client_turn_id) makes a replayed request join the
    existing turn instead of starting a second action.
  * refunds(intent_id) and escalation_tickets(intent_id) make a retry after a
    lost response return the original receipt rather than double-committing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from support.contracts.enums import (
    AgentState,
    ContractStatus,
    EscalationReason,
    GroundingDecision,
    MessageRole,
    Mode,
    Tier,
    ToolName,
    ToolStatus,
    TurnOutcome,
    TurnStatus,
)

# Embedding width for text-embedding-3-small. Asserted here so a model swap
# that changes dimensionality fails loudly at migration time.
EMBEDDING_DIM = 1536

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB}


def _enum(enum_cls: type, name: str) -> Enum:
    """Postgres native enum storing *values*, not member names."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )


def _pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ------------------------------------------------------------------ fixtures --


class DemoAccount(Base):
    """A seeded, clearly-labelled synthetic account. Never a real customer."""

    __tablename__ = "demo_accounts"

    id: Mapped[UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(128))
    customer_name: Mapped[str] = mapped_column(String(128))
    tier: Mapped[Tier] = mapped_column(_enum(Tier, "tier"))
    contract_status: Mapped[ContractStatus] = mapped_column(
        _enum(ContractStatus, "contract_status")
    )
    created_at: Mapped[datetime] = _now()

    invoices: Mapped[list[Invoice]] = relationship(back_populates="account")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[UUID] = _pk()
    account_id: Mapped[UUID] = mapped_column(ForeignKey("demo_accounts.id", ondelete="CASCADE"))
    external_ref: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(String(256))
    amount_minor: Mapped[int] = mapped_column(Integer)
    refunded_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    issued_at: Mapped[datetime] = _now()

    account: Mapped[DemoAccount] = relationship(back_populates="invoices")

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("refunded_minor >= 0", name="refunded_non_negative"),
        # The invariant that makes over-refunding impossible at the storage layer,
        # independently of any application check.
        CheckConstraint("refunded_minor <= amount_minor", name="refunded_within_amount"),
    )

    @property
    def refundable_minor(self) -> int:
        return self.amount_minor - self.refunded_minor


# ------------------------------------------------------------------ sessions --


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[UUID] = _pk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Which browser created this session.
    #
    # An opaque, long-lived value from an httpOnly cookie, so a visitor can be
    # offered their own earlier sessions -- one per demo account -- after
    # closing the browser. It is deliberately NOT the account: the account is a
    # public dropdown choice and proves nothing, so keying resumption on it
    # would hand one visitor another visitor's transcript. This is a secret the
    # browser holds; the account name is only the label shown against it.
    browser_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Bound by the UI sign-in control, server-side. A chat message claiming an
    # account or tier never sets this.
    demo_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("demo_accounts.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_state: Mapped[AgentState] = mapped_column(
        _enum(AgentState, "agent_state"), default=AgentState.INIT
    )
    injected_fault: Mapped[str | None] = mapped_column(String(48), nullable=True)

    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    account: Mapped[DemoAccount | None] = relationship()


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    client_turn_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[Mode] = mapped_column(_enum(Mode, "mode"))
    status: Mapped[TurnStatus] = mapped_column(
        _enum(TurnStatus, "turn_status"), default=TurnStatus.RUNNING
    )
    outcome: Mapped[TurnOutcome | None] = mapped_column(
        _enum(TurnOutcome, "turn_outcome"), nullable=True
    )

    # Hash of normalised input. A repeated client_turn_id carrying different
    # text is a client bug and is rejected rather than silently joined.
    input_hash: Mapped[str] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)

    # Retrieval result for this turn, as chunk keys and scores (never text).
    #
    # The chat loop keeps this in memory, because one chat turn is one request.
    # A voice turn is not: each function call from the realtime model arrives
    # as its own request, so the evidence gathered by retrieve_policy has to
    # outlive it or the grounding gate could never pass on the voice path.
    # See support.retrieval.persistence.
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "client_turn_id", name="uq_turns_session_client_turn"),
        Index("ix_turns_session_started", "session_id", "started_at"),
    )


class Message(Base):
    """Conversation history, shared by chat and voice."""

    __tablename__ = "messages"

    id: Mapped[UUID] = _pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"))
    mode: Mapped[Mode] = mapped_column(_enum(Mode, "mode"))
    content: Mapped[str] = mapped_column(Text)

    # An interrupted draft is kept for display but excluded from future model
    # context, so the model never continues from text the user cut off.
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)


# --------------------------------------------------------------------- tools --


class ToolRun(Base):
    """One tool invocation. Source of the drawer's waterfall."""

    __tablename__ = "tool_runs"

    id: Mapped[UUID] = _pk()
    turn_id: Mapped[UUID] = mapped_column(ForeignKey("turns.id", ondelete="CASCADE"))
    tool: Mapped[ToolName] = mapped_column(_enum(ToolName, "tool_name"))
    call_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[ToolStatus] = mapped_column(_enum(ToolStatus, "tool_status"))

    # Redacted. No verification codes, no raw account data.
    args_redacted: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = _now()
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("turn_id", "call_index", name="uq_tool_runs_turn_call"),)


class Refund(Base):
    """A durable mock refund receipt.

    Nothing may report a successful refund without a row here.
    """

    __tablename__ = "refunds"

    id: Mapped[UUID] = _pk()
    intent_id: Mapped[str] = mapped_column(String(80), unique=True)
    receipt_ref: Mapped[str] = mapped_column(String(32), unique=True)

    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"))

    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))

    # Which policy authorised this, captured at commit time so a later policy
    # revision cannot retroactively change the record.
    policy_document_key: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    cited_chunk_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = _now()

    __table_args__ = (CheckConstraint("amount_minor > 0", name="amount_positive"),)


class EscalationTicket(Base):
    __tablename__ = "escalation_tickets"

    id: Mapped[UUID] = _pk()
    intent_id: Mapped[str] = mapped_column(String(80), unique=True)
    ticket_ref: Mapped[str] = mapped_column(String(32), unique=True)

    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )

    reason: Mapped[EscalationReason] = mapped_column(_enum(EscalationReason, "escalation_reason"))
    summary: Mapped[str] = mapped_column(Text)
    citations: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _now()


# ----------------------------------------------------------------- retrieval --


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[UUID] = _pk()
    doc_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(192))
    version: Mapped[str] = mapped_column(String(32))

    # Superseded versions stay for outstanding receipts but are excluded from
    # live retrieval.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Empty list means global: retrievable regardless of verified tier.
    tiers: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)

    source_path: Mapped[str] = mapped_column(String(256))
    checksum: Mapped[str] = mapped_column(String(64))
    embedding_deployment: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()

    chunks: Mapped[list[PolicyChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("doc_key", "version", name="uq_policy_documents_key_version"),
        Index("ix_policy_documents_active", "is_active"),
    )


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"

    id: Mapped[UUID] = _pk()

    # Deterministic from (version, normalised text), so reindexing the same
    # corpus produces identical ids and citations stay stable.
    chunk_key: Mapped[str] = mapped_column(String(80), unique=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("policy_documents.id", ondelete="CASCADE"))

    heading_path: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))

    document: Mapped[PolicyDocument] = relationship(back_populates="chunks")

    # No HNSW index: the fixture corpus is a few hundred rows, where exact
    # cosine search with tier filters applied first is both faster and
    # perfectly recalled. Revisit only with measurements.
    __table_args__ = (Index("ix_policy_chunks_document", "document_id"),)


class TurnMetric(Base):
    __tablename__ = "turn_metrics"

    turn_id: Mapped[UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), primary_key=True
    )
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Labelled an estimate everywhere it surfaces; rates and their effective
    # date come from config, not from the provider response.
    cost_estimate_usd: Mapped[float | None] = mapped_column(nullable=True)
    cost_rates_effective: Mapped[str | None] = mapped_column(String(32), nullable=True)

    model: Mapped[str] = mapped_column(String(64))
    grounding_decision: Mapped[GroundingDecision | None] = mapped_column(
        _enum(GroundingDecision, "grounding_decision"), nullable=True
    )
    top_similarity: Mapped[float | None] = mapped_column(nullable=True)
    waterfall: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
