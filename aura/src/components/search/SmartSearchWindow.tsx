'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiUrl } from '@/lib/apiBase';

/**
 * The smart search window — one box for everything Akansha knows, and can do.
 *
 * Ctrl+K (or Cmd+K) anywhere in the app. It searches connected apps, goals, past
 * messages and memory in one request, and the row at the top is not a search hit
 * at all: it is what would happen if you just pressed Enter, resolved by the same
 * operator that drives the real browser window.
 *
 * Two rules this component is built around:
 *
 * 1. **Typing never acts.** The act row is a *decision*, resolved server-side
 *    with no runner bound, so `/api/search/omni` cannot open a window or move the
 *    mouse no matter how fast someone types. Acting takes a deliberate Enter, and
 *    a step that cannot be undone still stops for a second confirmation on the
 *    backend.
 * 2. **A stale answer is worse than a pending one.** Every request carries an
 *    `AbortController` and a sequence number, so an in-flight search for "wh"
 *    can never overwrite the results for "whatsapp".
 */

type OmniRow = {
  kind: string;
  id?: string;
  title: string;
  subtitle?: string;
  action: string;
  route?: string;
  runnable?: boolean;
  reasoning?: string[];
  needs?: string[];
  session_id?: string;
};

type OmniResponse = {
  query: string;
  results: OmniRow[];
  act: OmniRow | null;
  goal?: OmniRow;
};

/** Long enough that a fast typist makes one request per word, not per letter. */
const DEBOUNCE_MS = 180;

const KIND_LABEL: Record<string, string> = {
  act: 'Do it',
  app: 'App',
  goal: 'Goal',
  goal_new: 'New goal',
  message: 'Said before',
  memory: 'Remembered',
};

const KIND_TINT: Record<string, string> = {
  act: 'text-emerald-300 border-emerald-400/30 bg-emerald-400/10',
  app: 'text-sky-300 border-sky-400/30 bg-sky-400/10',
  goal: 'text-amber-300 border-amber-400/30 bg-amber-400/10',
  goal_new: 'text-amber-300 border-amber-400/30 bg-amber-400/10',
  message: 'text-slate-300 border-slate-400/30 bg-slate-400/10',
  memory: 'text-fuchsia-300 border-fuchsia-400/30 bg-fuchsia-400/10',
};

