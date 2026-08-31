/**
 * The client half of the one-click connect registry (`backend/app_connect.py`).
 *
 * There is one reason this file exists rather than the page calling `fetch`
 * directly: the six outcomes. The backend deliberately refuses to answer a
 * connect attempt with a boolean, because "not connected" is the answer that
 * made the old integrations surface useless — a missing API key, an application
 * that is not installed, and a self-hosted bridge that is not running are three
 * different problems and only the first is fixable by typing. If the UI collapses
 * them back into connected/disconnected, the backend's whole design is wasted, so
 * the outcome union is declared once, here, and `OUTCOME_PRESENTATION` forces a
 * distinct label and colour for each member. Adding a seventh outcome to the
 * backend without giving it presentation here is a TypeScript error rather than a
 * card that silently renders as a failure.
 *
 * Nothing in this module holds a credential. The catalog ships field
 * *descriptions* — key, label, where to find the value, whether it is secret —
 * and never values, masked or otherwise. That is why there is no reveal toggle
 * and no copy button anywhere in this surface: the page it replaced had both, and
 * both operated on hardcoded bullet characters.
 */

import { apiUrl } from './apiBase';

export type AppOutcome =
  | 'connected'
  | 'needs_input'
  | 'needs_authorization'
  | 'needs_sign_in'
  | 'not_installed'
  | 'unreachable'
  | 'disconnected';

export type AppStrategy =
  | 'local_executable'
  | 'api_key'
  | 'oauth2'
  | 'local_bridge'
  | 'web_session';

/** A field the user may have to fill in. Description only — never a value. */
export interface AppCredentialField {
  key: string;
  label: string;
  /** Where to obtain the value. Load-bearing: the usual reason an integration
   *  stalls is that the user cannot find what is being asked for. */
  where: string;
  secret: boolean;
  optional: boolean;
}

export interface AppStatus {
  app_id: string;
  outcome: AppOutcome;
  connected: boolean;
  detail: string;
  required?: AppCredentialField[];
  authorize_url?: string;
  connected_to?: string;
}

export interface AppEntry {
  app_id: string;
  label: string;
  family: string;
  strategy: AppStrategy;
  capabilities: string[];
  docs_url: string;
  safety_note: string;
  fields: AppCredentialField[];
  /** Added at runtime by scanning this PC or pasting a URL, rather than declared
   *  in the registry. Only these can be removed — a built-in entry has nothing to
   *  remove, it is simply installed or not. */
  adopted: boolean;
  /** `web_session` only. */
  site_url: string;
  /** The site this app can be connected to by simply signing in, when its declared
   *  route is a credential. Set wherever being logged in as the user genuinely
   *  enables the listed capabilities, and empty otherwise — a cookie at
   *  chatgpt.com does not let this application request a completion, so the model
   *  providers have none and still ask for a key.
   *
   *  When this is set the backend reports `needs_sign_in` rather than
   *  `needs_input`, which is the whole point: the fields are still offered, but as
   *  the second route rather than the only one. */
  web_url: string;
  /** Absent when the catalog was fetched with `probe=false`. */
  status?: AppStatus;
}

export interface AppCatalog {
  version: number;
  families: string[];
  strategies: string[];
  outcomes: string[];
  apps: AppEntry[];
}

/**
 * How each outcome is shown. Keyed by the full union, so the compiler rejects a
 * new backend outcome that has no presentation rather than letting it fall
 * through to a generic error state.
 *
 * Presentation only — the button label lives in `actionFor`, because the right
 * action depends on the connector's strategy as well as its outcome.
 */
export const OUTCOME_PRESENTATION: Record<
  AppOutcome,
  { label: string; tone: string; dot: string }
> = {
  connected: {
    label: 'Connected',
    tone: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30',
    dot: 'bg-emerald-400',
  },
  needs_input: {
    label: 'Needs details',
    tone: 'bg-amber-500/10 text-amber-300 border-amber-500/30',
    dot: 'bg-amber-400',
  },
  needs_authorization: {
    label: 'Needs your approval',
    tone: 'bg-sky-500/10 text-sky-300 border-sky-500/30',
    dot: 'bg-sky-400',
  },
  needs_sign_in: {
    label: 'Sign in once',
    tone: 'bg-violet-500/10 text-violet-300 border-violet-500/30',
    dot: 'bg-violet-400',
  },
  not_installed: {
    label: 'Not installed',
    tone: 'bg-slate-500/10 text-slate-300 border-slate-500/30',
    dot: 'bg-slate-400',
  },
  unreachable: {
    label: 'Server not answering',
    tone: 'bg-rose-500/10 text-rose-300 border-rose-500/30',
    dot: 'bg-rose-400',
  },
  disconnected: {
    label: 'Disconnected',
    tone: 'bg-slate-500/10 text-slate-300 border-slate-500/30',
    dot: 'bg-slate-500',
  },
};

