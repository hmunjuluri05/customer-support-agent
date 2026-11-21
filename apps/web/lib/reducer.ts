/**
 * Event reducer.
 *
 * One pure function turns the event stream into render state. Keeping it pure
 * and separate from the component means the stream semantics -- monotonic
 * sequence, exactly one terminal event, deltas only after grounding -- are
 * stated in one place instead of being spread across effects.
 *
 * Turns are keyed by clientTurnId, not turn_id: the client mints the former
 * before it has the latter, and the UI must render the submitted message
 * immediately rather than waiting for the server's first frame.
 */

import type {
  ActionCommitted,
  ActionDenied,
  Citation,
  EscalationCreated,
  GroundingDecided,
  IdentityVerified,
  Mode,
  SessionSnapshot,
  SnapshotMessage,
  StreamEvent,
  ToolName,
  TurnMetrics,
  TurnOutcome,
} from './events';

export type StepStatus = 'running' | 'completed' | 'failed';

export interface StepView {
  callIndex: number;
  tool: ToolName;
  status: StepStatus;
  summary: string;
  durationMs: number | null;
  errorCode: string | null;
  retryable: boolean;
}

export type TurnStatus = 'pending' | 'running' | 'finished' | 'cancelled' | 'error';

export interface TurnView {
  clientTurnId: string;
  turnId: string | null;
  userText: string;
  mode: Mode;
  status: TurnStatus;
  steps: StepView[];
  identity: IdentityVerified['payload'] | null;
  grounding: GroundingDecided['payload'] | null;
  answer: string;
  citations: Citation[];
  committed: ActionCommitted['payload'] | null;
  denied: ActionDenied['payload'] | null;
  escalation: EscalationCreated['payload'] | null;
  metrics: TurnMetrics['payload'] | null;
  outcome: TurnOutcome | null;
  closingMessage: string | null;
  error: {
    errorCode: string;
    message: string;
    retryAfterS: number | null;
    /** Whether trying the same thing again is worth the customer's time. */
    retryable: boolean;
  } | null;
  /** Set when a frame arrives with a sequence at or below one already applied. */
  outOfOrder: boolean;
  lastSequence: number;
  submittedAtMs: number;
  finishedAtMs: number | null;
}

export interface ChatState {
  /** Messages restored from the server snapshot, rendered above live turns. */
  history: SnapshotMessage[];
  turns: Record<string, TurnView>;
  order: string[];
  activeClientTurnId: string | null;
}

export const initialState: ChatState = {
  history: [],
  turns: {},
  order: [],
  activeClientTurnId: null,
};

export type ChatAction =
  | { kind: 'restore'; snapshot: SessionSnapshot }
  | { kind: 'submit'; clientTurnId: string; text: string; mode: Mode }
  | { kind: 'event'; clientTurnId: string; event: StreamEvent }
  | {
      kind: 'failed';
      clientTurnId: string;
      errorCode: string;
      message: string;
      retryAfterS: number | null;
      retryable: boolean;
    }
  | { kind: 'cancelled'; clientTurnId: string }
  // Voice turns reuse everything below `event`: a tool call made by the
  // realtime model produces the same typed events as a chat one, so the same
  // cases render it. Only the text arrives differently -- as transcripts
  // rather than as a submitted message and streamed deltas.
  | { kind: 'voice-turn'; clientTurnId: string }
  | { kind: 'voice-user'; clientTurnId: string; text: string; isFinal: boolean }
  | { kind: 'voice-agent'; clientTurnId: string; text: string; isFinal: boolean }
  | { kind: 'clear' };

function newTurn(clientTurnId: string, text: string, mode: Mode): TurnView {
  return {
    clientTurnId,
    turnId: null,
    userText: text,
    mode,
    status: 'pending',
    steps: [],
    identity: null,
    grounding: null,
    answer: '',
    citations: [],
    committed: null,
    denied: null,
    escalation: null,
    metrics: null,
    outcome: null,
    closingMessage: null,
    error: null,
    outOfOrder: false,
    lastSequence: -1,
    submittedAtMs: Date.now(),
    finishedAtMs: null,
  };
}

