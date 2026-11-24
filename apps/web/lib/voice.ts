/**
 * WebRTC voice client for Azure OpenAI Realtime.
 *
 * Shape of the connection:
 *
 *   browser --(SDP offer)--> our API --(ephemeral key)--> Azure
 *   browser <--(SDP answer)-- our API <----------------- Azure
 *   browser <===== audio + data channel =====> Azure        (direct)
 *
 * Audio never traverses our ingress, which is what keeps latency low. The
 * ephemeral key never reaches this file: the SDP exchange happens server-side,
 * so there is nothing here a visitor could lift and replay against our Azure
 * resource.
 *
 * Function calls are the one thing that comes back through our server. The
 * model asks for a tool over the data channel, we POST it to /voice/tool, the
 * executor applies the same guards the chat path uses, and the result goes
 * back over the data channel. The browser is the transport, never the
 * authority -- a tampered client can ask for a refund it has not earned and
 * will simply be refused, exactly as the chat path would refuse it.
 *
 * Barge-in is handled by the service. `turn_detection.interrupt_response` is
 * set at mint time, so when the customer starts speaking Azure stops
 * generating. Because the audio is a live WebRTC track rather than a buffered
 * file, "stop sending" is all it takes for playback to stop -- there is no
 * client-side queue to flush.
 */

import type { Credentials } from './client';
import type { StreamEvent, ToolName } from './events';
import { TOOL_LABELS } from './format';
import { toApiError } from './sse';

export type VoiceStatus =
  | 'idle'
  | 'requesting-mic'
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'closed'
  | 'error';

export interface VoiceCallbacks {
  onStatus: (status: VoiceStatus) => void;
  /** Partial and final transcripts of what the customer said. */
  onUserTranscript: (text: string, isFinal: boolean) => void;
  /** Partial and final transcripts of what the agent said. */
  onAgentTranscript: (text: string, isFinal: boolean) => void;
  /** Turn boundary, so the UI can open a new card before any tool runs. */
  onTurnStarted: (clientTurnId: string) => void;
  /** Typed events from a guarded tool call, identical to the chat stream. */
  onToolEvents: (clientTurnId: string, events: StreamEvent[]) => void;
  /**
   * A dropped call left a tool chain part-finished, and this session adopted
   * it instead of starting a new one.
   */
  onResumed: (clientTurnId: string, agentState: string) => void;
  /**
   * The spoken turn is closed AND its transcript is written.
   *
   * Fired after the POST rather than when the transcript event arrives,
   * because the server derives the turn's metric row from that write. Firing
   * earlier would have the UI read a trace that does not exist yet.
   */
  onTurnClosed: (clientTurnId: string, interrupted: boolean) => void;
  /** Input and output levels in 0..1, for the waveform. */
  onLevels: (mic: number, agent: number) => void;
  onError: (message: string) => void;
}

interface RealtimeServerEvent {
  type: string;
  [key: string]: unknown;
}

const DATA_CHANNEL = 'oai-events';

/** Did this response ask for a tool? Then the turn is not over. */
function calledTool(event: RealtimeServerEvent): boolean {
  const response = event.response;
  if (typeof response !== 'object' || response === null) return false;
  const output = (response as { output?: unknown }).output;
  if (!Array.isArray(output)) return false;
  return output.some(
    (item) =>
      typeof item === 'object' &&
      item !== null &&
      (item as { type?: unknown }).type === 'function_call',
  );
}

/** The tool's UI label, or its raw name if the model invented one. */
function toolLabel(name: string): string {
  return name in TOOL_LABELS ? TOOL_LABELS[name as ToolName] : name;
}


export class VoiceSession {
  private readonly credentials: Credentials;
  private readonly callbacks: VoiceCallbacks;

  private pc: RTCPeerConnection | null = null;
  /** A turn left open by a dropped call, adopted by the next one. */
  private resumeTurnId: string | null = null;
  private channel: RTCDataChannel | null = null;
  private micStream: MediaStream | null = null;
  private audioEl: HTMLAudioElement | null = null;

