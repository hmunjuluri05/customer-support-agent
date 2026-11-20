'use client';

import { useEffect, useRef } from 'react';

import type { TraceMetrics, TurnTrace, WaterfallStep } from '@/lib/client';
import {
  GROUNDING_LABELS,
  OUTCOME_LABELS,
  TOOL_LABELS,
  duration,
  outcomeTone,
  similarity,
  usd,
} from '@/lib/format';

interface Props {
  trace: TurnTrace | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRetry: () => void;
}

/**
 * Per-turn observability.
 *
 * Everything here is read back from the server's own audit rows rather than
 * from the browser's event log, so the drawer shows what was *recorded* about
 * a turn, not what this tab happened to receive. That distinction is the point
 * of the panel: if a frame was dropped on the way to the UI, the drawer still
 * shows the tool call that ran.
 */
export function TraceDrawer({ trace, loading, error, onClose, onRetry }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Turn trace"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer-head">
          <div>
            <div className="section-label">Turn trace</div>
            <div className="drawer-title">
              {trace === null ? 'Loading…' : <span className="mono">{trace.turn_id}</span>}
            </div>
          </div>
          <button type="button" className="btn small" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </header>

        <div className="drawer-body">
          {loading && <div className="center-note">Reading the trace…</div>}

          {error !== null && (
            <div className="error-box">
              <div>{error}</div>
              <button type="button" className="btn small" style={{ marginTop: 8 }} onClick={onRetry}>
                Try again
              </button>
            </div>
          )}

          {trace !== null && !loading && <TraceBody trace={trace} />}
        </div>
      </aside>
    </div>
  );
}