/** Upsert a step by call_index, so completion lands on the row that started. */
function putStep(steps: StepView[], step: StepView): StepView[] {
  const index = steps.findIndex((existing) => existing.callIndex === step.callIndex);
  if (index === -1) return [...steps, step];
  const next = [...steps];
  next[index] = step;
  return next;
}

function patchStep(
  steps: StepView[],
  callIndex: number,
  patch: Partial<StepView>,
): StepView[] {
  const index = steps.findIndex((existing) => existing.callIndex === callIndex);
  if (index === -1) return steps;
  const current = steps[index];
  if (current === undefined) return steps;
  const next = [...steps];
  next[index] = { ...current, ...patch };
  return next;
}

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.kind) {
    case 'clear':
      return initialState;

    case 'restore':
      return {
        ...initialState,
        history: action.snapshot.messages,
      };

    case 'submit': {
      if (state.turns[action.clientTurnId] !== undefined) return state;
      return {
        ...state,
        turns: {
          ...state.turns,
          [action.clientTurnId]: newTurn(action.clientTurnId, action.text, action.mode),
        },
        order: [...state.order, action.clientTurnId],
        activeClientTurnId: action.clientTurnId,
      };
    }

    case 'failed': {
      const turn = state.turns[action.clientTurnId];
      if (turn === undefined) return state;
      return withTurn(state, {
        ...turn,
        status: 'error',
        error: {
          errorCode: action.errorCode,
          message: action.message,
          retryAfterS: action.retryAfterS,
          retryable: action.retryable,
        },
        finishedAtMs: turn.finishedAtMs ?? Date.now(),
        steps: turn.steps.map((step) =>
          step.status === 'running' ? { ...step, status: 'failed' as const } : step,
        ),
      });
    }

    case 'cancelled': {
      const turn = state.turns[action.clientTurnId];
      if (turn === undefined) return state;
      // A turn that already finished is not retroactively cancelled, and a
      // refund that committed before the cancel landed stays visible.
      if (turn.status === 'finished') return state;
      return withTurn(state, {
        ...turn,
        status: 'cancelled',
        outcome: turn.outcome ?? 'cancelled',
        finishedAtMs: turn.finishedAtMs ?? Date.now(),
        steps: turn.steps.map((step) =>
          step.status === 'running' ? { ...step, status: 'failed' as const } : step,
        ),
      });
    }

    case 'voice-turn': {
      if (state.turns[action.clientTurnId] !== undefined) return state;
      const turn = newTurn(action.clientTurnId, '', 'voice');
      return {
        ...state,
        turns: { ...state.turns, [action.clientTurnId]: { ...turn, status: 'running' } },
        order: [...state.order, action.clientTurnId],
        activeClientTurnId: action.clientTurnId,
      };
    }

    case 'voice-user': {
      const turn = state.turns[action.clientTurnId];
      if (turn === undefined) return state;
      return withTurn(state, { ...turn, userText: action.text });
    }

    case 'voice-agent': {
      const turn = state.turns[action.clientTurnId];
      if (turn === undefined) return state;
      return withTurn(state, {
        ...turn,
        answer: action.text,
        // A spoken turn ends when the reply is complete. There is no
        // turn.finished event on this path: the realtime model signals
        // completion over the data channel, not through our stream.
        status: action.isFinal ? 'finished' : 'running',
        finishedAtMs: action.isFinal ? Date.now() : turn.finishedAtMs,
      });
    }

    case 'event':
      return applyEvent(state, action.clientTurnId, action.event);
  }
}

function withTurn(state: ChatState, turn: TurnView): ChatState {
  const stillActive = state.activeClientTurnId === turn.clientTurnId;
  const closed = turn.status !== 'pending' && turn.status !== 'running';
  return {
    ...state,
    turns: { ...state.turns, [turn.clientTurnId]: turn },
    activeClientTurnId: stillActive && closed ? null : state.activeClientTurnId,
  };
}

