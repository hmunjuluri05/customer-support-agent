'use client';

import { useState } from 'react';

import type { FaultKind } from '@/lib/events';

const FAULTS: { kind: FaultKind; label: string; effect: string }[] = [
  {
    kind: 'tool_error',
    label: 'Tool failure',
    effect: 'A downstream call fails after identity is verified. Nothing commits.',
  },
  {
    kind: 'retrieval_unavailable',
    label: 'Retrieval down',
    effect: 'The policy library is unreachable, so no policy claim and no action.',
  },
  {
    kind: 'llm_timeout',
    label: 'Model timeout',
    effect: 'Every model call times out. Retried twice, then reported.',
  },
  {
    kind: 'rate_limit',
    label: 'Model rate limit',
    effect: 'The provider returns 429 with a Retry-After the UI shows you.',
  },
  {
    kind: 'action_timeout_after_commit',
    label: 'Lost confirmation',
    effect: 'The refund commits, then the call fails. The turn recovers the receipt.',
  },
];

interface Props {
  armed: FaultKind;
  busy: boolean;
  onArm: (fault: FaultKind) => void;
  onReset: () => void;
  resetNote: string | null;
}

/**
 * Demo controls.
 *
 * Faults are armed on the caller's own session, server-side — one visitor
 * cannot break another's demo. Each entry says what it will do, because a
 * control that produces an unexplained failure looks like a bug rather than a
 * demonstration.
 */
export function DemoPanel({ armed, busy, onArm, onReset, resetNote }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rail-card">
      <h2>Sample data</h2>

      {/* Outside the collapsible section on purpose: this is the control
          someone reaches for between runs, and it was invisible behind a
          "show" toggle labelled "Break something" -- a different concern
          entirely. */}
      <div className="row-gap">
        <button type="button" className="btn small" disabled={busy} onClick={onReset}>
          Reset sample data
        </button>
      </div>
      <div className="hint">
        Clears every conversation and restores the invoice balances. Accounts, invoices and the
        policy corpus are untouched, and you stay signed in.
      </div>
      {resetNote !== null && <div className="hint">{resetNote}</div>}

      <h2 style={{ marginTop: 18 }}>
        Break something
        <button
          type="button"
          className="link-btn"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? 'hide' : 'show'}
        </button>
      </h2>

      {armed !== 'none' && (
        <div className="chips" style={{ marginBottom: 8 }}>
          <span className="chip bad">armed: {armed.replace(/_/g, ' ')}</span>
        </div>
      )}

      {open && (
        <>
          <div className="faults">
            {FAULTS.map((fault) => (
              <button
                type="button"
                key={fault.kind}
                className={`suggestion ${armed === fault.kind ? 'active' : ''}`}
                disabled={busy}
                onClick={() => onArm(armed === fault.kind ? 'none' : fault.kind)}
              >
                <strong style={{ display: 'block' }}>{fault.label}</strong>
                {fault.effect}
              </button>
            ))}
          </div>

          <div className="row-gap">
            <button
              type="button"
              className="btn small"
              disabled={busy || armed === 'none'}
              onClick={() => onArm('none')}
            >
              Clear fault
            </button>
          </div>

          <div className="hint">
            The fault applies to this session only, and stays armed until cleared.
          </div>
        </>
      )}
    </div>
  );
}
