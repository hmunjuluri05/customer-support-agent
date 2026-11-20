/**
 * Same-origin reverse proxy to the API container.
 *
 * The API runs with internal-only ingress, so this is the only path from the
 * browser to it. Keeping both behind one public origin means no CORS, no
 * cross-site cookie rules, and one HTTPS certificate.
 *
 * Streaming is the whole point of this file, so it is written explicitly
 * rather than using a next.config rewrite: the upstream response body is
 * handed back untouched, so SSE frames reach the browser as they are produced
 * instead of being collected and flushed at the end of the turn.
 */

const API_BASE = process.env.API_INTERNAL_URL ?? 'http://127.0.0.1:8000';

// Never prerender, never cache: every request here is session-scoped.
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
export const fetchCache = 'force-no-store';

/** Hop-by-hop and length headers must not be forwarded verbatim. */
const STRIP_REQUEST = new Set([
  'host',
  'connection',
  'keep-alive',
  'transfer-encoding',
  'upgrade',
  'content-length',
  'accept-encoding',
]);

const STRIP_RESPONSE = new Set([
  'connection',
  'keep-alive',
  'transfer-encoding',
  'content-encoding',
  'content-length',
]);

function upstreamUrl(request: Request): string {
  const incoming = new URL(request.url);
  // The matched segment is the full /api/... path, which the API also serves
  // under /api, so the pathname passes through unchanged.
  return `${API_BASE}${incoming.pathname}${incoming.search}`;
}

function forwardHeaders(request: Request): Headers {
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!STRIP_REQUEST.has(key.toLowerCase())) headers.set(key, value);
  });
  // Identity encoding keeps an event stream from being buffered by a
  // compressor that waits for enough bytes to be worth compressing.
  headers.set('accept-encoding', 'identity');
  return headers;
}

/**
 * Map an upstream Location back onto the public origin.
 *
 * Returns null when the target is not the API we proxy, so a redirect to an
 * unrelated host is dropped rather than passed to the browser.
 */
function sameOriginLocation(location: string, request: Request): string | null {
  const incoming = new URL(request.url);
  let target: URL;
  try {
    target = new URL(location, API_BASE);
  } catch {
    return null;
  }

  if (target.origin !== new URL(API_BASE).origin) return null;
  return `${incoming.origin}${target.pathname}${target.search}`;
}

async function proxy(request: Request): Promise<Response> {
  const method = request.method;
  const hasBody = method !== 'GET' && method !== 'HEAD';

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl(request), {
      method,
      headers: forwardHeaders(request),
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: 'manual',
      cache: 'no-store',
    });
  } catch (error) {
    // The API being unreachable is a distinct, actionable failure: surface it
    // as 502 with a machine-readable code rather than a Next.js stack page.
    return Response.json(
      {
        error_code: 'api_unreachable',
        message: `Could not reach the API at ${API_BASE}.`,
        detail: error instanceof Error ? error.message : String(error),
      },
      { status: 502 },
    );
  }

  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!STRIP_RESPONSE.has(key.toLowerCase())) headers.append(key, value);
  });
  headers.set('cache-control', headers.get('cache-control') ?? 'no-store');

  // An upstream redirect names the API's internal FQDN, which resolves only
  // inside the Container Apps environment. Forwarding it verbatim both breaks
  // the browser and publishes an internal hostname, so it is rewritten back
  // onto this origin and dropped if it points anywhere else.
  const location = headers.get('location');
  if (location !== null) {
    const rewritten = sameOriginLocation(location, request);
    if (rewritten === null) headers.delete('location');
    else headers.set('location', rewritten);
  }

  // upstream.body is passed through as a stream. Reading it to completion here
  // would defeat the entire design.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
export const PUT = proxy;
