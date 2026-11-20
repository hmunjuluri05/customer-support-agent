import {
  DENIAL_LABELS,
  ESCALATION_LABELS,
  GROUNDING_LABELS,
  OUTCOME_LABELS,
  duration,
  money,
  outcomeTone,
  similarity,
  usd,
} from '@/lib/format';
import type { TurnView } from '@/lib/reducer';
import { Citations } from './Citations';
import { ToolTimeline } from './ToolTimeline';

interface Props {
  turn: TurnView;
  onInspect: (turnId: string) => void;
  /** Runs the same request again as a new turn. Only offered when that is safe. */
  onRetry: (text: string) => void;
  retryDisabled: boolean;
}

/** One exchange: what the customer said, and everything the agent did about it. */
export function TurnCard({ turn, onInspect, onRetry, retryDisabled }: Props) {
  const running = turn.status === 'pending' || turn.status === 'running';
  const hasAnswer = turn.answer.length > 0;
  const turnId = turn.turnId;

  // Citations on the released answer come from turn.finished; while the turn is
  // still running the retrieval set is the best available view of the evidence.
  const answerCitations =
    turn.citations.length > 0 ? turn.citations : (turn.grounding?.citations ?? []);

  // A denial renders its own "Policy basis", drawn from the same accepted
  // evidence. Listing those chunks again under "Sources" just prints the
  // identical five rows twice, so the generic block only shows what the
  // denial did not already account for.
  const denialKeys = new Set((turn.denied?.citations ?? []).map((c) => c.chunk_id));
  const sourceCitations = answerCitations.filter((c) => !denialKeys.has(c.chunk_id));

  return (
    <div className="turn">
      <div className="bubble user">
        <div className="bubble-meta">{turn.mode === 'voice' ? 'You · voice' : 'You'}</div>
        {turn.userText}
      </div>

      <div className="agent-card">
        {(turn.steps.length > 0 || turn.identity !== null) && (
          <div className="agent-section">
            <div className="section-label">Agent steps</div>
            <ToolTimeline steps={turn.steps} />
            {turn.identity !== null && (
              <div className="chips" style={{ marginTop: 8 }}>
                <span className="chip">{turn.identity.customer_name}</span>
                <span className="chip">{turn.identity.tier}</span>
                <span
                  className={`chip ${turn.identity.contract_status === 'active' ? 'good' : 'warn'}`}
                >
                  contract {turn.identity.contract_status}
                </span>
              </div>
            )}
          </div>
        )}

        {turn.grounding !== null && (
          <div className="agent-section">
            <div className="section-label">Grounding</div>
            <div className="chips">
              <span
                className={`chip ${
                  turn.grounding.decision === 'grounded'
                    ? 'good'
                    : turn.grounding.decision === 'weak'
                      ? 'warn'
                      : 'bad'
                }`}
              >
                {GROUNDING_LABELS[turn.grounding.decision]}
              </span>
              <span className="chip mono">
                top {similarity(turn.grounding.top_similarity)} vs τ{' '}
                {turn.grounding.threshold.toFixed(2)}
              </span>
              <span className="chip mono">
                {turn.grounding.accepted_chunks} chunk
                {turn.grounding.accepted_chunks === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        )}

        {(hasAnswer || running) && (
          <div className="agent-section">
            <div className="section-label">Response</div>
            <div className={`answer${hasAnswer ? '' : ' pending'}`}>
              {hasAnswer ? turn.answer : 'Working on it'}
              {running && <span className="caret" />}
            </div>
          </div>
        )}

        <Citations citations={sourceCitations} label="Sources" />

        {turn.committed !== null && (
          <div className="agent-section">
            <div className="receipt">
              <div className="receipt-head">
                <strong>Refund issued</strong>
                <span className="receipt-ref">{turn.committed.receipt_ref}</span>
                {/* Two different stories, and conflating them would mislead:
                    one means somebody asked twice, the other means the
                    confirmation was lost and the server went and checked. */}
                {turn.committed.reconciled ? (
                  <span className="chip warn">recovered after a failed call</span>
                ) : (
                  turn.committed.idempotent_replay && (
                    <span className="chip">replayed, not re-charged</span>
                  )
                )}
              </div>
              <dl className="kv">
                <dt>Amount</dt>
                <dd>{money(turn.committed.amount_minor, turn.committed.currency)}</dd>
                <dt>Invoice</dt>
                <dd>{turn.committed.invoice_ref}</dd>
                <dt>Policy</dt>
                <dd>{turn.committed.policy_version}</dd>
                <dt>Intent</dt>
                <dd>{turn.committed.intent_id}</dd>
              </dl>
            </div>
          </div>
        )}

        {turn.denied !== null && (
          <div className="agent-section">
            <div className="receipt denied">
              <div className="receipt-head">
                <strong>Refund denied</strong>
                <span className="chip warn">{DENIAL_LABELS[turn.denied.reason]}</span>
              </div>
              <div>{turn.denied.explanation}</div>
            </div>
            {turn.denied.citations.length > 0 && (
              <Citations citations={turn.denied.citations} label="Policy basis" />
            )}
          </div>
        )}

        {turn.escalation !== null && (
          <div className="agent-section">
            <div className="receipt escalated">
              <div className="receipt-head">
                <strong>Escalated to a human</strong>
                <span className="receipt-ref">{turn.escalation.ticket_ref}</span>
                <span className="chip">{ESCALATION_LABELS[turn.escalation.reason]}</span>
              </div>
              <div>{turn.escalation.summary}</div>
            </div>
          </div>
        )}

        {turn.error !== null && (
          <div className="agent-section">
            <div className="error-box">
              <strong>{turn.error.errorCode}</strong>
              <div>{turn.error.message}</div>
              {turn.error.retryAfterS !== null && (
                <div className="hint">
                  The service asked us to wait {turn.error.retryAfterS.toFixed(0)}s.
                </div>
              )}
              {/* Only when the server said retrying is worth it, and only
                  when nothing committed. A refund that went through is
                  reported above as a receipt, and re-sending the request
                  would be asking for a second one. */}
              {turn.error.retryable && turn.committed === null && turn.userText.length > 0 && (
                <button
                  type="button"
                  className="btn small"
                  style={{ marginTop: 8 }}
                  disabled={retryDisabled}
                  onClick={() => onRetry(turn.userText)}
                >
                  Try again
                </button>
              )}
            </div>
          </div>
        )}

        {(turn.outcome !== null ||
          turn.metrics !== null ||
          turn.outOfOrder ||
          (turnId !== null && !running)) && (
          <div className="agent-section">
            <div className="chips" style={{ marginBottom: turn.metrics !== null ? 8 : 0 }}>
              {turn.outcome !== null && (
                <span className={`chip ${outcomeTone(turn.outcome)}`}>
                  {OUTCOME_LABELS[turn.outcome]}
                </span>
              )}
              {turn.status === 'cancelled' && <span className="chip warn">stopped by you</span>}
              {turn.outOfOrder && (
                <span className="chip bad" title="A frame arrived with a non-monotonic sequence">
                  out-of-order frame dropped
                </span>
              )}
              <span className="spacer" />
              {/* Needs the server's turn id, which only exists once the first
                  frame has arrived: the trace is read from the database, not
                  from what this tab happened to receive. */}
              {turnId !== null && (
                <button type="button" className="btn small" onClick={() => onInspect(turnId)}>
                  Inspect
                </button>
              )}
            </div>
            {turn.metrics !== null && (
              <div className="metrics-line">
                <span>total {duration(turn.metrics.total_ms)}</span>
                <span>ttft {duration(turn.metrics.ttft_ms)}</span>
                <span>tools {turn.metrics.tool_calls}</span>
                <span>
                  tokens {turn.metrics.tokens_in ?? '--'}/{turn.metrics.tokens_out ?? '--'}
                </span>
                <span title="Estimated from configured rates, not billed usage">
                  ~{usd(turn.metrics.cost_estimate_usd)}
                </span>
                <span>{turn.metrics.model}</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
