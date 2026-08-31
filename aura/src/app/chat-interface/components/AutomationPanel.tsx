'use client';

/**
 * The automation console, inside chat.
 *
 * Asked for directly: *"please delete the browser page, integrate its features
 * only features to the chat interface directly"*. So `/browser-automation` is
 * gone and what it could actually do lives here, next to the conversation that
 * triggers most of it.
 *
 * What moved, and what did not:
 *
 *  - **Freeform prompts did not need to move.** `ChatThread` already routes an
 *    automation intent to `POST /api/automation/browser/prompt`, so typing "open
 *    notepad and type my checklist" in chat has always run it. Duplicating a
 *    second prompt box here would have been the old page with a new frame.
 *  - **Scheduling moved**, because there is no way to say "at 7:30pm" to a chat
 *    box and have it mean a stored run — that needs a time field.
 *  - **The saved list moved**, with its delete.
 *  - **Permission toggles are new here.** `PUT /api/automation/browser/permissions`
 *    has existed the whole time with no interface anywhere in the app, so the six
 *    flags the chat header counts were unreachable and could only ever read as
 *    their defaults.
 *  - **Live runtime moved to the top**, because it is the answer to *"it is not
 *    live, actually"*: screen size, window manager, worker presence, overdue runs.
 *  - **The capability marketing cards did not move.** Four cards describing what
 *    automation is, on a panel opened by someone who is already using it.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  Clock3,
  Loader2,
  MonitorCheck,
  MonitorOff,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react';
import { toast } from 'sonner';

import { apiUrl } from '@/lib/apiBase';
import { summariseAutomation, type AutomationStatus } from '@/lib/automationStatus';

interface ScheduledAction {
  id: string;
  action: string;
  label: string;
  target?: string;
  run_at: string;
  background: boolean;
  status: string;
  created_at: string;
  note: string;
}

interface Status extends AutomationStatus {
  scheduled_actions: ScheduledAction[];
  disclaimer?: string;
}

/** Labels for the six flags. The keys are the server's contract. */
const PERMISSION_LABELS: Record<string, string> = {
  open_links: 'Open links',
  open_close_tabs: 'Open and close tabs',
  type_into_page: 'Type into a page',
  edit_fields: 'Edit form fields',
  delete_draft_content: 'Clear draft content',
  background_open: 'Open in the background',
};