export default function SmartSearchWindow() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [data, setData] = useState<OmniResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [note, setNote] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const sequence = useRef(0);

  // Ctrl/Cmd+K to open, Escape to close. Registered once, on window, so it works
  // from any route without every page having to opt in.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((was) => !was);
        return;
      }
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    if (open) {
      setNote('');
      // The frame delay is not superstition: the input does not exist until this
      // render commits, so focusing synchronously focuses nothing.
      const frame = requestAnimationFrame(() => inputRef.current?.focus());
      return () => cancelAnimationFrame(frame);
    }
    setQuery('');
    setData(null);
    setCursor(0);
    return undefined;
  }, [open]);

  useEffect(() => {
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setData(null);
      setBusy(false);
      return undefined;
    }
    const mine = ++sequence.current;
    const controller = new AbortController();
    setBusy(true);
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(apiUrl(`/api/search/omni?q=${encodeURIComponent(trimmed)}`), {
          signal: controller.signal,
        });
        const body = (await response.json()) as OmniResponse;
        // Late answers are dropped rather than shown against a newer query.
        if (mine === sequence.current) setData(body);
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError' && mine === sequence.current) {
          setNote('Search is unavailable — is the backend running?');
        }
      } finally {
        if (mine === sequence.current) setBusy(false);
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  const rows = useMemo<OmniRow[]>(() => {
    if (!data) return [];
    const ordered: OmniRow[] = [];
    if (data.act) ordered.push(data.act);
    ordered.push(...data.results);
    if (data.goal) ordered.push(data.goal);
    return ordered;
  }, [data]);

  useEffect(() => setCursor(0), [rows.length]);

  const run = useCallback(
    async (row: OmniRow) => {
      const said = query.trim();
      try {
        if (row.action === 'operate') {
          setNote('Working on it…');
          const response = await fetch(apiUrl('/api/apps/operate'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ utterance: said, app_id: row.kind === 'app' ? row.id : '' }),
          });
          const body = await response.json();
          setNote(body?.report?.summary || body?.summary || body?.blocked || 'Done.');
          return;
        }
        if (row.action === 'goal') {
          setNote('Setting that as a goal…');
          const response = await fetch(apiUrl('/api/goals/set'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ utterance: said, pursue: true, dry_run: true }),
          });
          const body = await response.json();
          setNote(body?.spoken || 'Goal set.');
          return;
        }
        if (row.action === 'pursue' && row.id) {
          setNote('Moving that goal along…');
          const response = await fetch(apiUrl(`/api/goals/${row.id}/pursue`), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dry_run: true }),
          });
          const body = await response.json();
          setNote(body?.plan?.summary || 'Planned.');
          return;
        }
        if (row.action === 'connect') {
          setOpen(false);
          router.push('/connections');
          return;
        }
        if (row.action === 'open_chat' || row.action === 'ask') {
          setOpen(false);
          router.push('/chat-interface');
          return;
        }
        setNote('Nothing to do for that row.');
      } catch (error) {
        setNote(`That did not work: ${(error as Error)?.message ?? 'unknown error'}`);
      }
    },
    [query, router]
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setCursor((at) => Math.min(at + 1, Math.max(rows.length - 1, 0)));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setCursor((at) => Math.max(at - 1, 0));
    } else if (event.key === 'Enter' && rows[cursor]) {
      event.preventDefault();
      void run(rows[cursor]);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[120] flex items-start justify-center bg-slate-950/70 p-4 pt-[12vh] backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Smart search"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-white/10 bg-slate-900/95 shadow-2xl">
        <div className="flex items-center gap-3 border-b border-white/10 px-4 py-3">
          <span aria-hidden className="text-slate-400">
            ⌕
          </span>
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search, or say what to do — “message Amma on WhatsApp”, “set a goal to…”"
            aria-label="Search or give an instruction"
            className="w-full bg-transparent text-[15px] text-slate-100 outline-none placeholder:text-slate-500"
          />
          {busy ? <span className="text-xs text-slate-500">…</span> : null}
        </div>

        <ul className="max-h-[52vh] overflow-y-auto py-1" role="listbox">
          {rows.map((row, index) => (
            <li
              key={`${row.kind}-${row.id ?? index}`}
              role="option"
              aria-selected={index === cursor}
            >
              <button
                type="button"
                onMouseEnter={() => setCursor(index)}
                onClick={() => void run(row)}
                className={`flex w-full items-start gap-3 px-4 py-2.5 text-left transition-colors ${
                  index === cursor ? 'bg-white/10' : 'hover:bg-white/5'
                }`}
              >
                <span
                  className={`mt-0.5 shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
                    KIND_TINT[row.kind] ?? 'border-white/10 bg-white/5 text-slate-400'
                  }`}
                >
                  {KIND_LABEL[row.kind] ?? row.kind}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm text-slate-100">{row.title}</span>
                  {row.subtitle ? (
                    <span className="block truncate text-xs text-slate-400">{row.subtitle}</span>
                  ) : null}
                  {/* The reasoning is shown, not hidden: an action you cannot see the
                      basis for is one you cannot sensibly approve. */}
                  {row.kind === 'act' && row.reasoning?.length ? (
                    <span className="mt-1 block text-[11px] leading-relaxed text-slate-500">
                      {row.reasoning.join(' ')}
                    </span>
                  ) : null}
                </span>
              </button>
            </li>
          ))}
          {!rows.length && query.trim().length >= 2 && !busy ? (
            <li className="px-4 py-6 text-center text-sm text-slate-500">
              Nothing matched “{query.trim()}”.
            </li>
          ) : null}
          {query.trim().length < 2 ? (
            <li className="px-4 py-6 text-center text-sm text-slate-500">
              Type at least two characters. ↑↓ to move, Enter to run, Esc to close.
            </li>
          ) : null}
        </ul>

        {note ? (
          <p
            className="border-t border-white/10 bg-white/5 px-4 py-2.5 text-xs text-slate-300"
            role="status"
          >
            {note}
          </p>
        ) : null}
      </div>
    </div>
  );
}