function applyEvent(state: ChatState, clientTurnId: string, event: StreamEvent): ChatState {
  const existing = state.turns[clientTurnId];
  if (existing === undefined) return state;

  // Duplicate or reordered frame: drop it, but record that it happened so the
  // drawer can show a contract violation rather than hiding it.
  if (event.sequence <= existing.lastSequence) {
    return withTurn(state, { ...existing, outOfOrder: true });
  }

  const turn: TurnView = { ...existing, lastSequence: event.sequence };

  switch (event.type) {
    case 'turn.started':
      return withTurn(state, { ...turn, turnId: event.turn_id, status: 'running' });

    case 'tool.started':
      return withTurn(state, {
        ...turn,
        turnId: turn.turnId ?? event.turn_id,
        status: 'running',
        steps: putStep(turn.steps, {
          callIndex: event.payload.call_index,
          tool: event.payload.tool,
          status: 'running',
          summary: event.payload.summary,
          durationMs: null,
          errorCode: null,
          retryable: false,
        }),
      });

    case 'tool.completed':
      return withTurn(state, {
        ...turn,
        steps: patchStep(turn.steps, event.payload.call_index, {
          status: 'completed',
          summary: event.payload.summary,
          durationMs: event.payload.duration_ms,
        }),
      });

    case 'tool.failed':
      return withTurn(state, {
        ...turn,
        steps: patchStep(turn.steps, event.payload.call_index, {
          status: 'failed',
          summary: event.payload.summary,
          durationMs: event.payload.duration_ms,
          errorCode: event.payload.error_code,
          retryable: event.payload.retryable,
        }),
      });

    case 'identity.verified':
      return withTurn(state, { ...turn, identity: event.payload });

    case 'grounding.decided':
      return withTurn(state, { ...turn, grounding: event.payload });

    case 'answer.delta':
      return withTurn(state, { ...turn, answer: turn.answer + event.payload.text });

    case 'action.committed':
      return withTurn(state, { ...turn, committed: event.payload });

    case 'action.denied':
      return withTurn(state, { ...turn, denied: event.payload });

    case 'escalation.created':
      return withTurn(state, { ...turn, escalation: event.payload });

    case 'turn.metrics':
      return withTurn(state, { ...turn, metrics: event.payload });

    case 'turn.finished':
      return withTurn(state, {
        ...turn,
        turnId: turn.turnId ?? event.turn_id,
        status: 'finished',
        outcome: event.payload.outcome,
        closingMessage: event.payload.message,
        // The replay path re-emits only turn.finished, so its message is the
        // answer when no deltas were streamed this time.
        answer: turn.answer.length > 0 ? turn.answer : (event.payload.message ?? ''),
        citations:
          event.payload.citations.length > 0 ? event.payload.citations : turn.citations,
        finishedAtMs: Date.now(),
        steps: turn.steps.map((step) =>
          step.status === 'running' ? { ...step, status: 'completed' as const } : step,
        ),
      });

    // Voice transcripts are handled by the voice controller, not here.
    case 'transcript.partial':
    case 'transcript.final':
      return withTurn(state, turn);

    case 'error':
      return withTurn(state, {
        ...turn,
        status: 'error',
        error: {
          errorCode: event.payload.error_code,
          message: event.payload.message,
          retryAfterS: event.payload.retry_after_s,
          retryable: event.payload.retryable,
        },
        // Not terminal on its own. The runner emits `error` and then still
        // finishes the turn, so marking the card closed here would hide the
        // outcome -- including a receipt recovered after the failure.
        finishedAtMs: turn.finishedAtMs,
      });
  }
}

/** Ordered live turns, oldest first. */
export function turnList(state: ChatState): TurnView[] {
  return state.order.flatMap((id) => {
    const turn = state.turns[id];
    return turn === undefined ? [] : [turn];
  });
}
