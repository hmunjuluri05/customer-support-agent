import { TOOL_LABELS, TOOL_RUNNING_LABELS, duration } from '@/lib/format';
import type { StepView } from '@/lib/reducer';

/**
 * Live tool-call status.
 *
 * Each row is one executor call, keyed by call_index so the completion lands
 * on the row that started rather than appending a second one. The summary text
 * comes from the server and is already redacted -- no raw arguments and no
 * verification code ever reaches the browser.
 */
export function ToolTimeline({ steps }: { steps: StepView[] }) {
  if (steps.length === 0) return null;

  return (
    <ol className="steps" aria-label="Tool calls">
      {steps.map((step) => (
        <li key={step.callIndex} className={`step ${step.status}`}>
          <span className="step-icon" aria-hidden="true">
            {step.status === 'running' ? (
              <span className="spinner" />
            ) : step.status === 'completed' ? (
              <span className="tick">✓</span>
            ) : (
              <span className="cross">✕</span>
            )}
          </span>
          <span className="step-text">
            <strong>
              {step.status === 'running'
                ? `${TOOL_RUNNING_LABELS[step.tool]}…`
                : TOOL_LABELS[step.tool]}
            </strong>
            {/* While running, the server's summary is just the starting text,
                which the label above already says. Showing it would print the
                same sentence twice. */}
            {step.status !== 'running' && step.summary.length > 0 && (
              <div className="step-summary">{step.summary}</div>
            )}
            {step.errorCode !== null && (
              <div className="step-summary">
                <span className="cross">{step.errorCode}</span>
                {step.retryable ? ' · retryable' : ' · not retryable'}
              </div>
            )}
          </span>
          <span className="step-duration">
            {step.status === 'running' ? '' : duration(step.durationMs)}
          </span>
        </li>
      ))}
    </ol>
  );
}