export const FAMILY_LABELS: Record<string, string> = {
  desktop: 'Desktop apps',
  messaging: 'Messaging',
  model_provider: 'AI models',
  productivity: 'Productivity',
  developer: 'Developer',
  media: 'Media',
  website: 'Websites',
  on_this_pc: 'On this PC',
};

export const STRATEGY_LABELS: Record<AppStrategy, string> = {
  local_executable: 'Installed on this PC',
  api_key: 'API key',
  oauth2: 'Sign in with the provider',
  local_bridge: 'Self-hosted server',
  web_session: 'You sign in, in a browser',
};

export function familyLabel(family: string): string {
  return FAMILY_LABELS[family] ?? family;
}

/**
 * How this entry actually connects, which is not always what its strategy says.
 *
 * An `oauth2` connector carrying a `web_url` reads "Sign in with the provider",
 * and that sentence is what the user's complaint was about — it sounds like one
 * click and then asks for a client id and a client secret. Where the browser route
 * exists it is the route, so it is what gets named.
 */
export function routeLabel(entry: AppEntry): string {
  if (entry.web_url && entry.strategy !== 'web_session') return 'You sign in, in a browser';
  return STRATEGY_LABELS[entry.strategy] ?? entry.strategy;
}

/** The browser verbs, mirroring `WEB_VERBS` in `backend/app_control.py`. */
export const WEB_VERBS = ['open', 'navigate', 'type', 'click', 'read_page'];

/** True when this entry can be driven through Akansha's browser window. */
export function drivesBrowser(entry: AppEntry): boolean {
  return entry.strategy === 'web_session' || Boolean(entry.web_url);
}

/**
 * True when what makes this entry connected is a cookie rather than something
 * stored here. The backend answers that by reporting the site it signed into as
 * `connected_to`, so this compares against it rather than guessing from strategy.
 */
export function connectedInBrowser(entry: AppEntry): boolean {
  const target = entry.status?.connected_to ?? '';
  if (!target) return false;
  return target === entry.site_url || target === entry.web_url;
}

/** What the card's primary button does. */
export type AppActionKind =
  | 'input'
  | 'authorize'
  | 'signin'
  | 'install'
  | 'recheck'
  | 'disconnect'
  | 'remove';

/**
 * The one action a card offers, decided by outcome *and* strategy.
 *
 * Outcome alone is not enough for `connected`. A stored API key can be forgotten,
 * so "Disconnect" is real there; an installed desktop application has no
 * credential to forget, so offering to disconnect Chrome was an action that could
 * not do what it said — it deleted a row that never existed, answered
 * "disconnected", and then read `connected` again on the next probe because Chrome
 * is still installed. For that strategy the only honest action is to look again.
 *
 * `adopted` is the third input, and for the same reason. An app the user added by
 * clicking it in a scan *can* be taken away again — that is the only thing removing
 * it means, since there was never a credential — so the honest word is "Remove",
 * not "Disconnect".
 */
