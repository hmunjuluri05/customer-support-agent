'use client';

import { useEffect, useRef } from 'react';

import type { VoiceStatus } from '@/lib/voice';

const STATUS_LABEL: Record<VoiceStatus, string> = {
  idle: 'Voice off',
  'requesting-mic': 'Waiting for microphone…',
  connecting: 'Connecting…',
  listening: 'Listening',
  thinking: 'Working on it',
  speaking: 'Speaking',
  closed: 'Voice ended',
  error: 'Voice unavailable',
};

const STATUS_TONE: Record<VoiceStatus, string> = {
  idle: '',
  'requesting-mic': 'warn',
  connecting: 'warn',
  listening: 'good',
  thinking: 'warn',
  speaking: 'good',
  closed: '',
  error: 'bad',
};

interface Props {
  status: VoiceStatus;
  micLevel: number;
  agentLevel: number;
  userPartial: string;
  agentPartial: string;
  error: string | null;
  onStart: () => void;
  onStop: () => void;
}

export function VoicePanel({
  status,
  micLevel,
  agentLevel,
  userPartial,
  agentPartial,
  error,
  onStart,
  onStop,
}: Props) {
  const active = status !== 'idle' && status !== 'closed' && status !== 'error';
  const connecting = status === 'requesting-mic' || status === 'connecting';

  return (
    <div className="voice-panel">
      <div className="voice-row">
        <button
          type="button"
          className={`btn ${active ? 'danger' : ''}`}
          onClick={active ? onStop : onStart}
          disabled={connecting}
        >
          {active ? 'End call' : 'Start voice'}
        </button>

        <span className={`chip ${STATUS_TONE[status]}`}>{STATUS_LABEL[status]}</span>

        {active && (
          <>
            <Waveform level={micLevel} label="You" tone="accent" />
            <Waveform level={agentLevel} label="Agent" tone="good" />
          </>
        )}
      </div>

      {(userPartial.length > 0 || agentPartial.length > 0) && (
        <div className="voice-live">
          {userPartial.length > 0 && (
            <div className="voice-line">
              <span className="voice-who">You</span>
              {userPartial}
            </div>
          )}
          {agentPartial.length > 0 && (
            <div className="voice-line agent">
              <span className="voice-who">Agent</span>
              {agentPartial}
            </div>
          )}
        </div>
      )}

      {error !== null && <div className="voice-error">{error}</div>}
    </div>
  );
}

/**
 * Level meter.
 *
 * Drawn on a canvas with a scrolling history rather than a single bar,
 * because a bar that only reflects the current sample reads as noise. The
 * history makes it obvious when someone is actually speaking, which is the
 * thing a viewer needs to see during a barge-in.
 */
function Waveform({
  level,
  label,
  tone,
}: {
  level: number;
  label: string;
  tone: 'accent' | 'good';
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const history = useRef<number[]>([]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;

    const bars = 28;
    const samples = history.current;
    samples.push(level);
    if (samples.length > bars) samples.shift();

    const context = canvas.getContext('2d');
    if (context === null) return;

    // Match the backing store to the CSS size so the bars are not blurry on
    // a high-DPI display.
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);

    const colour = getComputedStyle(canvas).getPropertyValue(
      tone === 'good' ? '--good' : '--accent',
    );
    context.fillStyle = colour.trim() || '#4da3ff';

    const barWidth = width / bars;
    samples.forEach((sample, index) => {
      const barHeight = Math.max(2, sample * height);
      context.fillRect(
        index * barWidth,
        (height - barHeight) / 2,
        Math.max(1, barWidth - 2),
        barHeight,
      );
    });
  }, [level, tone]);

  return (
    <span className="waveform" title={label}>
      <canvas ref={canvasRef} aria-hidden="true" />
      <span className="waveform-label">{label}</span>
    </span>
  );
}