function formatRunAt(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

export default function AutomationPanel({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [runAt, setRunAt] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(apiUrl('/api/automation/browser/status'));
      if (!res.ok) throw new Error('status request failed');
      setStatus((await res.json()) as Status);
    } catch (error) {
      console.warn('Automation status failed:', error);
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const summary = useMemo(() => summariseAutomation(status), [status]);

  const togglePermission = useCallback(async (key: string, next: boolean) => {
    setSaving(key);
    try {
      const res = await fetch(apiUrl('/api/automation/browser/permissions'), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: next }),
      });
      if (!res.ok) throw new Error('permission update failed');
      setStatus((await res.json()) as Status);
      toast.success(`${PERMISSION_LABELS[key] ?? key} ${next ? 'allowed' : 'blocked'}`);
    } catch (error) {
      console.warn('Permission update failed:', error);
      toast.error('Could not change that permission');
    } finally {
      setSaving(null);
    }
  }, []);

  const schedule = useCallback(async () => {
    if (!prompt.trim()) {
      toast.info('Describe the task to run');
      return;
    }
    if (!runAt) {
      toast.info('Pick the time to run it');
      return;
    }
    setScheduling(true);
    try {
      const res = await fetch(apiUrl('/api/automation/browser/prompt'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, run_at: runAt, background: true, cowork: true }),
      });
      const payload = (await res.json()) as { message?: string; detail?: string };
      if (!res.ok) throw new Error(payload.detail ?? 'Could not schedule that');
      toast.success(payload.message ?? 'Saved');
      setPrompt('');
      setRunAt('');
      await load();
    } catch (error) {
      console.warn('Scheduling failed:', error);
      toast.error(error instanceof Error ? error.message : 'Could not schedule that');
    } finally {
      setScheduling(false);
    }
  }, [load, prompt, runAt]);

  const remove = useCallback(
    async (id: string) => {
      setDeletingId(id);
      try {
        const res = await fetch(apiUrl(`/api/automation/browser/scheduled/${id}`), {
          method: 'DELETE',
        });
        if (!res.ok) throw new Error('delete failed');
        toast.success('Removed');
        await load();
      } catch (error) {
        console.warn('Could not remove scheduled automation:', error);
        toast.error('Could not remove that');
      } finally {
        setDeletingId(null);
      }
    },
    [load]
  );

  const runtime = status?.runtime;
  const permissions = status?.permissions ?? {};

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-bold tracking-tight text-foreground">Automation</h2>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{summary.label}</p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="Re-probe now"
          aria-label="Re-probe automation status"
        >
          {loading ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="Close"
          aria-label="Close automation panel"
        >
          <X size={15} />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {/* Liveness. Measured on this request, not declared. */}
        <section
          className={`rounded-xl border p-3 ${
            summary.tone === 'live'
              ? 'border-accent/30 bg-accent/5'
              : summary.tone === 'degraded'
                ? 'border-amber-400/30 bg-amber-400/5'
                : 'border-rose-400/30 bg-rose-400/5'
          }`}
        >
          <div className="flex items-center gap-2">
            {summary.tone === 'live' ? (
              <MonitorCheck size={15} className="shrink-0 text-accent" />
            ) : summary.tone === 'degraded' ? (
              <AlertTriangle size={15} className="shrink-0 text-amber-400" />
            ) : (
              <MonitorOff size={15} className="shrink-0 text-rose-400" />
            )}
            <span className="text-xs font-semibold text-foreground">{summary.label}</span>
          </div>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">{summary.detail}</p>
          {runtime && (
            <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
              <Fact
                label="Screen"
                value={runtime.screen ? `${runtime.screen.width}x${runtime.screen.height}` : 'none'}
                ok={Boolean(runtime.screen)}
              />
              <Fact
                label="Window manager"
                value={runtime.window_manager ? 'responding' : 'silent'}
                ok={runtime.window_manager}
              />
              <Fact
                label="Timed-run worker"
                value={runtime.runner_available ? 'present' : 'missing'}
                ok={runtime.runner_available}
              />
              <Fact
                label="Overdue runs"
                value={String(runtime.scheduled_due)}
                ok={runtime.scheduled_due === 0}
              />
            </dl>
          )}
        </section>

        {/* Permissions. The endpoint has always existed; this is its first UI. */}
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Permissions
          </h3>
          <div className="mt-2 space-y-1.5">
            {Object.keys(PERMISSION_LABELS).map((key) => {
              const on = Boolean(permissions[key]);
              const inherited = status?.defaulted?.includes(key);
              return (
                <label
                  key={key}
                  className="flex cursor-pointer items-center gap-2.5 rounded-lg border border-border bg-card/50 px-3 py-2 transition-colors hover:bg-muted/50"
                >
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={saving === key || !status}
                    onChange={(event) => void togglePermission(key, event.target.checked)}
                    className="h-3.5 w-3.5 accent-[hsl(var(--primary))]"
                  />
                  <span className="min-w-0 flex-1 truncate text-xs text-foreground">
                    {PERMISSION_LABELS[key]}
                  </span>
                  {saving === key ? (
                    <Loader2 size={12} className="animate-spin text-muted-foreground" />
                  ) : inherited ? (
                    <span
                      className="text-[10px] text-muted-foreground/70"
                      title="On because of the default, never confirmed by you"
                    >
                      default
                    </span>
                  ) : on ? (
                    <CheckCircle2 size={12} className="text-accent" />
                  ) : null}
                </label>
              );
            })}
          </div>
        </section>

        {/* Scheduling: the one thing a chat message genuinely cannot express. */}
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Run at a set time
          </h3>
          <p className="mt-1.5 text-[11px] leading-4 text-muted-foreground/80">
            For anything to run now, just say it in the chat — that already reaches the same
            automation layer.
          </p>
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={3}
            placeholder="Open the downloads folder and convert every PDF to a PPT"
            className="mt-2 w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-xs leading-5 text-foreground outline-none transition placeholder:text-muted-foreground/60 focus:border-primary/60"
          />
          <div className="mt-2 flex gap-2">
            <label className="min-w-0 flex-1">
              <span className="sr-only">Run time</span>
              <input
                type="datetime-local"
                value={runAt}
                onChange={(event) => setRunAt(event.target.value)}
                className="w-full rounded-lg border border-border bg-background px-2.5 py-2 text-xs text-foreground outline-none focus:border-primary/60"
              />
            </label>
            <button
              type="button"
              onClick={() => void schedule()}
              disabled={scheduling}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-60"
            >
              {scheduling ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <CalendarClock size={13} />
              )}
              Schedule
            </button>
          </div>
        </section>

        {/* Saved runs. */}
        <section>
          <h3 className="flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Saved runs
            <span className="font-normal normal-case tracking-normal">
              {status?.scheduled_actions?.length ?? 0}
            </span>
          </h3>
          <div className="mt-2 space-y-1.5">
            {status?.scheduled_actions?.length ? (
              status.scheduled_actions.map((item) => {
                const overdue = new Date(item.run_at).getTime() < Date.now();
                return (
                  <div
                    key={item.id}
                    className="rounded-lg border border-border bg-card/50 px-3 py-2"
                  >
                    <div className="flex items-start gap-2">
                      <p className="min-w-0 flex-1 text-xs leading-4 text-foreground">
                        {item.label}
                      </p>
                      <button
                        type="button"
                        onClick={() => void remove(item.id)}
                        className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:bg-rose-500/10 hover:text-rose-400"
                        title="Remove"
                        aria-label={`Remove ${item.label}`}
                      >
                        {deletingId === item.id ? (
                          <Loader2 size={12} className="animate-spin" />
                        ) : (
                          <Trash2 size={12} />
                        )}
                      </button>
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 ${
                          overdue
                            ? 'bg-amber-400/10 text-amber-300'
                            : 'bg-muted text-muted-foreground'
                        }`}
                      >
                        <Clock3 size={9} />
                        {formatRunAt(item.run_at)}
                        {overdue ? ' · overdue' : ''}
                      </span>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-muted-foreground">
                        {item.status}
                      </span>
                    </div>
                  </div>
                );
              })
            ) : (
              <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-xs text-muted-foreground">
                Nothing saved. Anything with a time goes here.
              </p>
            )}
          </div>
        </section>

        {status?.disclaimer && (
          <p className="rounded-lg border border-amber-400/20 bg-amber-400/5 px-3 py-2 text-[11px] leading-5 text-amber-200/90">
            {status.disclaimer}
          </p>
        )}
      </div>
    </div>
  );
}

function Fact({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="rounded-lg bg-background/60 px-2 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`mt-0.5 font-medium ${ok ? 'text-foreground' : 'text-rose-300'}`}>{value}</dd>
    </div>
  );
}
