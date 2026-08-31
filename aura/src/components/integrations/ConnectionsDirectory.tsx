'use client';

/**
 * The connections directory.
 *
 * This replaces a grid of twenty-seven cards. The grid was not wrong about any
 * individual app — every status on it was probed — but it answered the wrong
 * question. It showed *a list somebody typed*, in a shape that gave a browser you
 * have installed and a Slack workspace you have never registered the same amount
 * of screen, and it had no way at all to reach the other hundred applications on
 * this machine or any website in the world. The report was blunt: *"no connections
 * not like this Static route ... all apps which have windows apps can connect by
 * clicking once ... can also in one click authenticate any website"*.
 *
 * So the shape here is a directory, not a dashboard:
 *
 *   - **Connected** first, as a strip of tiles. What is already working should be
 *     one glance, not twenty-seven cards to read past.
 *   - **On this PC** — the real Start Menu, scanned. One `+` per app. There is
 *     nothing to authenticate: a desktop application has no login.
 *   - **Any website** — a URL box. One click opens it in a browser profile Akansha
 *     owns and the person signs in themselves. No password is ever typed here.
 *   - **Everything else** in compact two-column rows by category, so a category
 *     fits on a screen instead of scrolling for three.
 *
 * A row is deliberately one line of description and one button. The card's four
 * paragraphs — capabilities, safety note, what is missing, docs — are still all
 * here, but behind expanding the row, because they are what you read *once*, when
 * you connect a thing, and never again.
 *
 * No brand logos: this repo ships none, and inventing them by hotlinking a CDN
 * would put a network request per app on a page about not leaking anything. Tiles
 * are a deterministic monogram plus a hue derived from the app id, so the same app
 * is the same colour on every load.
 */