export function actionFor(entry: AppEntry): { kind: AppActionKind; label: string } {
  const outcome = entry.status?.outcome ?? 'disconnected';
  if (outcome === 'needs_sign_in') return { kind: 'signin', label: 'Sign in' };
  if (outcome === 'connected') {
    if (entry.adopted) return { kind: 'remove', label: 'Remove' };
    // A browser session is not ours to delete either. What connects these is a
    // cookie the site set in Akansha's profile, so "Disconnect" here would delete
    // an `integration_connections` row that does not exist and then read
    // `connected` again on the next probe — the same broken action that offering to
    // disconnect Chrome used to be. Signing out happens in the window itself.
    if (entry.strategy === 'local_executable' || connectedInBrowser(entry)) {
      return { kind: 'recheck', label: 'Re-check' };
    }
    return { kind: 'disconnect', label: 'Disconnect' };
  }
  if (outcome === 'needs_authorization') return { kind: 'authorize', label: 'Authorize' };
  if (outcome === 'not_installed') {
    return entry.adopted
      ? { kind: 'remove', label: 'Remove' }
      : { kind: 'install', label: 'Get the app' };
  }
  if (outcome === 'needs_input') return { kind: 'input', label: 'Add details' };
  // `unreachable`, and `disconnected` on anything with fields to fill in.
  if (outcome === 'disconnected' && entry.fields.length) {
    return { kind: 'input', label: 'Connect' };
  }
  return { kind: 'recheck', label: outcome === 'unreachable' ? 'Try again' : 'Connect' };
}

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    // The backend puts a sentence in `detail` for every rejection — a 403 from a
    // non-owner turn, a 400 naming an unknown field. Surfacing the status code
    // alone would throw that sentence away.
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return payload as T;
}

export async function fetchAppCatalog(probe = true): Promise<AppCatalog> {
  const response = await fetch(apiUrl(`/api/apps/catalog?probe=${probe ? 'true' : 'false'}`), {
    cache: 'no-store',
  });
  return readJson<AppCatalog>(response);
}

export async function connectApp(appId: string): Promise<AppStatus> {
  const response = await fetch(apiUrl(`/api/apps/connect/${encodeURIComponent(appId)}`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return readJson<AppStatus>(response);
}

export async function saveAppCredentials(
  appId: string,
  config: Record<string, string>
): Promise<AppStatus> {
  const response = await fetch(apiUrl(`/api/apps/credentials/${encodeURIComponent(appId)}`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config }),
  });
  return readJson<AppStatus>(response);
}

export async function disconnectApp(appId: string): Promise<AppStatus> {
  const response = await fetch(apiUrl(`/api/apps/disconnect/${encodeURIComponent(appId)}`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return readJson<AppStatus>(response);
}

// --- Adopting whatever is actually on this machine, and any site -------------

/** One application found by scanning the Start Menu. */
export interface DiscoveredApp {
  app_id: string;
  label: string;
  launch_target: string;
  exe_path: string;
  source: string;
  adopted: boolean;
  /** True only when a built-in entry covers this app *and currently works*. A
   *  declared entry that reports "not installed" does not get to claim the app —
   *  that is how the old page kept insisting Discord was missing while its
   *  shortcut sat in the Start Menu. */
  declared: boolean;
  declared_as: string;
}

export interface DiscoverResult {
  apps: DiscoveredApp[];
  count: number;
  adopted_count: number;
  scanned_at: string;
}

/** An adopted entry plus its state, which is the same shape a catalog row has. */
export type AdoptResult = AppEntry & { already: string; detail?: string };

export async function discoverApps(): Promise<DiscoverResult> {
  const response = await fetch(apiUrl('/api/apps/discover'), { cache: 'no-store' });
  return readJson<DiscoverResult>(response);
}

export async function adoptApp(input: {
  kind: 'desktop' | 'web';
  label?: string;
  target: string;
}): Promise<AdoptResult> {
  const response = await fetch(apiUrl('/api/apps/adopt'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return readJson<AdoptResult>(response);
}

/**
 * Open a site in Akansha's own browser window so the user can sign in.
 *
 * This is the whole of "one-click authenticate any website", and it is the only
 * version of it that is not a lie: no OAuth client can be registered for an
 * arbitrary domain, and a form in this app that asked for a Gmail password would
 * be the worst thing this codebase could offer. The person signs in themselves,
 * in a real browser, and the site's own cookie stays in that profile.
 */
export async function signInToSite(
  appId: string
): Promise<{ app_id: string; ok: boolean; detail: string; profile_dir?: string }> {
  const response = await fetch(apiUrl(`/api/apps/web/login/${encodeURIComponent(appId)}`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return readJson<{ app_id: string; ok: boolean; detail: string; profile_dir?: string }>(response);
}

export async function controlApp(
  appId: string,
  input: { verb: string; selector?: string; text?: string; url?: string }
): Promise<{ app_id: string; verb: string; ok: boolean; detail: string; text?: string }> {
  const response = await fetch(apiUrl(`/api/apps/control/${encodeURIComponent(appId)}`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return readJson<{ app_id: string; verb: string; ok: boolean; detail: string; text?: string }>(
    response
  );
}