  private audioContext: AudioContext | null = null;
  private micAnalyser: AnalyserNode | null = null;
  private agentAnalyser: AnalyserNode | null = null;
  private levelTimer: number | null = null;
  private sessionTimer: number | null = null;

  private clientTurnId: string | null = null;
  private userTranscript = '';
  private agentTranscript = '';
  private agentSpeaking = false;
  private closed = false;
  /** Serialises data-channel handling; see the onmessage wiring. */
  private events: Promise<void> = Promise.resolve();

  constructor(credentials: Credentials, callbacks: VoiceCallbacks) {
    this.credentials = credentials;
    this.callbacks = callbacks;
  }

  // ------------------------------------------------------------ lifecycle --

  async start(): Promise<void> {
    try {
      this.callbacks.onStatus('requesting-mic');
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch (error) {
      // Denial is a normal outcome, not a crash: the UI offers chat instead.
      this.callbacks.onStatus('error');
      this.callbacks.onError(
        error instanceof DOMException && error.name === 'NotAllowedError'
          ? 'Microphone access was blocked. You can keep going in chat, or allow the microphone and try again.'
          : 'No microphone is available. You can keep going in chat.',
      );
      return;
    }

    this.callbacks.onStatus('connecting');

    // Before opening a new turn, ask the server whether the last one is still
    // open. A dropped call does not roll back the tools that already ran, and
    // the state machine lives in the audit rows, so the honest thing on
    // reconnect is to continue that turn rather than silently restart at INIT
    // with a refund half-done.
    await this.adoptInFlightTurn();

    const pc = new RTCPeerConnection();
    this.pc = pc;

    // Remote audio. Attaching the stream to an element is what makes it play;
    // the analyser below only observes it.
    const audio = document.createElement('audio');
    audio.autoplay = true;
    this.audioEl = audio;
    pc.ontrack = (event) => {
      const [stream] = event.streams;
      if (stream === undefined) return;
      audio.srcObject = stream;
      this.attachAgentAnalyser(stream);
    };

    for (const track of this.micStream.getAudioTracks()) {
      pc.addTrack(track, this.micStream);
    }

    const channel = pc.createDataChannel(DATA_CHANNEL);
    this.channel = channel;
    channel.onmessage = (event) => {
      // Serialised, not fire-and-forget. Handlers await network calls, and
      // `response.done` must not be processed while the tool call from that
      // same response is still in flight -- the two interleaving is what made
      // the turn-boundary logic below unpredictable.
      this.events = this.events.then(() => this.onServerEvent(event)).catch(() => undefined);
    };
    channel.onopen = () => this.callbacks.onStatus('listening');

    pc.onconnectionstatechange = () => {
      if (this.closed) return;
      if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
        this.callbacks.onStatus('error');
        this.callbacks.onError('The voice connection dropped. You can reconnect or use chat.');
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering so the offer carries its candidates, which keeps
    // the exchange to a single round trip instead of needing trickle ICE
    // through our API.
    await this.waitForIceGathering(pc);

    const localSdp = pc.localDescription?.sdp ?? offer.sdp;
    if (localSdp === undefined) {
      this.fail('Could not create a voice connection offer.');
      return;
    }

    let answer: string;
    let maxSessionS: number;
    try {
      const response = await fetch(`/api/sessions/${this.credentials.sessionId}/voice/connect`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.authHeaders(),
        },
        credentials: 'include',
        body: JSON.stringify({ sdp: localSdp }),
      });
      if (!response.ok) throw await toApiError(response);
      const body = (await response.json()) as { sdp: string; max_session_s: number };
      answer = body.sdp;
      maxSessionS = body.max_session_s;
    } catch (error) {
      this.fail(error instanceof Error ? error.message : 'Could not start the voice session.');
      return;
    }

    await pc.setRemoteDescription({ type: 'answer', sdp: answer });

    this.attachMicAnalyser(this.micStream);
    this.startLevelLoop();

    // A forgotten open microphone is a real cost and a real privacy problem,
    // so the session closes itself rather than relying on the page being shut.
    this.sessionTimer = window.setTimeout(() => {
      this.callbacks.onError('The voice session reached its time limit and was closed.');
      void this.stop();
    }, maxSessionS * 1000);
  }