import React, { memo, useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  AlertTriangle,
  ArrowUpRight,
  Check,
  ChevronRight,
  Globe,
  KeyRound,
  Loader2,
  MonitorSmartphone,
  MoreHorizontal,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react';

import {
  type AppCatalog,
  type AppEntry,
  type AppStatus,
  type DiscoveredApp,
  OUTCOME_PRESENTATION,
  actionFor,
  adoptApp,
  connectApp,
  controlApp,
  disconnectApp,
  discoverApps,
  drivesBrowser,
  familyLabel,
  fetchAppCatalog,
  routeLabel,
  saveAppCredentials,
  signInToSite,
} from '@/lib/appConnect';

const EMPTY_DRAFT: Record<string, string> = {};

/** Category order. Anything unlisted sorts after these, alphabetically. */
const FAMILY_ORDER = [
  'on_this_pc',
  'website',
  'desktop',
  'messaging',
  'model_provider',
  'productivity',
  'developer',
  'media',
];

/**
 * A stable hue from an app id.
 *
 * Stable is the whole requirement: a tile that changes colour between loads reads
 * as a different app. A hash rather than an index into a palette, because an index
 * would shift every tile when one app is added to the list.
 */
function hueFor(appId: string): number {
  let hash = 0;
  for (let index = 0; index < appId.length; index += 1) {
    hash = (hash * 31 + appId.charCodeAt(index)) % 3600;
  }
  return hash / 10;
}

/** `https://web.whatsapp.com/` -> `web.whatsapp.com`. The scheme is noise in a sentence. */
function hostOf(url: string): string {
  return url.replace(/^https?:\/\//, '').replace(/\/+$/, '');
}

/** Up to two initials, skipping the noise words that would produce "T" for everything. */ function monogram(
  label: string
): string {
  const words = label
    .replace(/[^\p{L}\p{N} ]+/gu, ' ')
    .split(/\s+/)
    .filter((word) => word && !['the', 'for', 'and', 'of', 'my'].includes(word.toLowerCase()));
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** The monogram tile. Two sizes: the connected strip, and a directory row. */
const Tile = memo(function Tile({
  appId,
  label,
  large = false,
}: {
  appId: string;
  label: string;
  large?: boolean;
}) {
  const hue = hueFor(appId);
  return (
    <span
      aria-hidden
      className={`flex shrink-0 items-center justify-center rounded-xl border font-semibold tracking-tight ${
        large ? 'h-12 w-12 text-sm' : 'h-9 w-9 text-[11px]'
      }`}
      style={{
        background: `hsl(${hue} 62% 22%)`,
        borderColor: `hsl(${hue} 62% 38%)`,
        color: `hsl(${hue} 85% 82%)`,
      }}
    >
      {monogram(label)}
    </span>
  );
});

/** The one-line status word under a row's label, when there is something to say. */
function statusLine(entry: AppEntry): string {
  const status = entry.status;
  if (!status) return entry.capabilities.slice(0, 3).join(' · ');
  if (status.outcome === 'connected') {
    return status.connected_to ? `Connected · ${status.connected_to}` : status.detail;
  }
  return status.detail;
}

interface RowProps {
  entry: AppEntry;
  busy: boolean;
  expanded: boolean;
  editing: boolean;
  draft: Record<string, string>;
  onToggle: (entry: AppEntry) => void;
  onAct: (entry: AppEntry) => void;
  /** Opens the credential form for an entry whose primary route is now the browser. */
  onUseKeys: (entry: AppEntry) => void;
  onCancel: () => void;
  onDraftChange: (key: string, value: string) => void;
  onSubmit: (entry: AppEntry) => void;
  onControl: (entry: AppEntry, verb: string, text: string) => Promise<string>;
}

/**
 * One directory row.
 *
 * `memo` is real here only because every handler arrives `useCallback`-stable and
 * takes the entry as an argument. An inline `() => onAct(entry)` in the parent's
 * map would allocate a new function per entry per render and re-render all thirty
 * rows on every keystroke in the search box — which is exactly the mistake the
 * chat surface was measured making.
 */
const AppRow = memo(function AppRow({
  entry,
  busy,
  expanded,
  editing,
  draft,
  onToggle,
  onAct,
  onUseKeys,
  onCancel,
  onDraftChange,
  onSubmit,
  onControl,
}: RowProps) {
  const action = actionFor(entry);
  const outcome = entry.status?.outcome ?? 'disconnected';
  const presentation = OUTCOME_PRESENTATION[outcome];
  const connected = outcome === 'connected';
  const missing = entry.status?.required ?? [];

  return (
    <div
      className={`rounded-xl border transition-colors ${
        expanded
          ? 'border-white/15 bg-white/[0.055]'
          : 'border-transparent hover:border-white/10 hover:bg-white/[0.035]'
      }`}
    >
      <div className="flex items-center gap-3 px-2.5 py-2">
        <Tile appId={entry.app_id} label={entry.label} />
        <button
          type="button"
          onClick={() => onToggle(entry)}
          aria-expanded={expanded}
          className="min-w-0 flex-1 text-left"
        >
          <span className="flex items-center gap-1.5">
            <span className="truncate text-[13px] font-semibold text-white/90">{entry.label}</span>
            {connected ? (
              <Check className="h-3 w-3 shrink-0 text-emerald-400" aria-label="Connected" />
            ) : null}
          </span>
          <span className="mt-0.5 block truncate text-[11px] leading-tight text-white/45">
            {statusLine(entry)}
          </span>
        </button>
        <button
          type="button"
          onClick={() => onAct(entry)}
          disabled={busy}
          title={action.label}
          aria-label={`${action.label} — ${entry.label}`}
          className={`flex h-7 items-center gap-1 rounded-lg border px-2 text-[11px] font-medium transition-colors disabled:opacity-50 ${
            connected
              ? 'border-white/10 text-white/55 hover:border-white/25 hover:text-white/90'
              : 'border-white/15 bg-white/[0.06] text-white/80 hover:border-cyan-400/40 hover:text-cyan-200'
          }`}
        >
          {busy ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : connected ? (
            <MoreHorizontal className="h-3.5 w-3.5" />
          ) : (
            <Plus className="h-3.5 w-3.5" />
          )}
          {!connected ? <span className="hidden sm:inline">{action.label}</span> : null}
        </button>
      </div>

      {expanded || editing ? (
        <div className="space-y-2.5 border-t border-white/[0.07] px-2.5 pb-3 pt-2.5 text-[11px]">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${presentation.tone}`}
            >
              <span className={`h-1.5 w-1.5 rounded-full ${presentation.dot}`} />
              {presentation.label}
            </span>
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] text-white/45">
              {routeLabel(entry)}
            </span>
          </div>

          {entry.capabilities.length ? (
            <p className="text-white/60">
              <span className="text-white/35">Akansha can: </span>
              {entry.capabilities.join(', ')}
            </p>
          ) : null}

          <p className="flex items-start gap-1.5 text-white/45">
            <ShieldCheck className="mt-[1px] h-3 w-3 shrink-0 text-emerald-400/70" />
            {entry.safety_note}
          </p>

          {connected ? <ControlStrip entry={entry} onRun={onControl} /> : null}

          {/* The browser route, said out loud. This row used to open with "OAuth
              client id" and "OAuth client secret", which reads as: to let Akansha
              post for you, first become a registered developer. One sentence
              naming the click, and the fields demoted to a link below it. */}
          {!connected && entry.web_url ? (
            <p className="flex items-start gap-1.5 text-violet-200/80">
              <Globe className="mt-[1px] h-3 w-3 shrink-0 text-violet-300/80" />
              One click opens {hostOf(entry.web_url)} in Akansha&apos;s own browser window and you
              sign in there as yourself. No developer app, no client secret, and your password never
              passes through this app.
            </p>
          ) : null}

          {missing.length && !editing ? (
            entry.web_url ? (
              // Demoted, not hidden. Someone who has already registered an
              // application should still be able to use it -- the API route needs no
              // window and no visible browser, so it is the better one where the work
              // is already done. It is a link rather than a form because the form
              // being open by default is what made this row look like a requirement.
              <button
                type="button"
                onClick={() => onUseKeys(entry)}
                className="text-left text-white/35 underline decoration-white/20 underline-offset-2 hover:text-white/70"
              >
                Already have {entry.label} developer credentials? Use them instead.
              </button>
            ) : (
              <p className="flex items-start gap-1.5 text-amber-300/80">
                <AlertTriangle className="mt-[1px] h-3 w-3 shrink-0" />
                Still needs: {missing.map((field) => field.label).join(', ')}
              </p>
            )
          ) : null}

          {entry.status?.authorize_url ? (
            <a
              href={entry.status.authorize_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sky-300 hover:text-sky-200"
            >
              Open the provider&apos;s approval page <ArrowUpRight className="h-3 w-3" />
            </a>
          ) : null}

          {editing && entry.fields.length ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                onSubmit(entry);
              }}
              className="space-y-2 rounded-lg border border-white/10 bg-black/25 p-2.5"
            >
              {entry.fields.map((field) => (
                <label key={field.key} className="block">
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="text-[11px] font-medium text-white/75">
                      {field.label}
                      {field.optional ? (
                        <span className="ml-1 text-white/35">(optional)</span>
                      ) : null}
                    </span>
                    {field.secret ? <KeyRound className="h-3 w-3 text-white/30" /> : null}
                  </span>
                  <input
                    type={field.secret ? 'password' : 'text'}
                    value={draft[field.key] ?? ''}
                    onChange={(event) => onDraftChange(field.key, event.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-[12px] text-white/90 outline-none focus:border-cyan-400/50"
                  />
                  <span className="mt-1 block text-[10px] leading-tight text-white/35">
                    {field.where}
                  </span>
                </label>
              ))}
              <div className="flex items-center gap-2 pt-0.5">
                <button
                  type="submit"
                  disabled={busy}
                  className="rounded-md border border-cyan-400/40 bg-cyan-500/10 px-2.5 py-1 text-[11px] font-medium text-cyan-100 hover:bg-cyan-500/20 disabled:opacity-50"
                >
                  {busy ? 'Saving…' : 'Save and connect'}
                </button>
                <button
                  type="button"
                  onClick={onCancel}
                  className="rounded-md border border-white/10 px-2.5 py-1 text-[11px] text-white/55 hover:text-white/85"
                >
                  Cancel
                </button>
              </div>
              <p className="text-[10px] text-white/35">
                Stored encrypted on this machine. Nothing is sent anywhere except the service it
                belongs to.
              </p>
            </form>
          ) : null}

          {entry.docs_url ? (
            <a
              href={entry.docs_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-white/35 hover:text-white/70"
            >
              Documentation <ArrowUpRight className="h-3 w-3" />
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});

/**
 * A row for something found by scanning this PC that is not connected yet.
 *
 * Separate from `AppRow` because a scanned shortcut has no status, no fields and no
 * safety note — it has a path. Reusing the entry row would mean inventing an
 * `AppEntry` shaped object for it and then having to explain why its status is
 * always undefined.
 */
const DiscoveredRow = memo(function DiscoveredRow({
  app,
  busy,
  onAdopt,
}: {
  app: DiscoveredApp;
  busy: boolean;
  onAdopt: (app: DiscoveredApp) => void;
}) {
  const taken = app.adopted || app.declared;
  return (
    <div className="flex items-center gap-3 rounded-xl border border-transparent px-2.5 py-2 hover:border-white/10 hover:bg-white/[0.035]">
      <Tile appId={app.app_id} label={app.label} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-semibold text-white/90">{app.label}</p>
        <p className="mt-0.5 truncate text-[11px] leading-tight text-white/40">
          {taken ? 'Already connected' : app.exe_path || app.launch_target}
        </p>
      </div>
      <button
        type="button"
        onClick={() => onAdopt(app)}
        disabled={busy || taken}
        aria-label={taken ? `${app.label} is already connected` : `Connect ${app.label}`}
        className="flex h-7 w-7 items-center justify-center rounded-lg border border-white/15 bg-white/[0.06] text-white/80 transition-colors hover:border-cyan-400/40 hover:text-cyan-200 disabled:border-white/5 disabled:text-white/25"
      >
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : taken ? (
          <Check className="h-3.5 w-3.5" />
        ) : (
          <Plus className="h-3.5 w-3.5" />
        )}
      </button>
    </div>
  );
});

/**
 * The verbs a connected app can actually be driven with.
 *
 * Keyed by strategy and kept deliberately short of the backend's full table: the
 * `click` and `type` web verbs need a CSS selector, and a selector box on a
 * directory row is a developer console, not a connection. Those two live in chat,
 * where there is room to say what to click. Everything listed here needs nothing
 * but the verb — which is what makes it a button.
 *
 * `backend/app_control.py` owns the real table; a verb here that is not in
 * `DESKTOP_VERBS` or `WEB_VERBS` is a button whose only possible outcome is the
 * backend's "'x' is not one of:" rejection, so the two are pinned together by a
 * test rather than by hoping.
 *
 * The desktop `click` verb is the one omission that is deliberate: it needs the
 * name of the control to click, and a name box is only useful next to the list of
 * names — which is what "Read window" produces, in chat, where there is room for
 * the answer.
 */
const CONTROL_VERBS: Record<string, { verb: string; label: string; needsText?: boolean }[]> = {
  local_executable: [
    { verb: 'launch', label: 'Launch' },
    { verb: 'focus', label: 'Bring to front' },
    { verb: 'read_window', label: 'Read window' },
    { verb: 'type_text', label: 'Type', needsText: true },
    { verb: 'close', label: 'Close' },
  ],
  web_session: [
    { verb: 'open', label: 'Open' },
    { verb: 'read_page', label: 'Read the page' },
  ],
};

/**
 * Which of those tables applies to this entry.
 *
 * Not simply `CONTROL_VERBS[entry.strategy]`, because a connector whose declared
 * strategy is `oauth2` or `api_key` but which was connected by signing in at its
 * website is driven through the browser exactly like a `web_session` one — the
 * backend widens its capabilities the same way. Keying on strategy alone would
 * connect X in one click and then offer nothing to do with it.
 */
function verbsFor(entry: AppEntry): { verb: string; label: string; needsText?: boolean }[] {
  if (entry.strategy === 'local_executable') return CONTROL_VERBS.local_executable;
  if (drivesBrowser(entry)) return CONTROL_VERBS.web_session;
  return [];
}

/**
 * The control strip on an expanded, connected row.
 *
 * Its own component with its own state because the text to type belongs to one row
 * and nothing else on the page needs it — lifting it to the view would mean a
 * keystroke here re-rendering thirty rows.
 *
 * Every one of these buttons has a real effect on this machine: `launch` starts a
 * program, `type_text` sends keystrokes to whatever window is focused. So the strip
 * only appears once the connection is already `connected`, and the result reported
 * is the backend's own sentence — including its failures, like Chrome refusing a
 * second window on one profile.
 */
const ControlStrip = memo(function ControlStrip({
  entry,
  onRun,
}: {
  entry: AppEntry;
  onRun: (entry: AppEntry, verb: string, text: string) => Promise<string>;
}) {
  const verbs = verbsFor(entry);
  const [text, setText] = useState('');
  const [running, setRunning] = useState('');
  const [result, setResult] = useState('');

  if (!verbs.length) return null;

  const run = async (verb: string, needsText: boolean) => {
    if (needsText && !text.trim()) {
      setResult('Type something first — this sends real keystrokes.');
      return;
    }
    setRunning(verb);
    setResult('');
    try {
      setResult(await onRun(entry, verb, needsText ? text : ''));
    } finally {
      setRunning('');
    }
  };

  return (
    <div className="space-y-2 rounded-lg border border-white/10 bg-black/20 p-2.5">
      <p className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.14em] text-white/35">
        <Play className="h-3 w-3" />
        Drive it
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        {verbs.map(({ verb, label, needsText }) => (
          <button
            key={verb}
            type="button"
            onClick={() => void run(verb, Boolean(needsText))}
            disabled={Boolean(running)}
            className="flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.05] px-2 py-1 text-[11px] text-white/75 transition-colors hover:border-cyan-400/40 hover:text-cyan-100 disabled:opacity-40"
          >
            {running === verb ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            {label}
          </button>
        ))}
      </div>
      {verbs.some((entryVerb) => entryVerb.needsText) ? (
        <input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Text to type into it"
          spellCheck={false}
          className="w-full rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-[11px] text-white/85 outline-none focus:border-cyan-400/50"
        />
      ) : null}
      {result ? (
        <p className="whitespace-pre-wrap break-words text-[11px] leading-relaxed text-white/55">
          {result}
        </p>
      ) : null}
    </div>
  );
});

/** A section heading in the directory's own voice: label, count, optional action. */
function SectionHeading({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children?: React.ReactNode;
}) {
  return (
    <div className="mb-1.5 flex items-center justify-between gap-3 px-1">
      <h2 className="flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-[0.14em] text-white/45">
        {title}
        {typeof count === 'number' ? (
          <span className="rounded-full bg-white/[0.07] px-1.5 py-0.5 text-[10px] font-medium normal-case tracking-normal text-white/45">
            {count}
          </span>
        ) : null}
      </h2>
      {children}
    </div>
  );
}

export default function ConnectionsDirectory() {
  const [catalog, setCatalog] = useState<AppCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busyId, setBusyId] = useState('');
  const [expandedId, setExpandedId] = useState('');
  const [editingId, setEditingId] = useState('');
  const [draft, setDraft] = useState<Record<string, string>>(EMPTY_DRAFT);
  const [query, setQuery] = useState('');
  const [discovered, setDiscovered] = useState<DiscoveredApp[]>([]);
  const [scanning, setScanning] = useState(false);
  const [scanOpen, setScanOpen] = useState(false);
  const [siteUrl, setSiteUrl] = useState('');
  const [addingSite, setAddingSite] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setCatalog(await fetchAppCatalog(true));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not reach the backend.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Patch one entry's status in place.
   *
   * Re-fetching the catalog after every click would re-probe thirty connectors —
   * filesystem lookups, HTTP HEADs and a cookie-file read — to update one row, and
   * would collapse the row the user just expanded. The endpoint already returns the
   * new status for exactly the app that changed.
   */
  const applyStatus = useCallback((appId: string, status: AppStatus) => {
    setCatalog((current) =>
      current
        ? {
            ...current,
            apps: current.apps.map((entry) =>
              entry.app_id === appId ? { ...entry, status } : entry
            ),
          }
        : current
    );
  }, []);

  /**
   * Run one connect-ish call and report what the backend actually said.
   *
   * The `detail` sentence is the whole value of the seven-outcome design — "Add your
   * workspace token, from Slack → Apps → OAuth" is actionable and "Connected" or
   * "Failed" is not. So the toast carries `detail`, never a generic string.
   */
  const runAndReport = useCallback(
    async (appId: string, call: () => Promise<AppStatus>) => {
      setBusyId(appId);
      try {
        const status = await call();
        applyStatus(appId, status);
        const tone = OUTCOME_PRESENTATION[status.outcome];
        if (status.outcome === 'connected') toast.success(status.detail);
        else if (status.outcome === 'disconnected') toast.message(status.detail);
        else toast.warning(`${tone.label} — ${status.detail}`);
        return status;
      } catch (cause) {
        toast.error(cause instanceof Error ? cause.message : 'That did not work.');
        return null;
      } finally {
        setBusyId('');
      }
    },
    [applyStatus]
  );

  const handleToggle = useCallback((entry: AppEntry) => {
    setExpandedId((current) => (current === entry.app_id ? '' : entry.app_id));
  }, []);

  const handleDraftChange = useCallback((key: string, value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
  }, []);

  const handleCancel = useCallback(() => {
    setEditingId('');
    setDraft(EMPTY_DRAFT);
  }, []);

  /** The secondary route: open the credential form on an entry that offers both. */
  const handleUseKeys = useCallback((entry: AppEntry) => {
    setEditingId(entry.app_id);
    setExpandedId(entry.app_id);
    setDraft(EMPTY_DRAFT);
  }, []);

  const handleSubmit = useCallback(
    async (entry: AppEntry) => {
      const status = await runAndReport(entry.app_id, () =>
        saveAppCredentials(entry.app_id, draft)
      );
      if (status && status.outcome === 'connected') handleCancel();
    },
    [draft, handleCancel, runAndReport]
  );

  /** Drop a row from the list. Only adopted entries can leave — see `handleAct`. */
  const forget = useCallback((appId: string) => {
    setCatalog((current) =>
      current ? { ...current, apps: current.apps.filter((e) => e.app_id !== appId) } : current
    );
    setDiscovered((current) =>
      current.map((app) => (app.app_id === appId ? { ...app, adopted: false } : app))
    );
    setExpandedId((current) => (current === appId ? '' : current));
  }, []);

  /**
   * The single button on a row, dispatched on `actionFor`'s kind.
   *
   * `signin` is the interesting one. It does not authenticate anything itself — it
   * opens the site in a browser window Akansha owns and gets out of the way. The
   * cookie the person's own login leaves behind in that profile *is* the connection,
   * which is why the next step is "press Re-check when you're done" rather than a
   * status change here: this app cannot know when a login on someone else's domain
   * finished.
   */
  const handleAct = useCallback(
    async (entry: AppEntry) => {
      const { kind } = actionFor(entry);
      if (kind === 'input') {
        setEditingId(entry.app_id);
        setExpandedId(entry.app_id);
        setDraft(EMPTY_DRAFT);
        return;
      }
      if (kind === 'install') {
        setExpandedId(entry.app_id);
        if (entry.docs_url) window.open(entry.docs_url, '_blank', 'noreferrer');
        else toast.message(entry.status?.detail ?? 'Not installed on this machine.');
        return;
      }
      if (kind === 'signin') {
        setBusyId(entry.app_id);
        try {
          const result = await signInToSite(entry.app_id);
          if (result.ok) {
            toast.success(result.detail, { duration: 10000 });
            setExpandedId(entry.app_id);
          } else {
            toast.error(result.detail);
          }
        } catch (cause) {
          toast.error(cause instanceof Error ? cause.message : 'Could not open a browser.');
        } finally {
          setBusyId('');
        }
        return;
      }
      if (kind === 'remove') {
        setBusyId(entry.app_id);
        try {
          const status = await disconnectApp(entry.app_id);
          toast.message(status.detail);
          forget(entry.app_id);
        } catch (cause) {
          toast.error(cause instanceof Error ? cause.message : 'Could not remove it.');
        } finally {
          setBusyId('');
        }
        return;
      }
      if (kind === 'disconnect') {
        await runAndReport(entry.app_id, () => disconnectApp(entry.app_id));
        return;
      }
      // `authorize` and `recheck` are the same call: the backend decides whether the
      // honest next step is an approval URL or simply another probe.
      const status = await runAndReport(entry.app_id, () => connectApp(entry.app_id));
      if (status?.authorize_url) window.open(status.authorize_url, '_blank', 'noreferrer');
    },
    [forget, runAndReport]
  );

  /**
   * Run one verb against a connected app and hand back the sentence to show.
   *
   * Returned rather than toasted: the answer to "read the page" is up to eight
   * thousand characters of text, and a toast is the wrong place for it. The row keeps
   * it. Failures come back as text too — the backend's own words, so "close the
   * sign-in window, then try again" reaches the person who has to do it.
   */
  const handleControl = useCallback(
    async (entry: AppEntry, verb: string, text: string): Promise<string> => {
      try {
        const result = await controlApp(entry.app_id, { verb, text });
        if (!result.ok) return result.detail;
        return result.text ? `${result.detail}\n\n${result.text.slice(0, 1200)}` : result.detail;
      } catch (cause) {
        return cause instanceof Error ? cause.message : 'That did not work.';
      }
    },
    []
  );

  const handleScan = useCallback(async () => {
    setScanning(true);
    setScanOpen(true);
    try {
      const result = await discoverApps();
      setDiscovered(result.apps);
      toast.message(
        `${result.count} application${result.count === 1 ? '' : 's'} on this PC · ${result.adopted_count} connected`
      );
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : 'Could not read the Start Menu.');
    } finally {
      setScanning(false);
    }
  }, []);

  /** Merge a newly adopted entry into the list, replacing any row with the same id. */
  const absorb = useCallback((entry: AppEntry) => {
    setCatalog((current) => {
      if (!current) return current;
      const others = current.apps.filter((e) => e.app_id !== entry.app_id);
      return { ...current, apps: [...others, entry] };
    });
  }, []);

  const handleAdopt = useCallback(
    async (app: DiscoveredApp) => {
      setBusyId(app.app_id);
      try {
        const result = await adoptApp({
          kind: 'desktop',
          label: app.label,
          target: app.launch_target,
        });
        absorb(result);
        setDiscovered((current) =>
          current.map((item) => (item.app_id === app.app_id ? { ...item, adopted: true } : item))
        );
        toast.success(result.detail ?? `${result.label} is connected.`);
      } catch (cause) {
        toast.error(cause instanceof Error ? cause.message : 'Could not connect that app.');
      } finally {
        setBusyId('');
      }
    },
    [absorb]
  );

  /**
   * "One click authenticate any website": register the site, then open it.
   *
   * Both halves in one click because splitting them would leave a row that says
   * "Sign in once" and a person wondering what they were meant to do — and because
   * registering a site without ever opening it accomplishes nothing. The sign-in
   * itself happens in a real browser window; no password is typed into this app.
   */
  const handleAddSite = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      const target = siteUrl.trim();
      if (!target) return;
      setAddingSite(true);
      try {
        const entry = await adoptApp({ kind: 'web', target });
        absorb(entry);
        setSiteUrl('');
        setExpandedId(entry.app_id);
        const opened = await signInToSite(entry.app_id);
        if (opened.ok) toast.success(opened.detail, { duration: 10000 });
        else toast.warning(`${entry.label} was added, but: ${opened.detail}`);
      } catch (cause) {
        toast.error(cause instanceof Error ? cause.message : 'Could not add that site.');
      } finally {
        setAddingSite(false);
      }
    },
    [absorb, siteUrl]
  );

  /**
   * The list, memoised on the catalog rather than re-derived per render.
   *
   * `catalog?.apps ?? []` inline would allocate a fresh array every render, which
   * makes it a changed dependency for both memos below — so the search filter and
   * the connected-strip filter would run on every keystroke *and* every toast, over
   * every entry. The empty-array fallback is the whole reason: `undefined` is stable,
   * `[]` is not.
   */
  const entries = useMemo(() => catalog?.apps ?? [], [catalog]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter(
      (entry) =>
        entry.label.toLowerCase().includes(needle) ||
        entry.app_id.toLowerCase().includes(needle) ||
        familyLabel(entry.family).toLowerCase().includes(needle) ||
        entry.capabilities.some((capability) => capability.toLowerCase().includes(needle))
    );
  }, [entries, query]);

  const connected = useMemo(
    () => entries.filter((entry) => entry.status?.outcome === 'connected'),
    [entries]
  );

  /** Grouped by category, in `FAMILY_ORDER`, unlisted families alphabetically after. */
  const sections = useMemo(() => {
    const buckets = new Map<string, AppEntry[]>();
    for (const entry of visible) {
      const bucket = buckets.get(entry.family);
      if (bucket) bucket.push(entry);
      else buckets.set(entry.family, [entry]);
    }
    return [...buckets.entries()]
      .sort(([left], [right]) => {
        const leftIndex = FAMILY_ORDER.indexOf(left);
        const rightIndex = FAMILY_ORDER.indexOf(right);
        if (leftIndex !== -1 && rightIndex !== -1) return leftIndex - rightIndex;
        if (leftIndex !== -1) return -1;
        if (rightIndex !== -1) return 1;
        return left.localeCompare(right);
      })
      .map(([family, apps]) => ({
        family,
        apps: [...apps].sort((left, right) => left.label.localeCompare(right.label)),
      }));
  }, [visible]);

  const scanMatches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return discovered;
    return discovered.filter((app) => app.label.toLowerCase().includes(needle));
  }, [discovered, query]);

  const pending = discovered.filter((app) => !app.adopted && !app.declared).length;

  return (
    <div className="space-y-7 pb-16">
      <header className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-white">Connections</h1>
            <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-white/50">
              Anything on this PC, and any website. Desktop apps connect in one click — there is
              nothing to log into. A website opens in Akansha&apos;s own browser window so you sign
              in yourself; your password never passes through this app.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1.5 text-[12px] text-white/60 transition-colors hover:border-white/25 hover:text-white/90 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Re-check all
          </button>
        </div>

        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
          <label className="flex items-center gap-2 rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2">
            <Search className="h-4 w-4 shrink-0 text-white/35" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search connections and apps on this PC"
              spellCheck={false}
              className="w-full bg-transparent text-[13px] text-white/85 outline-none placeholder:text-white/30"
            />
          </label>

          <form
            onSubmit={handleAddSite}
            className="flex items-center gap-2 rounded-xl border border-white/10 bg-white/[0.04] px-3 py-2"
          >
            <Globe className="h-4 w-4 shrink-0 text-white/35" />
            <input
              value={siteUrl}
              onChange={(event) => setSiteUrl(event.target.value)}
              placeholder="Connect any website — mail.google.com, github.com…"
              spellCheck={false}
              autoComplete="off"
              className="w-full bg-transparent text-[13px] text-white/85 outline-none placeholder:text-white/30"
            />
            <button
              type="submit"
              disabled={addingSite || !siteUrl.trim()}
              className="flex shrink-0 items-center gap-1.5 rounded-lg border border-cyan-400/40 bg-cyan-500/10 px-2.5 py-1 text-[11px] font-medium text-cyan-100 transition-colors hover:bg-cyan-500/20 disabled:border-white/10 disabled:bg-transparent disabled:text-white/30"
            >
              {addingSite ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Plus className="h-3.5 w-3.5" />
              )}
              Connect &amp; sign in
            </button>
          </form>
        </div>
      </header>

      {error ? (
        <p className="flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-200">
          <AlertTriangle className="mt-[2px] h-4 w-4 shrink-0" />
          {error}
        </p>
      ) : null}

      {connected.length ? (
        <section>
          <SectionHeading title="Connected" count={connected.length} />
          <div className="flex gap-2 overflow-x-auto pb-2">
            {connected.map((entry) => (
              <button
                key={entry.app_id}
                type="button"
                onClick={() => handleToggle(entry)}
                title={statusLine(entry)}
                className="flex w-[104px] shrink-0 flex-col items-center gap-2 rounded-xl border border-white/10 bg-white/[0.04] px-2 py-3 text-center transition-colors hover:border-emerald-400/30 hover:bg-white/[0.07]"
              >
                <Tile appId={entry.app_id} label={entry.label} large />
                <span className="w-full truncate text-[11px] font-medium text-white/80">
                  {entry.label}
                </span>
                <span className="flex items-center gap-1 text-[10px] text-emerald-300/80">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  Live
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <section>
        {/* "Add from this PC", not "On this PC": the family section below carries that
            label for apps already adopted, and two identical headings on one page is
            a reader wondering which of them is the real one. */}
        <SectionHeading title="Add from this PC" count={discovered.length || undefined}>
          <div className="flex items-center gap-2">
            {discovered.length ? (
              <button
                type="button"
                onClick={() => setScanOpen((open) => !open)}
                className="flex items-center gap-1 text-[11px] text-white/45 hover:text-white/80"
              >
                {scanOpen ? 'Hide' : `Show ${pending} not connected`}
                <ChevronRight
                  className={`h-3 w-3 transition-transform ${scanOpen ? 'rotate-90' : ''}`}
                />
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => void handleScan()}
              disabled={scanning}
              className="flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1 text-[11px] text-white/60 transition-colors hover:border-cyan-400/40 hover:text-cyan-200 disabled:opacity-50"
            >
              {scanning ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <MonitorSmartphone className="h-3.5 w-3.5" />
              )}
              {discovered.length ? 'Scan again' : 'Scan this PC'}
            </button>
          </div>
        </SectionHeading>

        {!discovered.length ? (
          <p className="rounded-xl border border-dashed border-white/10 px-3 py-4 text-[12px] text-white/40">
            Nothing scanned yet. Reading the Start Menu finds every application installed on this
            machine — it only reads shortcut paths, and connects nothing until you press a{' '}
            <Plus className="mb-[2px] inline h-3 w-3" />.
          </p>
        ) : scanOpen ? (
          <div className="grid gap-x-4 md:grid-cols-2">
            {scanMatches.map((app) => (
              <DiscoveredRow
                key={app.app_id}
                app={app}
                busy={busyId === app.app_id}
                onAdopt={handleAdopt}
              />
            ))}
            {!scanMatches.length ? (
              <p className="px-1 py-2 text-[12px] text-white/40">
                No application on this PC matches “{query}”.
              </p>
            ) : null}
          </div>
        ) : null}
      </section>

      {loading && !entries.length ? (
        <p className="flex items-center gap-2 px-1 text-[12px] text-white/45">
          <Loader2 className="h-4 w-4 animate-spin" />
          Probing every connection…
        </p>
      ) : null}

      {sections.map(({ family, apps }) => (
        <section key={family}>
          <SectionHeading title={familyLabel(family)} count={apps.length} />
          <div className="grid gap-x-4 md:grid-cols-2">
            {apps.map((entry) => (
              <AppRow
                key={entry.app_id}
                entry={entry}
                busy={busyId === entry.app_id}
                expanded={expandedId === entry.app_id}
                editing={editingId === entry.app_id}
                draft={draft}
                onToggle={handleToggle}
                onAct={handleAct}
                onUseKeys={handleUseKeys}
                onCancel={handleCancel}
                onDraftChange={handleDraftChange}
                onSubmit={handleSubmit}
                onControl={handleControl}
              />
            ))}
          </div>
        </section>
      ))}

      {!loading && !sections.length && !error ? (
        <p className="rounded-xl border border-dashed border-white/10 px-3 py-5 text-center text-[12px] text-white/40">
          Nothing matches “{query}”. Try scanning this PC, or paste a website address above.
        </p>
      ) : null}

      <footer className="flex flex-wrap items-start gap-x-6 gap-y-2 rounded-xl border border-white/[0.07] bg-white/[0.02] px-3 py-3 text-[11px] text-white/40">
        <span className="flex items-center gap-1.5">
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-400/70" />
          Credentials are encrypted on this machine and never leave it except to the service they
          belong to.
        </span>
        <span className="flex items-center gap-1.5">
          <Globe className="h-3.5 w-3.5 text-white/30" />
          Websites sign in in a browser window you can see. This app never asks for a site password.
        </span>
        {catalog ? <span className="ml-auto">Catalog v{catalog.version}</span> : null}
      </footer>
    </div>
  );
}
