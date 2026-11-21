/**
 * Server-sent event client built on fetch.
 *
 * EventSource is not usable here: a turn is submitted with POST (it carries a
 * body and an idempotency key) and EventSource only issues GET. So the frames
 * are parsed by hand, which is a dozen lines and also makes the parser
 * testable without a browser.
 *
 * The parser is deliberately strict about the one thing that silently corrupts
 * a stream: a frame may arrive split across any number of network chunks, so
 * nothing is decoded until a blank-line frame boundary has actually been seen.
 */

import { isStreamEvent, type StreamEvent } from './events';

export class ApiError extends Error {
  readonly status: number;
  readonly errorCode: string;
  readonly retryAfterS: number | null;

  constructor(status: number, errorCode: string, message: string, retryAfterS: number | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.errorCode = errorCode;
    this.retryAfterS = retryAfterS;
  }
}

/** Shape of FastAPI's `detail`, which is a string for some raises and an object for others. */
interface ErrorBody {
  detail?: unknown;
  error_code?: unknown;
  message?: unknown;
  retry_after_s?: unknown;
}

export async function toApiError(response: Response): Promise<ApiError> {
  let body: ErrorBody = {};
  try {
    body = (await response.json()) as ErrorBody;
  } catch {
    // A non-JSON error body (an ingress 502 page, say) is still an error.
  }

  const detail = body.detail;
  const fromDetail =
    typeof detail === 'object' && detail !== null ? (detail as ErrorBody) : undefined;

  const errorCode =
    pickString(body.error_code) ??
    pickString(fromDetail?.error_code) ??
    `http_${response.status}`;

  const message =
    pickString(body.message) ??
    pickString(detail) ??
    pickString(fromDetail?.message) ??
    response.statusText ??
    'Request failed.';

  const headerRetry = response.headers.get('retry-after');
  const retryAfterS =
    pickNumber(fromDetail?.retry_after_s) ??
    pickNumber(body.retry_after_s) ??
    (headerRetry ? Number(headerRetry) : null);

  return new ApiError(response.status, errorCode, message, retryAfterS);
}

function pickString(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function pickNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Split a byte stream into SSE frames.
 *
 * Comment frames (`: hb`) are heartbeats used to keep the ingress from timing
 * out an idle connection; they carry no data and are dropped here.
 */
export async function* parseFrames(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const onAbort = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal?.addEventListener('abort', onAbort, { once: true });

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Frames end with a blank line. \r\n is accepted because an
      // intermediary may normalise line endings.
      let boundary = findBoundary(buffer);
      while (boundary !== null) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const event = decodeFrame(frame);
        if (event !== null) yield event;
        boundary = findBoundary(buffer);
      }
    }

    // A final frame with no trailing blank line (a stream cut short) is still
    // parsed, so a terminal event is not lost to a missing newline.
    const tail = decodeFrame(buffer);
    if (tail !== null) yield tail;
  } finally {
    signal?.removeEventListener('abort', onAbort);
    reader.releaseLock();
  }
}

function findBoundary(buffer: string): { index: number; length: number } | null {
  const lf = buffer.indexOf('\n\n');
  const crlf = buffer.indexOf('\r\n\r\n');
  if (lf === -1 && crlf === -1) return null;
  if (crlf !== -1 && (lf === -1 || crlf < lf)) return { index: crlf, length: 4 };
  return { index: lf, length: 2 };
}

function decodeFrame(frame: string): StreamEvent | null {
  const dataLines: string[] = [];

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line.length === 0) continue;
    if (line.startsWith(':')) continue; // heartbeat comment
    if (line.startsWith('data:')) {
      // One optional space after the colon is part of the framing, per spec.
      const value = line.slice(5);
      dataLines.push(value.startsWith(' ') ? value.slice(1) : value);
    }
    // `event:` is intentionally ignored: the discriminator is also inside the
    // JSON body, and trusting one source avoids the two disagreeing.
  }

  if (dataLines.length === 0) return null;

  try {
    const parsed: unknown = JSON.parse(dataLines.join('\n'));
    return isStreamEvent(parsed) ? parsed : null;
  } catch {
    // An unparseable frame is dropped rather than killing the turn. The server
    // emits only model_dump_json output, so this would be a transport bug.
    return null;
  }
}
