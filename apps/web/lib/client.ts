/**
 * Typed API client.
 *
 * Everything goes through same-origin /api/*, which app/api/[...path]/route.ts
 * proxies to the internal API container.
 *
 * Credentials: the API sets an httpOnly cookie AND returns the token in the
 * create-session body. This client sends the token as an X-Session-Token
 * header as well as allowing the cookie to ride along. The header is what
 * makes a page reload able to rejoin the same session, since the cookie alone
 * cannot tell the client which session id it belongs to.
 */

import {
  type CreatedSession,
  type DemoAccount,
  type FaultKind,
  type GroundingDecision,
  type Mode,
  type SessionAccount,
  type SessionSnapshot,
  type StreamEvent,
  type ToolName,
  type TurnOutcome,
} from './events';
import { ApiError, parseFrames, toApiError } from './sse';

export interface Credentials {
  sessionId: string;
  /**
   * Null for a session resumed from the httpOnly cookie after a browser
   * restart. The cookie is the credential there and the server accepts it, so
   * the token never has to be handed back to JavaScript to make resume work.
   */
  token: string | null;
}

function authHeaders(credentials: Credentials): HeadersInit {
  return credentials.token === null ? {} : { 'X-Session-Token': credentials.token };
}

async function expectOk(response: Response): Promise<Response> {
  if (!response.ok) throw await toApiError(response);
  return response;
}

export interface DemoCatalogue {
  accounts: DemoAccount[];
  /** False in a non-demo deployment: the fault and reset controls do not exist there. */
  demoMode: boolean;
}

export async function fetchAccounts(): Promise<DemoCatalogue> {
  const response = await expectOk(await fetch('/api/demo/accounts', { cache: 'no-store' }));
  const body = (await response.json()) as { accounts: DemoAccount[]; demo_mode?: boolean };
  return { accounts: body.accounts, demoMode: body.demo_mode === true };
}

export async function createSession(accountSlug: string | null): Promise<CreatedSession> {
  const response = await expectOk(
    await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ account_slug: accountSlug }),
    }),
  );
  return (await response.json()) as CreatedSession;
}

/** A live session this browser created, identified by its own cookie. */
export interface ResumableSession {
  session_id: string;
  account: (SessionAccount & { customer_name?: string }) | null;
  messages: number;
  last_activity: string | null;
  expires_at: string;
}

/**
 * Sessions this browser can resume: newest first, at most one per account.
 *
 * Keyed on the browser's own cookie, never on the account — which is a public
 * dropdown choice and would hand one visitor another visitor's transcript.
 */
export async function fetchResumable(): Promise<ResumableSession[]> {
  try {
    const response = await fetch('/api/sessions/current', {
      credentials: 'include',
      cache: 'no-store',
    });
    if (!response.ok) return [];
    const body = (await response.json()) as { sessions?: ResumableSession[] };
    return body.sessions ?? [];
  } catch {
    // Offering to resume is a convenience. If the lookup fails the picker
    // still works, which is the behaviour that must not break.
    return [];
  }
}

/**
 * Point this browser's session cookie at one of its earlier sessions.
 *
 * Required before reading it: listing is browser-scoped, but authorisation is
 * one cookie naming one session, so an older session is a 404 until the
 * cookie is switched to it.
 */
export async function resumeSession(sessionId: string): Promise<void> {
  await expectOk(
    await fetch(`/api/sessions/${sessionId}/resume`, {
      method: 'POST',
      credentials: 'include',
    }),
  );
}

export async function fetchSnapshot(credentials: Credentials): Promise<SessionSnapshot> {
  const response = await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}`, {
      headers: authHeaders(credentials),
      credentials: 'include',
      cache: 'no-store',
    }),
  );
  return (await response.json()) as SessionSnapshot;
}

export interface TurnRequest {
  credentials: Credentials;
  clientTurnId: string;
  text: string;
  mode: Mode;
  signal: AbortSignal;
}

/**
 * Submit a turn and yield its events as they arrive.
 *
 * Re-submitting the same clientTurnId is safe by construction: the server
 * replays the finished turn instead of re-running it, so a double click or a
 * retry cannot produce a second refund.
 */
export async function* streamTurn(request: TurnRequest): AsyncGenerator<StreamEvent> {
  const response = await fetch(`/api/sessions/${request.credentials.sessionId}/turns`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...authHeaders(request.credentials),
    },
    credentials: 'include',
    cache: 'no-store',
    signal: request.signal,
    body: JSON.stringify({
      client_turn_id: request.clientTurnId,
      text: request.text,
      mode: request.mode,
    }),
  });

  if (!response.ok) throw await toApiError(response);
  if (response.body === null) {
    throw new ApiError(502, 'empty_stream', 'The server returned no event stream.', null);
  }

  yield* parseFrames(response.body, request.signal);
}

export interface CancelResult {
  turn_id: string;
  status: string;
  committed_receipts: string[];
}

export async function cancelTurn(
  credentials: Credentials,
  turnId: string,
): Promise<CancelResult> {
  const response = await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/turns/${turnId}/cancel`, {
      method: 'POST',
      headers: authHeaders(credentials),
      credentials: 'include',
    }),
  );
  return (await response.json()) as CancelResult;
}