function TraceBody({ trace }: { trace: TurnTrace }) {
  const metrics = trace.metrics;

  return (
    <>
      <div className="drawer-section">
        <div className="chips">
          <span className="chip">{trace.mode === 'voice' ? 'voice' : 'chat'}</span>
          <span className="chip">{trace.status}</span>
          {trace.outcome !== null && (
            <span className={`chip ${outcomeTone(trace.outcome)}`}>
              {OUTCOME_LABELS[trace.outcome]}
            </span>
          )}
        </div>
      </div>

      {metrics === null ? (
        <div className="drawer-section">
          <div className="section-label">Timing</div>
          <div className="hint">
            No metric row for this turn. A turn that was cancelled or failed before it finished
            never wrote one — the tool calls below are still the real audit rows.
          </div>
        </div>
      ) : (
        <>
          <div className="drawer-section">
            <div className="section-label">Timing</div>
            <Waterfall metrics={metrics} mode={trace.mode} />
          </div>

          <div className="drawer-section">
            <div className="section-label">Grounding</div>
            <GroundingScale metrics={metrics} />
          </div>

          <div className="drawer-section">
            <div className="section-label">Tokens and cost</div>
            <dl className="kv">
              <dt>Model</dt>
              <dd className="mono">{metrics.model ?? '--'}</dd>
              <dt>Tokens in</dt>
              <dd>{metrics.tokens_in ?? 'not reported'}</dd>
              <dt>Tokens out</dt>
              <dd>{metrics.tokens_out ?? 'not reported'}</dd>
              <dt>Cost</dt>
              <dd>
                {metrics.cost_estimate_usd === null ? (
                  'not estimated'
                ) : (
                  <>
                    ~{usd(metrics.cost_estimate_usd)}
                    <span className="hint-inline"> estimate</span>
                  </>
                )}
              </dd>
            </dl>
            <div className="hint">
              {metrics.cost_estimate_usd === null
                ? trace.mode === 'voice'
                  ? 'The realtime session reports token usage over the audio data channel to the browser, not to this server. There is nothing measured here to price, so the field is blank rather than $0.00.'
                  : 'No usage was reported for this turn, so there is nothing to price.'
                : `Priced from rates in configuration, effective ${metrics.cost_rates_effective ?? 'unknown'} — not from billed usage. Treat it as an order of magnitude.`}
            </div>
          </div>
        </>
      )}

      <div className="drawer-section">
        <div className="section-label">Tool calls ({trace.tool_runs.length})</div>
        {trace.tool_runs.length === 0 ? (
          <div className="hint">No tool calls on this turn.</div>
        ) : (
          <div className="runs">
            {trace.tool_runs.map((run) => (
              <div className={`run ${run.status}`} key={run.call_index}>
                <div className="run-head">
                  <span className="run-index mono">#{run.call_index}</span>
                  <strong>{TOOL_LABELS[run.tool]}</strong>
                  <span className={`chip ${run.status === 'completed' ? 'good' : 'bad'}`}>
                    {run.status}
                  </span>
                  <span className="spacer" />
                  <span className="run-duration">{duration(run.duration_ms)}</span>
                </div>
                {run.error_code !== null && (
                  <div className="run-error mono">{run.error_code}</div>
                )}
                <KeyValues label="args (redacted)" value={run.args} />
                <KeyValues label="result" value={run.result} />
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

/**
 * Tool calls laid out on the turn's clock.
 *
 * The gaps matter more than the bars: everything between two tool calls is the
 * model deciding what to do next, and on this agent that is usually the larger
 * share of the turn. A stacked bar of tool durations would quietly blame the
 * tools for latency they did not cause, so the remainder is drawn as its own
 * row and named.
 */
function Waterfall({ metrics, mode }: { metrics: TraceMetrics; mode: TurnTrace['mode'] }) {
  const total = metrics.total_ms;
  const steps = metrics.waterfall?.steps ?? [];
  const positioned = steps.every((step) => typeof step.start_ms === 'number');

  // Without recorded offsets the only honest layout is end to end, which is
  // what a turn recorded before offsets existed gets — flagged, not faked.
  const laidOut = withOffsets(steps, positioned);
  const toolMs = steps.reduce((sum, step) => sum + (step.ms ?? 0), 0);
  const otherMs = total === null ? null : Math.max(total - toolMs, 0);

  return (
    <>
      <dl className="kv">
        <dt>{mode === 'voice' ? 'Exchange span' : 'Total'}</dt>
        <dd>{duration(total)}</dd>
        <dt>{mode === 'voice' ? 'To first tool call' : 'To first token or tool'}</dt>
        <dd>{duration(metrics.ttft_ms)}</dd>
        <dt>Tool calls</dt>
        <dd>{metrics.tool_calls ?? steps.length}</dd>
      </dl>

      {mode === 'voice' && (
        <div className="hint">
          A spoken turn&rsquo;s span is measured from the turn row opening to the agent&rsquo;s reply
          landing, so it includes the time the customer spent talking. It is not comparable to a
          typed turn&rsquo;s total.
        </div>
      )}

      {total !== null && total > 0 && laidOut.length > 0 && (
        <div className="waterfall-rows">
          {laidOut.map((step) => (
            <div className="wf-row" key={`${step.call_index ?? step.tool}-${step.start}`}>
              <span className="wf-label">{TOOL_LABELS[step.tool]}</span>
              <span className="wf-track">
                <span
                  className={`wf-bar ${step.status === 'completed' ? 'ok' : 'bad'}`}
                  style={{
                    left: `${(step.start / total) * 100}%`,
                    width: `${Math.max(((step.ms ?? 0) / total) * 100, 1.5)}%`,
                  }}
                />
              </span>
              <span className="wf-ms">{duration(step.ms)}</span>
            </div>
          ))}

          {otherMs !== null && (
            <div className="wf-row">
              <span className="wf-label dim">model and overhead</span>
              <span className="wf-track">
                <span
                  className="wf-bar model"
                  style={{ left: '0%', width: `${(otherMs / total) * 100}%` }}
                />
              </span>
              <span className="wf-ms">{duration(otherMs)}</span>
            </div>
          )}
        </div>
      )}

      <div className="hint">
        {positioned
          ? 'Bars are placed on the turn clock; the space between them is model latency. The last row is the turn total minus time spent inside tools, drawn from the left because it is a sum, not an interval.'
          : 'This turn was recorded before per-call offsets were stored, so the bars are laid end to end and their positions are not measured.'}
      </div>
    </>
  );
}

interface LaidOutStep extends WaterfallStep {
  start: number;
}

function withOffsets(steps: WaterfallStep[], positioned: boolean): LaidOutStep[] {
  if (positioned) {
    return steps.map((step) => ({ ...step, start: step.start_ms ?? 0 }));
  }
  let cursor = 0;
  return steps.map((step) => {
    const laid = { ...step, start: cursor };
    cursor += step.ms ?? 0;
    return laid;
  });
}

/**
 * Where this turn's best match fell against the threshold.
 *
 * The bands are the actual decision rule, not a decoration: at or above tau is
 * GROUNDED and may back an action, the margin below it is WEAK and asks a
 * clarifying question, and below that is UNSUPPORTED. Drawing them makes a
 * refusal legible as arithmetic rather than as the model's mood.
 */
function GroundingScale({ metrics }: { metrics: TraceMetrics }) {
  const tau = metrics.grounding_threshold;
  const weakFloor = Math.max(tau - metrics.grounding_weak_margin, 0);
  const top = metrics.top_similarity;

  if (metrics.grounding_decision === null) {
    return <div className="hint">No retrieval ran on this turn, so there is no decision to show.</div>;
  }

  const tone =
    metrics.grounding_decision === 'grounded'
      ? 'good'
      : metrics.grounding_decision === 'weak'
        ? 'warn'
        : 'bad';

  return (
    <>
      <div className="chips">
        <span className={`chip ${tone}`}>{GROUNDING_LABELS[metrics.grounding_decision]}</span>
        <span className="chip mono">top {similarity(top)}</span>
        <span className="chip mono">τ {tau.toFixed(2)}</span>
        <span className="chip mono">margin {metrics.grounding_weak_margin.toFixed(2)}</span>
      </div>

      <div className="scale" aria-hidden="true">
        <span className="scale-band bad" style={{ width: `${weakFloor * 100}%` }} />
        <span className="scale-band warn" style={{ width: `${(tau - weakFloor) * 100}%` }} />
        <span className="scale-band good" style={{ width: `${(1 - tau) * 100}%` }} />
        {top !== null && (
          <span className="scale-marker" style={{ left: `${Math.min(Math.max(top, 0), 1) * 100}%` }} />
        )}
      </div>
      <div className="scale-axis">
        <span>0.00</span>
        <span>similarity = 1 − cosine distance</span>
        <span>1.00</span>
      </div>

      {!metrics.grounding_threshold_recorded && (
        <div className="hint">
          This turn predates per-turn threshold recording, so τ above is the value currently
          configured, not necessarily the one this decision was taken under.
        </div>
      )}
    </>
  );
}

/** Redacted args and result summaries, printed as they were stored. */
function KeyValues({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown> | string | null;
}) {
  if (value === null) return null;
  if (typeof value === 'string') {
    return value.length === 0 ? null : (
      <div className="run-kv">
        <span className="run-kv-label">{label}</span>
        <span className="mono">{value}</span>
      </div>
    );
  }

  // `summary` first: it is the one field written to be read by a person. The
  // rest follow in insertion order.
  const entries = Object.entries(value).sort(
    ([a], [b]) => Number(b === 'summary') - Number(a === 'summary'),
  );
  if (entries.length === 0) return null;

  return (
    <div className="run-kv">
      <span className="run-kv-label">{label}</span>
      <span className="mono">
        {entries.map(([key, item]) => (
          <span className="run-pair" key={key}>
            {key}={format(item)}
          </span>
        ))}
      </span>
    </div>
  );
}

/**
 * Scalars in full, containers by their size.
 *
 * A retrieval result carries its five citations, and printing them here would
 * bury the fields that are actually diagnostic -- the decision, the top score,
 * the threshold -- under a wall of JSON. The citations already render in the
 * turn card with their titles and scores, so this says how many there were.
 */
function format(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? '' : 's'}`;
  if (typeof value === 'object') {
    const keys = Object.keys(value);
    return `{${keys.length} field${keys.length === 1 ? '' : 's'}}`;
  }
  return String(value);
}
