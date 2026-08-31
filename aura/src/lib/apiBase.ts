/**
 * The one place that knows where the backend lives.
 *
 * Before this module, `http://localhost:8000` was written out at 58 call sites
 * across 18 files and only the cognitive dashboard read the environment. That is
 * not a style problem: it means the app cannot be pointed at a backend on another
 * host, cannot be served over HTTPS without mixed-content failures, and cannot be
 * deployed at all without editing source. Change the origin here (or in
 * `NEXT_PUBLIC_API_BASE_URL`) and every request follows.
 *
 * `NEXT_PUBLIC_` is required for the value to survive into the browser bundle;
 * Next.js inlines it at build time, so a change needs a rebuild, not just a
 * restart.
 */

const FALLBACK_ORIGIN = 'http://localhost:8000';

/** Backend origin, no trailing slash. */
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL || FALLBACK_ORIGIN).replace(
  /\/+$/,
  ''
);

/**
 * Absolute URL for a backend path.
 *
 * Accepts `/api/chat`, `api/chat`, or an already-absolute URL — the last case is
 * passed through so callers can hand through a link the backend gave them
 * without having to check its shape first.
 */
export function apiUrl(path: string): string {
  if (/^(https?:|wss?:|data:|blob:)/i.test(path)) return path;
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
}

/** Same, for WebSocket routes such as `/ws/voice/{session_id}`. */
export function wsUrl(path: string): string {
  if (/^wss?:/i.test(path)) return path;
  const base = API_BASE_URL.replace(/^http/i, (match) => (match === 'HTTP' ? 'WS' : 'ws'));
  return `${base}${path.startsWith('/') ? path : `/${path}`}`;
}