export interface TraceToolRun {
  tool: ToolName;
  call_index: number;
  status: string;
  duration_ms: number | null;
  /** Already redacted server-side: no verification codes, no raw account data. */
  args: Record<string, unknown> | null;
  result: Record<string, unknown> | string | null;
  error_code: string | null;
}

/**
 * One tool call on the turn's own clock.
 *
 * `start_ms` is an offset, not a duration. Turns recorded before offsets were
 * stored have none, which the drawer handles by laying the bars end to end and
 * saying so, rather than inventing positions.
 */
export interface WaterfallStep {
  tool: ToolName;
  status: string;
  ms: number | null;
  call_index?: number;
  start_ms?: number;
}

export interface TraceDetail {
  steps?: WaterfallStep[];
  citations?: string[];
  grounding?: { threshold: number; weak_margin: number } | null;
}

/** One message as the model received it. Bodies are previews, not transcripts. */
export interface PromptMessage {
  role: string;
  chars: number;
  preview: string;
  tool_calls: string[];
}

/** One model call: what went in, and what came back. */
export interface PromptStep {
  call: number;
  tools_offered: string[];
  messages: PromptMessage[];
  response: {
    tool_calls: string[];
    text_chars: number;
    preview: string;
    tokens_in: number | null;
    tokens_out: number | null;
  } | null;
  note?: string;
}

export interface TraceMetrics {
  ttft_ms: number | null;
  total_ms: number | null;
  tool_calls: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_estimate_usd: number | null;
  cost_rates_effective: string | null;
  cost_is_estimate: boolean;
  model: string | null;
  grounding_decision: GroundingDecision | null;
  top_similarity: number | null;
  grounding_threshold: number;
  grounding_weak_margin: number;
  /** False when the threshold shown is the current config, not the turn's own. */
  grounding_threshold_recorded: boolean;
  waterfall: TraceDetail | null;
}

export interface TurnTrace {
  turn_id: string;
  mode: Mode;
  status: string;
  outcome: TurnOutcome | null;
  tool_runs: TraceToolRun[];
  metrics: TraceMetrics | null;
  /** Per model call, in order. Empty for a turn recorded before this existed. */
  prompt: PromptStep[];
  /** Static instructions every turn runs under. Carries no customer data. */
  system_prompt: string;
}

export async function fetchTrace(
  credentials: Credentials,
  turnId: string,
): Promise<TurnTrace> {
  const response = await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/turns/${turnId}/trace`, {
      headers: authHeaders(credentials),
      credentials: 'include',
      cache: 'no-store',
    }),
  );
  return (await response.json()) as TurnTrace;
}

export interface LatencyStat {
  samples: number;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
}

export interface SessionMetrics {
  session_id: string;
  turns: number;
  turns_by_mode: Record<Mode, number>;
  tool_calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_estimate_usd: number | null;
  cost_rates_effective: string | null;
  cost_is_estimate: boolean;
  /** How many turns reported usage. Voice turns report none, so this is not `turns`. */
  turns_priced: number;
  /** Split by mode: a spoken turn's span includes the time the customer talked. */
  latency: Record<Mode, { total: LatencyStat; ttft: LatencyStat }>;
  grounding: Record<GroundingDecision, number>;
  outcomes: Partial<Record<TurnOutcome, number>>;
}

export async function fetchSessionMetrics(
  credentials: Credentials,
): Promise<SessionMetrics> {
  const response = await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/metrics`, {
      headers: authHeaders(credentials),
      credentials: 'include',
      cache: 'no-store',
    }),
  );
  return (await response.json()) as SessionMetrics;
}

export async function injectFault(
  credentials: Credentials,
  fault: FaultKind,
): Promise<void> {
  await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/fault`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders(credentials) },
      credentials: 'include',
      body: JSON.stringify({ fault }),
    }),
  );
}

/**
 * Delete this session's conversation, keeping the session and the receipts.
 *
 * Narrower than the fixture reset: one conversation, not every session, and
 * no invoice balance is touched. Money that moved stays moved.
 */
export async function clearHistory(credentials: Credentials): Promise<void> {
  await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/history/clear`, {
      method: 'POST',
      headers: authHeaders(credentials),
      credentials: 'include',
    }),
  );
}

export async function resetDemo(credentials: Credentials): Promise<void> {
  await expectOk(
    await fetch(`/api/sessions/${credentials.sessionId}/reset`, {
      method: 'POST',
      headers: authHeaders(credentials),
      credentials: 'include',
    }),
  );
}

// ------------------------------------------------------------- persistence --

const STORAGE_KEY = 'support-agent-session';

/**
 * sessionStorage, not localStorage: a demo session should not outlive the tab,
 * and two tabs holding different accounts must not fight over one key.
 */
export function saveCredentials(credentials: Credentials): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(credentials));
  } catch {
    // Private browsing can refuse storage. Losing reconnect is acceptable;
    // failing to start a session is not.
  }
}

export function loadCredentials(): Credentials | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<Credentials>;
    if (typeof parsed.sessionId !== 'string') return null;
    return {
      sessionId: parsed.sessionId,
      token: typeof parsed.token === 'string' ? parsed.token : null,
    };
  } catch {
    return null;
  }
}

export function clearCredentials(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to do.
  }
}

export function newClientTurnId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `t-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
