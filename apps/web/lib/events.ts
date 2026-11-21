/**
 * The stream event contract, mirroring support.contracts.events.
 *
 * Hand-written rather than code-generated, because a generated discriminated
 * union from Pydantic's JSON Schema needs post-processing before it is
 * ergonomic. Drift is caught by apps/api/tests/test_contract_parity.py, which
 * compares the `type` literals and enum members in this file against the
 * Python definitions and fails if either side gains a member the other lacks.
 */

export const EVENT_CONTRACT_VERSION = 1;

export type Tier = 'basic' | 'premium';
export type ContractStatus = 'active' | 'inactive';
export type Mode = 'chat' | 'voice';

export type ToolName =
  | 'verify_identity'
  | 'retrieve_policy'
  | 'execute_mock_refund'
  | 'create_escalation_ticket';

export type GroundingDecision = 'grounded' | 'weak' | 'unsupported';

export type TurnOutcome =
  | 'grounded_answer'
  | 'clarification'
  | 'policy_denial'
  | 'action_success'
  | 'escalation'
  | 'cancelled'
  | 'recoverable_failure'
  | 'budget_exhausted';

export type DenialReason =
  | 'exceeds_tier_cap'
  | 'contract_inactive'
  | 'invoice_not_owned'
  | 'insufficient_refundable_balance'
  | 'currency_mismatch'
  | 'ineligible_reason'
  | 'invalid_arguments'
  | 'not_verified'
  | 'not_grounded';

export type EscalationReason =
  | 'unsupported_topic'
  | 'conflicting_policy'
  | 'insufficient_evidence'
  | 'action_blocked'
  | 'user_requested'
  | 'tool_failure';

export type FaultKind =
  | 'none'
  | 'tool_error'
  | 'llm_timeout'
  | 'rate_limit'
  | 'retrieval_unavailable'
  | 'action_timeout_after_commit';

export interface Citation {
  chunk_id: string;
  document_title: string;
  document_version: string;
  heading_path: string;
  similarity: number;
}

interface Envelope {
  version: number;
  session_id: string;
  turn_id: string;
  sequence: number;
  timestamp: string;
}

export interface TurnStarted extends Envelope {
  type: 'turn.started';
  payload: { mode: string; client_turn_id: string };
}

export interface TranscriptPartial extends Envelope {
  type: 'transcript.partial';
  payload: { text: string; is_final: boolean };
}

export interface TranscriptFinal extends Envelope {
  type: 'transcript.final';
  payload: { text: string; is_final: boolean };
}

export interface ToolStarted extends Envelope {
  type: 'tool.started';
  payload: { tool: ToolName; call_index: number; summary: string };
}

export interface ToolCompleted extends Envelope {
  type: 'tool.completed';
  payload: { tool: ToolName; call_index: number; duration_ms: number; summary: string };
}

export interface ToolFailed extends Envelope {
  type: 'tool.failed';
  payload: {
    tool: ToolName;
    call_index: number;
    duration_ms: number;
    error_code: string;
    retryable: boolean;
    summary: string;
  };
}

export interface IdentityVerified extends Envelope {
  type: 'identity.verified';
  payload: { customer_name: string; tier: Tier; contract_status: ContractStatus };
}

export interface GroundingDecided extends Envelope {
  type: 'grounding.decided';
  payload: {
    decision: GroundingDecision;
    threshold: number;
    top_similarity: number | null;
    accepted_chunks: number;
    citations: Citation[];
  };
}

export interface AnswerDelta extends Envelope {
  type: 'answer.delta';
  payload: { text: string };
}

export interface ActionCommitted extends Envelope {
  type: 'action.committed';
  payload: {
    receipt_ref: string;
    intent_id: string;
    invoice_ref: string;
    amount_minor: number;
    currency: string;
    policy_version: string;
    idempotent_replay: boolean;
    /** Recovered from the database after the call that committed it failed to report back. */
    reconciled: boolean;
  };
}

export interface ActionDenied extends Envelope {
  type: 'action.denied';
  payload: { reason: DenialReason; explanation: string; citations: Citation[] };
}

export interface EscalationCreated extends Envelope {
  type: 'escalation.created';
  payload: { ticket_ref: string; intent_id: string; reason: EscalationReason; summary: string };
}

export interface TurnMetrics extends Envelope {
  type: 'turn.metrics';
  payload: {
    ttft_ms: number | null;
    total_ms: number;
    tool_calls: number;
    tokens_in: number | null;
    tokens_out: number | null;
    cost_estimate_usd: number | null;
    cost_rates_effective: string | null;
    model: string;
  };
}

export interface TurnFinished extends Envelope {
  type: 'turn.finished';
  payload: { outcome: TurnOutcome; message: string | null; citations: Citation[] };
}

export interface StreamError extends Envelope {
  type: 'error';
  payload: {
    error_code: string;
    message: string;
    retryable: boolean;
    retry_after_s: number | null;
  };
}

export type StreamEvent =
  | TurnStarted
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
  | StreamError;

export type StreamEventType = StreamEvent['type'];

/** Every member of StreamEventType, for runtime validation of unknown frames. */
export const EVENT_TYPES = [
  'turn.started',
  'transcript.partial',
  'transcript.final',
  'tool.started',
  'tool.completed',
  'tool.failed',
  'identity.verified',
  'grounding.decided',
  'answer.delta',
  'action.committed',
  'action.denied',
  'escalation.created',
  'turn.metrics',
  'turn.finished',
  'error',
] as const satisfies readonly StreamEventType[];

const KNOWN: ReadonlySet<string> = new Set<string>(EVENT_TYPES);

export function isStreamEvent(value: unknown): value is StreamEvent {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as { type?: unknown };
  return typeof candidate.type === 'string' && KNOWN.has(candidate.type);
}

/** A turn is over when this returns true. Exactly one such event per turn. */
export function isTerminal(event: StreamEvent): boolean {
  return event.type === 'turn.finished';
}

// ------------------------------------------------------------ REST shapes --

export interface DemoInvoice {
  ref: string;
  description: string;
  amount_minor: number;
  refunded_minor: number;
  currency: string;
}

export interface DemoAccount {
  slug: string;
  label: string;
  customer_name: string;
  tier: Tier;
  contract_status: ContractStatus;
  invoices: DemoInvoice[];
}

export interface SessionAccount {
  slug: string;
  label: string;
  tier: Tier;
  contract_status: ContractStatus;
}

export interface CreatedSession {
  session_id: string;
  session_token: string;
  account: SessionAccount | null;
  expires_at: string;
}

export interface SnapshotMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  mode: Mode;
  content: string;
  interrupted: boolean;
  created_at: string;
}

export interface SessionSnapshot {
  session_id: string;
  account: SessionAccount | null;
  active_turn_id: string | null;
  messages: SnapshotMessage[];
  receipts: {
    receipt_ref: string;
    amount_minor: number;
    currency: string;
    policy_version: string;
    created_at: string;
  }[];
  tickets: {
    ticket_ref: string;
    reason: EscalationReason;
    summary: string;
    created_at: string;
  }[];
}
