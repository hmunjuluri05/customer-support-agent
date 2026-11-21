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
}

/**
 * Fault injection.
 *
 * Faults are armed on the caller's own session, server-side — one visitor
 * cannot break another's demo. Each entry says what it will do, because a
 * control that produces an unexplained failure looks like a bug rather than a
 * demonstration.
 *
 * "Reset sample data" used to live here as well, and does not any more: it
 * sits with "Clear chat" under the gear in the top bar. Arming a fault and
 * putting everything back are not the same kind of act, and the one that wipes
 * every conversation should not be a near-miss of the one that breaks a tool.
 *
 * The armed chip renders whether or not the list is expanded. A fault left on
 * from an earlier run is the most confusing state this panel can produce, so
 * it is never hidden behind the toggle.
 */
export function DemoPanel({ armed, busy, onArm }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rail-card">
      <h2>
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