  async stop(): Promise<void> {
    if (this.closed) return;
    this.closed = true;

    if (this.levelTimer !== null) window.clearInterval(this.levelTimer);
    if (this.sessionTimer !== null) window.clearTimeout(this.sessionTimer);

    // An in-flight reply that was never finished is still shown to the
    // customer, but recorded as interrupted so it is withheld from model
    // context later.
    if (this.clientTurnId !== null && this.agentTranscript.trim().length > 0) {
      await this.persistTranscript('assistant', this.agentTranscript, true);
    }

    this.channel?.close();
    this.pc?.close();
    for (const track of this.micStream?.getTracks() ?? []) track.stop();
    if (this.audioEl !== null) this.audioEl.srcObject = null;
    await this.audioContext?.close().catch(() => undefined);

    this.callbacks.onLevels(0, 0);
    this.callbacks.onStatus('closed');
  }

  private fail(message: string): void {
    this.callbacks.onStatus('error');
    this.callbacks.onError(message);
    void this.stop();
  }

  private async waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
    if (pc.iceGatheringState === 'complete') return;
    await new Promise<void>((resolve) => {
      // Bounded: a candidate that never arrives must not hang the connect.
      const timer = window.setTimeout(resolve, 2000);
      const check = () => {
        if (pc.iceGatheringState === 'complete') {
          window.clearTimeout(timer);
          pc.removeEventListener('icegatheringstatechange', check);
          resolve();
        }
      };
      pc.addEventListener('icegatheringstatechange', check);
    });
  }

  // --------------------------------------------------------- server events --

  private async onServerEvent(message: MessageEvent<string>): Promise<void> {
    let event: RealtimeServerEvent;
    try {
      event = JSON.parse(message.data) as RealtimeServerEvent;
    } catch {
      return;
    }

    switch (event.type) {
      case 'input_audio_buffer.speech_started':
        await this.beginUserTurn();
        break;

      case 'conversation.item.input_audio_transcription.delta': {
        const delta = typeof event.delta === 'string' ? event.delta : '';
        this.userTranscript += delta;
        this.callbacks.onUserTranscript(this.userTranscript, false);
        break;
      }

      case 'conversation.item.input_audio_transcription.completed': {
        const text = typeof event.transcript === 'string' ? event.transcript : this.userTranscript;
        this.userTranscript = text;
        this.callbacks.onUserTranscript(text, true);
        if (text.trim().length > 0) await this.persistTranscript('user', text, false);
        break;
      }

      case 'response.output_audio_transcript.delta': {
        const delta = typeof event.delta === 'string' ? event.delta : '';
        this.agentSpeaking = true;
        this.agentTranscript += delta;
        this.callbacks.onStatus('speaking');
        this.callbacks.onAgentTranscript(this.agentTranscript, false);
        break;
      }

      case 'response.output_audio_transcript.done': {
        const text =
          typeof event.transcript === 'string' ? event.transcript : this.agentTranscript;
        this.agentTranscript = text;
        this.callbacks.onAgentTranscript(text, true);
        break;
      }

      case 'response.function_call_arguments.done':
        this.callbacks.onStatus('thinking');
        await this.runTool(event);
        break;

      case 'response.done':
        // A response that asked for a tool is not the end of the turn: its
        // result goes back over the channel and the model answers again.
        // Closing here made every multi-tool spoken turn fail its second
        // tool call with "turn already finished".
        await this.completeResponse(calledTool(event));
        break;

      case 'error': {
        const detail = event.error;
        const text =
          typeof detail === 'object' && detail !== null && 'message' in detail
            ? String((detail as { message: unknown }).message)
            : 'The voice service reported an error.';
        this.callbacks.onError(text);
        break;
      }

      default:
        // Unknown events are ignored rather than treated as failures: the
        // realtime surface adds event types over time and none of them are
        // required for this flow.
        break;
    }
  }

  /**
   * A new spoken exchange.
   *
   * Also the barge-in path: speech starting while the agent is mid-reply ends
   * the previous turn as interrupted and opens a new one.
   */
  private async adoptInFlightTurn(): Promise<void> {
    try {
      const response = await fetch(`/api/sessions/${this.credentials.sessionId}/voice/state`, {
        headers: this.authHeaders(),
        credentials: 'include',
        cache: 'no-store',
      });
      if (!response.ok) return;
      const body = (await response.json()) as {
        active_turn: { client_turn_id: string } | null;
        agent_state?: string;
      };
      const active = body.active_turn;
      if (active === null) return;
      this.resumeTurnId = active.client_turn_id;
      this.callbacks.onResumed(active.client_turn_id, body.agent_state ?? 'unknown');
    } catch {
      // Starting fresh is the safe fallback: the guards re-derive state from
      // the audit rows on the next tool call either way.
    }
  }

  private async beginUserTurn(): Promise<void> {
    if (this.agentSpeaking) await this.finishAgentTurn(true);

    // One resumption only. The adopted turn belongs to the request that was
    // interrupted; whatever the customer says after it is a new one.
    this.clientTurnId = this.resumeTurnId ?? `v-${crypto.randomUUID()}`;
    this.resumeTurnId = null;
    this.userTranscript = '';
    this.agentTranscript = '';
    this.callbacks.onStatus('listening');
    this.callbacks.onTurnStarted(this.clientTurnId);
  }

  /**
   * One response finished. Whether the *turn* finished with it depends on
   * whether the model asked for a tool.
   */
  private async completeResponse(moreToCome: boolean): Promise<void> {
    if (!moreToCome) {
      await this.finishAgentTurn(false);
      return;
    }
    // Spoken filler alongside a tool call is part of the transcript, but it
    // does not close the turn, so it is written with final: false.
    const text = this.agentTranscript.trim();
    this.agentSpeaking = false;
    this.agentTranscript = '';
    if (this.clientTurnId !== null && text.length > 0) {
      await this.persistTranscript('assistant', text, false, false);
    }
    if (!this.closed) this.callbacks.onStatus('thinking');
  }

  private async finishAgentTurn(interrupted: boolean): Promise<void> {
    const text = this.agentTranscript.trim();
    const clientTurnId = this.clientTurnId;
    this.agentSpeaking = false;
    if (clientTurnId !== null && text.length > 0) {
      await this.persistTranscript('assistant', text, interrupted);
      this.callbacks.onTurnClosed(clientTurnId, interrupted);
    }
    this.agentTranscript = '';
    if (!this.closed) this.callbacks.onStatus('listening');
  }

  // ---------------------------------------------------------- tool calling --

  private async runTool(event: RealtimeServerEvent): Promise<void> {
    const callId = typeof event.call_id === 'string' ? event.call_id : null;
    const name = typeof event.name === 'string' ? event.name : null;
    const args = typeof event.arguments === 'string' ? event.arguments : '{}';
    if (callId === null || name === null) return;

    // A tool call can arrive before any transcript, so the turn may not exist
    // yet. The server creates it either way.
    if (this.clientTurnId === null) await this.beginUserTurn();
    const turnId = this.clientTurnId;
    if (turnId === null) return;

    let output = 'The tool could not be run.';
    try {
      const response = await fetch(`/api/sessions/${this.credentials.sessionId}/voice/tool`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.authHeaders(),
        },
        credentials: 'include',
        body: JSON.stringify({
          client_turn_id: turnId,
          call_id: callId,
          name,
          arguments: args,
        }),
      });
      if (!response.ok) throw await toApiError(response);

      const body = (await response.json()) as { output: string; events: StreamEvent[] };
      output = body.output;
      this.callbacks.onToolEvents(turnId, body.events);
    } catch (error) {
      // The model is told plainly, so it can apologise or escalate rather
      // than stalling on a call that never comes back.
      const reason = error instanceof Error ? error.message : 'unknown error';
      output = `The tool did not run: ${reason}. Tell the customer you could not complete that step.`;
      // The reason used to go only to the model, leaving the UI with a
      // sentence nobody could act on.
      this.callbacks.onError(`${toolLabel(name)} could not be completed: ${reason}`);
    }

    this.send({
      type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: callId, output },
    });
    // The model does not continue on its own after a tool result.
    this.send({ type: 'response.create' });
  }

  /**
   * Auth headers for the voice endpoints.
   *
   * A session resumed from the httpOnly cookie has no token in JavaScript, and
   * does not need one: every request below sends `credentials: 'include'`, and
   * the server accepts the cookie.
   */
  private authHeaders(): Record<string, string> {
    return this.credentials.token === null
      ? {}
      : { 'X-Session-Token': this.credentials.token };
  }

  private send(payload: Record<string, unknown>): void {
    if (this.channel?.readyState !== 'open') return;
    this.channel.send(JSON.stringify(payload));
  }

  private async persistTranscript(
    role: 'user' | 'assistant',
    text: string,
    interrupted: boolean,
    final = true,
  ): Promise<void> {
    if (this.clientTurnId === null) return;
    try {
      const response = await fetch(
        `/api/sessions/${this.credentials.sessionId}/voice/transcript`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...this.authHeaders(),
          },
          credentials: 'include',
          body: JSON.stringify({
            client_turn_id: this.clientTurnId,
            role,
            text,
            interrupted,
            final,
          }),
        },
      );
      // `fetch` rejects only on a network failure, so a 4xx used to land in
      // the empty catch below and vanish. A spoken turn that is not being
      // recorded is worth saying out loud: it leaves no row, and with the
      // access log off, no trace anywhere.
      if (!response.ok) throw await toApiError(response);
    } catch (error) {
      // Still not fatal -- the call continues -- but no longer silent.
      this.callbacks.onError(
        `The spoken ${role === 'user' ? 'question' : 'reply'} was not saved to the transcript: ${
          error instanceof Error ? error.message : 'unknown error'
        }`,
      );
    }
  }

  // ------------------------------------------------------------- waveform --

  private ensureAudioContext(): AudioContext {
    if (this.audioContext === null) {
      this.audioContext = new AudioContext();
    }
    return this.audioContext;
  }

  private attachMicAnalyser(stream: MediaStream): void {
    const context = this.ensureAudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    context.createMediaStreamSource(stream).connect(analyser);
    this.micAnalyser = analyser;
  }

  private attachAgentAnalyser(stream: MediaStream): void {
    const context = this.ensureAudioContext();
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    // Deliberately not connected to the destination: the audio element is
    // already playing this stream, and connecting here too would double it.
    context.createMediaStreamSource(stream).connect(analyser);
    this.agentAnalyser = analyser;
  }

  private startLevelLoop(): void {
    const read = (analyser: AnalyserNode | null): number => {
      if (analyser === null) return 0;
      const buffer = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (const sample of buffer) {
        const centred = (sample - 128) / 128;
        sum += centred * centred;
      }
      // RMS, scaled so ordinary speech fills most of the meter.
      return Math.min(1, Math.sqrt(sum / buffer.length) * 3);
    };

    this.levelTimer = window.setInterval(() => {
      this.callbacks.onLevels(read(this.micAnalyser), read(this.agentAnalyser));
    }, 60);
  }
}
