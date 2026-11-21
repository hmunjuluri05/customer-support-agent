'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

interface Props {
  /** Reset is a demo fixture control and is hidden when demo mode is off. */
  demoMode: boolean;
  /** A turn is in flight, or a fixture call is already running. */
  busy: boolean;
  onClearChat: () => void;
  onResetDemo: () => void;
}

/**
 * The two destructive controls, behind a gear in the top bar.
 *
 * They were in the right-hand rail, which is the fourth card down on a laptop
 * and `display: none` under 900px -- findable only by scrolling a panel most
 * people never scroll. Promoting them to plain top-bar buttons fixed that and
 * created the opposite problem: "wipe every conversation" sat one stray click
 * away from the thing you were typing into. A menu is the middle ground --
 * always reachable, never adjacent to anything.
 *
 * Reset asks twice. It spans every session and restores invoice balances, so
 * the cost of an accidental click is the whole transcript you were recording.
 * Clear chat does not: it is scoped to the conversation in front of you, and
 * receipts survive it.
 */
export function SettingsMenu({ demoMode, busy, onClearChat, onResetDemo }: Props) {
  const [open, setOpen] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setConfirmingReset(false);
  }, []);

  // Dismiss on click-outside and on Escape. Bound only while open, so the
  // closed menu costs nothing.
  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };

    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [close, open]);

  return (
    <div className="menu-wrap" ref={wrap}>
      <button
        type="button"
        className={`icon-btn ${open ? 'active' : ''}`}
        aria-label="Settings"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <GearIcon />
      </button>

      {open && (
        <div className="menu" role="menu">
          <button
            type="button"
            role="menuitem"
            className="menu-item"
            disabled={busy}
            onClick={() => {
              onClearChat();
              close();
            }}
          >
            <span className="menu-label">Clear chat</span>
            <span className="menu-hint">
              Deletes this conversation. You stay signed in, and refund receipts and invoice
              balances are kept.
            </span>
          </button>

          {demoMode && (
            <>
              <div className="menu-sep" />

              {confirmingReset ? (
                <>
                  <button
                    type="button"
                    role="menuitem"
                    className="menu-item danger"
                    disabled={busy}
                    onClick={() => {
                      onResetDemo();
                      close();
                    }}
                  >
                    <span className="menu-label">Yes, reset everything</span>
                    <span className="menu-hint">
                      This clears the conversation you are in as well. Not something to click
                      mid-recording.
                    </span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className="menu-item"
                    onClick={() => setConfirmingReset(false)}
                  >
                    <span className="menu-label">Cancel</span>
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  disabled={busy}
                  onClick={() => setConfirmingReset(true)}
                >
                  <span className="menu-label">Reset sample data</span>
                  <span className="menu-hint">
                    Clears every conversation and puts the invoice balances back. Accounts and the
                    policy corpus are untouched.
                  </span>
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function GearIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
