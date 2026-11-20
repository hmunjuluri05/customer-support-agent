'use client';

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import {
  type Credentials,
  type SessionMetrics,
  type TurnTrace,
  clearCredentials,
  createSession,
  cancelTurn,
  fetchAccounts,
  fetchSessionMetrics,
  fetchSnapshot,
  fetchTrace,
  injectFault,
  loadCredentials,
  newClientTurnId,
  resetDemo,
  saveCredentials,
  streamTurn,
} from '@/lib/client';
import type { DemoAccount, FaultKind, Mode, SessionAccount } from '@/lib/events';
import { money } from '@/lib/format';
import { ApiError } from '@/lib/sse';
import { initialState, reduce, turnList } from '@/lib/reducer';
import { VoiceSession, type VoiceStatus } from '@/lib/voice';
import { AccountPicker } from './AccountPicker';
import { Composer } from './Composer';
import { DemoPanel } from './DemoPanel';
import { SessionStats } from './SessionStats';
import { TraceDrawer } from './TraceDrawer';
import { TurnCard } from './TurnCard';
import { VoicePanel } from './VoicePanel';

type Phase = 'loading' | 'picking' | 'chatting';

export function ChatApp() {
  const [phase, setPhase] = useState<Phase>('loading');
  const [accounts, setAccounts] = useState<DemoAccount[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  const [credentials, setCredentials] = useState<Credentials | null>(null);
  const [account, setAccount] = useState<SessionAccount | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [chat, dispatch] = useReducer(reduce, initialState);

  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle');
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [micLevel, setMicLevel] = useState(0);
  const [agentLevel, setAgentLevel] = useState(0);
  const [userPartial, setUserPartial] = useState('');
  const [agentPartial, setAgentPartial] = useState('');

  const [demoMode, setDemoMode] = useState(false);
  const [armedFault, setArmedFault] = useState<FaultKind>('none');
  const [demoBusy, setDemoBusy] = useState(false);
  const [resetNote, setResetNote] = useState<string | null>(null);
  const [sessionMetrics, setSessionMetrics] = useState<SessionMetrics | null>(null);
  const [inspecting, setInspecting] = useState<string | null>(null);
  const [trace, setTrace] = useState<TurnTrace | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);

  const inFlight = useRef<{ controller: AbortController; clientTurnId: string } | null>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const voice = useRef<VoiceSession | null>(null);
  // Which voice turn the transcript callbacks belong to. A ref, not state,
  // because the callbacks are created once and must not close over a stale value.
  const currentVoiceTurn = useRef<string | null>(null);

  // Read back from the server rather than accumulated in the browser, so a
  // refresh does not reset the numbers and a turn this tab missed still counts.
  const refreshMetrics = useCallback(async (creds: Credentials) => {
    try {
      setSessionMetrics(await fetchSessionMetrics(creds));
    } catch {
      // The rollup is informational. A failure here must not disturb the chat.
    }
  }, []);

  const openTrace = useCallback(
    async (turnId: string) => {
      if (credentials === null) return;
      setInspecting(turnId);
      setTrace(null);
      setTraceError(null);
      setTraceLoading(true);
      try {
        setTrace(await fetchTrace(credentials, turnId));
      } catch (error) {
        setTraceError(error instanceof Error ? error.message : String(error));
      } finally {
        setTraceLoading(false);
      }
    },
    [credentials],
  );

  const loadAccounts = useCallback(async () => {
    setAccountsError(null);
    try {
      const catalogue = await fetchAccounts();
      setAccounts(catalogue.accounts);
      setDemoMode(catalogue.demoMode);
    } catch (error) {
      setAccountsError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  // Mount: load the demo accounts and rejoin an existing session if this tab
  // has one. Rejoining restores the committed transcript from the server
  // rather than keeping it in the browser, so a refresh cannot invent history.
  useEffect(() => {
    let cancelled = false;

    const start = async () => {
      await loadAccounts();
      const stored = loadCredentials();
      if (stored === null) {
        if (!cancelled) setPhase('picking');
        return;
      }
      try {
        const snapshot = await fetchSnapshot(stored);
        if (cancelled) return;
        setCredentials(stored);
        setAccount(snapshot.account);
        dispatch({ kind: 'restore', snapshot });
        setPhase('chatting');
        void refreshMetrics(stored);
      } catch {
        // Expired or unknown token: drop it and start over rather than
        // showing a session that no longer exists.
        clearCredentials();
        if (!cancelled) setPhase('picking');
      }
    };

    void start();
    return () => {
      cancelled = true;
    };
  }, [loadAccounts, refreshMetrics]);

  // Keep the newest content in view, but only when already near the bottom, so
  // reading back through a transcript is not fought by every delta.
  useEffect(() => {
    const node = transcriptRef.current;
    if (node === null) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    if (distance < 180) node.scrollTop = node.scrollHeight;
  }, [chat]);

  const signIn = useCallback(async (slug: string) => {
    setSigningIn(true);
    setBanner(null);
    try {
      const created = await createSession(slug);
      const next: Credentials = {
        sessionId: created.session_id,
        token: created.session_token,
      };
      saveCredentials(next);
      setCredentials(next);
      setAccount(created.account);
      dispatch({ kind: 'clear' });
      setPhase('chatting');
    } catch (error) {
      setAccountsError(error instanceof Error ? error.message : String(error));
    } finally {
      setSigningIn(false);
    }
  }, []);

  const submit = useCallback(
    async (text: string, mode: Mode = 'chat') => {
      if (credentials === null || inFlight.current !== null) return;

      const clientTurnId = newClientTurnId();
      dispatch({ kind: 'submit', clientTurnId, text, mode });
      setDraft('');
      setBanner(null);

      const controller = new AbortController();
      inFlight.current = { controller, clientTurnId };

      let sawTerminal = false;
      try {
        for await (const event of streamTurn({
          credentials,
          clientTurnId,
          text,
          mode,
          signal: controller.signal,
        })) {
          if (event.type === 'turn.finished') sawTerminal = true;
          dispatch({ kind: 'event', clientTurnId, event });
        }

        if (!sawTerminal) {
          // The stream ended without the one terminal event a turn must emit.
          // Anything already committed is durable, so the honest move is to
          // say the view is incomplete and offer to reload it from the server.
          dispatch({
            kind: 'failed',
            clientTurnId,
            errorCode: 'stream_truncated',
            message: 'The connection closed before the turn finished.',
            retryAfterS: null,
            // Reconnect first. Re-sending blind could ask for a second
            // refund when the first one may have committed.
            retryable: false,
          });
          setBanner(
            'The event stream ended early. Any action that committed is still recorded — reconnect to reload the transcript from the server.',
          );
        }
      } catch (error) {
        if (controller.signal.aborted) {
          dispatch({ kind: 'cancelled', clientTurnId });
        } else if (error instanceof ApiError) {
          dispatch({
            kind: 'failed',
            clientTurnId,
            errorCode: error.errorCode,
            message: error.message,
            retryAfterS: error.retryAfterS,
            // A request that was refused before it ran committed nothing, so
            // trying again is safe. 409 is the exception: another turn of
            // this session is already in flight.
            retryable: error.status === 429 || error.status >= 500,
          });
        } else {
          dispatch({
            kind: 'failed',
            clientTurnId,
            errorCode: 'network_error',
            message: error instanceof Error ? error.message : String(error),
            retryAfterS: null,
            retryable: false,
          });
        }
      } finally {
        inFlight.current = null;
        void refreshMetrics(credentials);
      }
    },
    [credentials, refreshMetrics],
  );

  const cancel = useCallback(async () => {
    const current = inFlight.current;
    if (current === null || credentials === null) return;

    // Tell the server first: the durable cancel flag is what stops further
    // tool calls. Aborting only the fetch would leave the turn running.
    const turnId = chat.turns[current.clientTurnId]?.turnId ?? null;
    if (turnId !== null) {
      try {
        await cancelTurn(credentials, turnId);
      } catch {
        // Falls through to the local abort; the turn will time out server-side.
      }
    }
    current.controller.abort();
  }, [chat.turns, credentials]);

  // A new turn id, not the failed one: re-sending the same id replays the
  // stored failure instead of retrying. That is safe here because the control
  // is only offered when nothing committed -- see TurnCard.
  const retry = useCallback(
    (text: string) => {
      void submit(text);
    },
    [submit],
  );

  const armFault = useCallback(
    async (fault: FaultKind) => {
      if (credentials === null) return;
      setDemoBusy(true);
      setResetNote(null);
      try {
        await injectFault(credentials, fault);
        setArmedFault(fault);
      } catch (error) {
        setBanner(error instanceof Error ? error.message : String(error));
      } finally {
        setDemoBusy(false);
      }
    },
    [credentials],
  );

  const resetDemoData = useCallback(async () => {
    if (credentials === null) return;
    setDemoBusy(true);
    setResetNote(null);
    try {
      await resetDemo(credentials);
      // The reset clears every session row, including this one, so the token
      // in hand is dead the moment it returns. Signing in again is part of
      // the operation, not a separate step the user has to discover.
      const slug = account?.slug ?? null;
      clearCredentials();
      setArmedFault('none');
      setSessionMetrics(null);
      dispatch({ kind: 'clear' });
      await loadAccounts();
      if (slug !== null) await signIn(slug);
      else setPhase('picking');
      setResetNote('Fixture balances restored, new session started.');
    } catch (error) {
      setBanner(error instanceof Error ? error.message : String(error));
    } finally {
      setDemoBusy(false);
    }
  }, [account, credentials, loadAccounts, signIn]);

  const reconnect = useCallback(async () => {
    if (credentials === null) return;
    setBanner(null);
    try {
      const snapshot = await fetchSnapshot(credentials);
      setAccount(snapshot.account);
      dispatch({ kind: 'restore', snapshot });
    } catch (error) {
      setBanner(error instanceof Error ? error.message : String(error));
    }
  }, [credentials]);

  const startVoice = useCallback(() => {
    if (credentials === null || voice.current !== null) return;
    setVoiceError(null);
    setUserPartial('');
    setAgentPartial('');

    const session = new VoiceSession(credentials, {
      onStatus: setVoiceStatus,
      onTurnStarted: (clientTurnId) => {
        currentVoiceTurn.current = clientTurnId;
        setUserPartial('');
        setAgentPartial('');
        dispatch({ kind: 'voice-turn', clientTurnId });
      },
      onUserTranscript: (text, isFinal) => {
        setUserPartial(isFinal ? '' : text);
        const turnId = currentVoiceTurn.current;
        if (turnId !== null) dispatch({ kind: 'voice-user', clientTurnId: turnId, text, isFinal });
      },
      onAgentTranscript: (text, isFinal) => {
        setAgentPartial(isFinal ? '' : text);
        const turnId = currentVoiceTurn.current;
        if (turnId !== null) dispatch({ kind: 'voice-agent', clientTurnId: turnId, text, isFinal });
      },
      // Tool events from the voice path are the same typed events the chat
      // stream emits, so they go through the same reducer and render in the
      // same card.
      onToolEvents: (clientTurnId, events) => {
        for (const event of events) dispatch({ kind: 'event', clientTurnId, event });
      },
      onResumed: (clientTurnId, agentState) => {
        currentVoiceTurn.current = clientTurnId;
        dispatch({ kind: 'voice-turn', clientTurnId });
        setBanner(
          `Reconnected to a request that was still in progress (agent state: ${agentState}). ` +
            'The steps already completed were not repeated.',
        );
      },
      // Fired after the transcript POST, which is what makes the server write
      // the spoken turn's metric row. Refreshing any earlier would read a
      // rollup that does not include the turn just finished.
      onTurnClosed: () => {
        void refreshMetrics(credentials);
      },
      onLevels: (mic, agent) => {
        setMicLevel(mic);
        setAgentLevel(agent);
      },
      onError: setVoiceError,
    });

    voice.current = session;
    void session.start();
  }, [credentials, refreshMetrics]);

  const stopVoice = useCallback(() => {
    const session = voice.current;
    voice.current = null;
    setUserPartial('');
    setAgentPartial('');
    void session?.stop();
  }, []);

  // Closing the tab mid-call must still release the microphone.
  useEffect(() => {
    return () => {
      void voice.current?.stop();
      voice.current = null;
    };
  }, []);

  const newSession = useCallback(() => {
    inFlight.current?.controller.abort();
    inFlight.current = null;
    void voice.current?.stop();
    voice.current = null;
    setVoiceStatus('idle');
    clearCredentials();
    setCredentials(null);
    setAccount(null);
    setSessionMetrics(null);
    setInspecting(null);
    setTrace(null);
    dispatch({ kind: 'clear' });
    setPhase('picking');
  }, []);

  const turns = turnList(chat);
  const activeTurn =
    chat.activeClientTurnId === null ? null : (chat.turns[chat.activeClientTurnId] ?? null);
  const running = activeTurn !== null;

  const currentAccount = useMemo(
    () => accounts.find((candidate) => candidate.slug === account?.slug) ?? null,
    [accounts, account],
  );

  const suggestions = useMemo(
    () => buildSuggestions(accounts, account?.slug ?? null),
    [accounts, account],
  );

  return (
    <div className="shell">
      <header className="topbar">
        <h1>Customer Support Agent</h1>
        <span className="synthetic-badge">Synthetic demo data</span>
        <div className="spacer" />
        {account !== null && (
          <span className="chips">
            <span className="chip">{account.label}</span>
            <span className={`chip ${account.contract_status === 'active' ? 'good' : 'warn'}`}>
              {account.contract_status}
            </span>
          </span>
        )}
        {credentials !== null && (
          <button type="button" className="btn small" onClick={newSession}>
            New session
          </button>
        )}
      </header>

      {phase === 'loading' && <div className="center-note">Starting…</div>}

      {phase === 'picking' && (
        <AccountPicker
          accounts={accounts}
          busy={signingIn}
          error={accountsError}
          onPick={(slug) => void signIn(slug)}
          onRetry={() => void loadAccounts()}
        />
      )}

      {phase === 'chatting' && (
        <div className="main">
          <div className="column">
            <div className="transcript" ref={transcriptRef}>
              <div className="transcript-inner">
                {chat.history.length === 0 && turns.length === 0 && (
                  <div className="center-note">
                    Ask for a refund, or ask what the policy says. Each tool call the agent makes
                    appears here as it runs.
                  </div>
                )}

                {chat.history.map((message) => (
                  <div
                    key={message.id}
                    className={`bubble ${message.role === 'user' ? 'user' : 'assistant'}`}
                  >
                    <div className="bubble-meta">
                      {message.role === 'user' ? 'You' : 'Agent'}
                      {message.mode === 'voice' ? ' · voice' : ''}
                      {message.interrupted ? ' · interrupted' : ''}
                      {' · restored'}
                    </div>
                    {message.content}
                  </div>
                ))}

                {turns.map((turn) => (
                  <TurnCard
                    key={turn.clientTurnId}
                    turn={turn}
                    onInspect={(turnId) => void openTrace(turnId)}
                    onRetry={retry}
                    retryDisabled={running}
                  />
                ))}

                {banner !== null && (
                  <div className="error-box">
                    <div>{banner}</div>
                    <button
                      type="button"
                      className="btn small"
                      style={{ marginTop: 8 }}
                      onClick={() => void reconnect()}
                    >
                      Reconnect
                    </button>
                  </div>
                )}
              </div>
            </div>

            <VoicePanel
              status={voiceStatus}
              micLevel={micLevel}
              agentLevel={agentLevel}
              userPartial={userPartial}
              agentPartial={agentPartial}
              error={voiceError}
              onStart={startVoice}
              onStop={stopVoice}
            />

            <Composer
              disabled={running || credentials === null}
              running={running}
              draft={draft}
              onDraftChange={setDraft}
              onSubmit={(text) => void submit(text)}
              onCancel={() => void cancel()}
            />
          </div>

          <aside className="rail">
            <div className="rail-card">
              <h2>Try one of these</h2>
              <div className="suggestions">
                {suggestions.map((suggestion) => (
                  <button
                    type="button"
                    className="suggestion"
                    key={suggestion.text}
                    disabled={running}
                    onClick={() => void submit(suggestion.text)}
                  >
                    <strong style={{ display: 'block' }}>{suggestion.label}</strong>
                    {suggestion.text}
                  </button>
                ))}
              </div>
            </div>

            {currentAccount !== null && (
              <div className="rail-card">
                <h2>Invoices on file</h2>
                {currentAccount.invoices.map((invoice) => (
                  <div className="invoice-row" key={invoice.ref}>
                    <div>
                      <div className="invoice-ref">{invoice.ref}</div>
                      <div className="invoice-note">{invoice.description}</div>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <div>{money(invoice.amount_minor, invoice.currency)}</div>
                      {invoice.refunded_minor > 0 && (
                        <div className="invoice-note">
                          {money(invoice.refunded_minor, invoice.currency)} refunded
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            <SessionStats metrics={sessionMetrics} />

            {demoMode && (
              <DemoPanel
                armed={armedFault}
                busy={demoBusy || running}
                onArm={(fault) => void armFault(fault)}
                onReset={() => void resetDemoData()}
                resetNote={resetNote}
              />
            )}

            <div className="rail-card">
              <h2>Session</h2>
              <dl className="kv">
                <dt>Session</dt>
                <dd>{credentials?.sessionId.slice(0, 8) ?? '--'}</dd>
                <dt>Turns this tab</dt>
                <dd>{turns.length}</dd>
                <dt>Restored</dt>
                <dd>{chat.history.length}</dd>
              </dl>
            </div>
          </aside>
        </div>
      )}

      {inspecting !== null && (
        <TraceDrawer
          trace={trace}
          loading={traceLoading}
          error={traceError}
          onClose={() => {
            setInspecting(null);
            setTrace(null);
            setTraceError(null);
          }}
          onRetry={() => void openTrace(inspecting)}
        />
      )}
    </div>
  );
}

interface Suggestion {
  label: string;
  text: string;
}

/**
 * Demo prompts derived from the fixtures rather than hard-coded, so the
 * cross-account probe always names an invoice that genuinely belongs to a
 * different account than the one signed in.
 */
function buildSuggestions(accounts: DemoAccount[], slug: string | null): Suggestion[] {
  const mine = accounts.find((candidate) => candidate.slug === slug);
  const ownInvoice = mine?.invoices.find((invoice) => invoice.refunded_minor === 0);
  const exhausted = mine?.invoices.find(
    (invoice) => invoice.refunded_minor > 0 && invoice.amount_minor - invoice.refunded_minor < 1500,
  );
  const foreign = accounts
    .filter((candidate) => candidate.slug !== slug)
    .flatMap((candidate) => candidate.invoices)[0];

  // Each prompt names an invoice, an amount AND a reason. Without all three
  // the agent correctly asks a clarifying question instead of acting, which
  // makes for a confusing first click.
  const refundRequest = (ref: string) =>
    `Invoice ${ref} was a duplicate charge. Please refund $15.00 on it.`;

  const suggestions: Suggestion[] = [
    {
      label: 'Refund request',
      text: refundRequest(ownInvoice?.ref ?? 'INV-1001'),
    },
    {
      label: 'Policy question',
      text: 'What does the refund policy allow for my plan?',
    },
  ];

  if (exhausted !== undefined) {
    suggestions.push({
      label: 'Already refunded',
      text: refundRequest(exhausted.ref),
    });
  }

  if (foreign !== undefined) {
    suggestions.push({
      label: 'Another account’s invoice',
      text: refundRequest(foreign.ref),
    });
  }

  suggestions.push({
    label: 'Out of scope',
    text: 'Can you tell me when my next shipment arrives?',
  });

  return suggestions;
}
