'use client';

import { useEffect, useRef, useState } from 'react';

interface Props {
  disabled: boolean;
  running: boolean;
  draft: string;
  onDraftChange: (value: string) => void;
  onSubmit: (text: string) => void;
  onCancel: () => void;
}

export function Composer({
  disabled,
  running,
  draft,
  onDraftChange,
  onSubmit,
  onCancel,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [composing, setComposing] = useState(false);

  // Grow with the content up to the CSS max-height, then scroll.
  useEffect(() => {
    const node = textareaRef.current;
    if (node === null) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, 160)}px`;
  }, [draft]);

  const send = () => {
    const text = draft.trim();
    if (text.length === 0 || disabled) return;
    onSubmit(text);
  };

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <div className="composer-inner">
        <div className="composer-row">
          <label className="sr-only" htmlFor="composer-input">
            Message
          </label>
          <textarea
            id="composer-input"
            ref={textareaRef}
            value={draft}
            placeholder="Ask about a refund, a policy, or request a human…"
            maxLength={4000}
            onChange={(event) => onDraftChange(event.target.value)}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={() => setComposing(false)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter inserts a newline. IME composition is
              // excluded so a candidate selection does not submit a turn.
              if (event.key === 'Enter' && !event.shiftKey && !composing) {
                event.preventDefault();
                send();
              }
            }}
          />
          {running ? (
            <button type="button" className="btn danger" onClick={onCancel}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn primary" disabled={disabled || draft.trim() === ''}>
              Send
            </button>
          )}
        </div>
        <div className="hint">
          {running
            ? 'A turn is in flight. Stopping records a cancellation; any refund that already committed stays committed.'
            : 'Enter to send · Shift+Enter for a new line'}
        </div>
      </div>
    </form>
  );
}
