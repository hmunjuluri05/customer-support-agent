/** Display helpers. Kept out of components so the labels have one home. */

import type {
  DenialReason,
  EscalationReason,
  GroundingDecision,
  ToolName,
  TurnOutcome,
} from './events';

/** Amounts cross the wire as integer minor units; only this function divides. */
export function money(amountMinor: number, currency = 'USD'): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(
    amountMinor / 100,
  );
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '--';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function similarity(value: number | null | undefined): string {
  return value === null || value === undefined ? '--' : value.toFixed(3);
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '--';
  // Per-turn cost is fractions of a cent; two decimals would round it to $0.00.
  return `$${value.toFixed(5)}`;
}

export const TOOL_LABELS: Record<ToolName, string> = {
  verify_identity: 'Verify identity',
  retrieve_policy: 'Search policy library',
  execute_mock_refund: 'Issue refund',
  create_escalation_ticket: 'Escalate to a human',
};

export const TOOL_RUNNING_LABELS: Record<ToolName, string> = {
  verify_identity: 'Verifying account',
  retrieve_policy: 'Searching policy library',
  execute_mock_refund: 'Issuing refund',
  create_escalation_ticket: 'Creating escalation ticket',
};

export const GROUNDING_LABELS: Record<GroundingDecision, string> = {
  grounded: 'Grounded',
  weak: 'Weak evidence',
  unsupported: 'Unsupported',
};

export const OUTCOME_LABELS: Record<TurnOutcome, string> = {
  grounded_answer: 'Answered from policy',
  clarification: 'Clarification requested',
  policy_denial: 'Denied by policy',
  action_success: 'Action completed',
  escalation: 'Escalated',
  cancelled: 'Cancelled',
  recoverable_failure: 'Recoverable failure',
  budget_exhausted: 'Budget exhausted',
};

export const DENIAL_LABELS: Record<DenialReason, string> = {
  exceeds_tier_cap: 'Exceeds the tier refund cap',
  contract_inactive: 'Contract is not active',
  invoice_not_owned: 'Invoice belongs to another account',
  insufficient_refundable_balance: 'Not enough refundable balance left',
  currency_mismatch: 'Currency mismatch',
  ineligible_reason: 'Reason is not refund-eligible',
  invalid_arguments: 'Invalid request arguments',
  not_verified: 'Identity not verified',
  not_grounded: 'No grounded policy evidence',
};

export const ESCALATION_LABELS: Record<EscalationReason, string> = {
  unsupported_topic: 'Topic outside the policy library',
  conflicting_policy: 'Conflicting policy clauses',
  insufficient_evidence: 'Insufficient evidence',
  action_blocked: 'Action blocked',
  user_requested: 'Customer asked for a human',
  tool_failure: 'Tool failure',
};

/** The kind of banner an outcome deserves. */
export function outcomeTone(outcome: TurnOutcome | null): 'good' | 'warn' | 'bad' | 'muted' {
  switch (outcome) {
    case 'action_success':
    case 'grounded_answer':
      return 'good';
    case 'policy_denial':
    case 'clarification':
    case 'escalation':
      return 'warn';
    case 'recoverable_failure':
    case 'budget_exhausted':
      return 'bad';
    default:
      return 'muted';
  }
}
