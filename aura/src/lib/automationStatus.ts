/**
 * What the chat header is allowed to claim about automation.
 *
 * The header used to read "6 automation permissions active" on every load, in
 * every condition. That number was `Object.keys(BROWSER_AUTOMATION_DEFAULTS)`
 * counted client-side: six hardcoded `true`s in a dict literal on the server. It
 * would have said "6 active" on a machine with no screen, and it said nothing at
 * all about whether a single click could be delivered — which is what the person
 * reading it wants to know. Reported as *"there are six automation permissions
 * active. It is not live, actually, so all the things should be live."*
 *
 * `/api/automation/browser/status` now probes the GUI stack per request and
 * returns a `runtime` block. This module turns that into the one line the header
 * shows, and lives outside React so `backend/test_audit_regressions.py` can drive
 * the real shipped file through Node — the same treatment `presenceBehavior.ts`
 * and `wakeWord.ts` get, for the same reason: a re-implementation in the test
 * would assert what I believe the wording rules are while the shipped file said
 * something else.
 *
 * The ordering rule is the whole point: **capability outranks preference.** Six
 * granted permissions with no reachable screen is not "6 active", it is offline.
 */

export interface AutomationRuntime {
  /** Screen and window manager both answered. Every click needs both. */
  gui_control: boolean;
  screen: { width: number; height: number } | null;
  /** Why the screen probe failed, when it did. Shown to the user verbatim. */
  screen_error: string | null;
  window_manager: boolean;
  /** The detached child that timed runs are spawned as. */
  runner_available: boolean;
  scheduled_total: number;
  /** Saved runs whose time has already passed. Non-zero means something stuck. */
  scheduled_due: number;
  checked_at: string;
}

export interface AutomationStatus {
  permissions: Record<string, boolean>;
  /** Keys the owner actually turned on, as opposed to inherited defaults. */
  granted?: string[];
  /** Keys that are only on because the defaults table says so. */
  defaulted?: string[];
  runtime?: AutomationRuntime;
  scheduled_actions?: unknown[];
}

/** How the badge should read, and how alarmed it should look. */
export type AutomationTone = 'live' | 'degraded' | 'offline' | 'unknown';

export interface AutomationSummary {
  tone: AutomationTone;
  /** One line, ~40 characters, for the chat header. */
  label: string;
  /** The longer form, for a tooltip. Always says what was measured. */
  detail: string;
  /** Permissions that are on, however they got that way. */
  activeCount: number;
}

/**
 * `null` means the request failed or has not returned.
 *
 * Deliberately not "0 permissions": an unreachable backend is not a revoked
 * permission, and the old code collapsed both into the same silent state.
 */
export function summariseAutomation(status: AutomationStatus | null): AutomationSummary {
  if (!status) {
    return {
      tone: 'unknown',
      label: 'Automation status unavailable',
      detail: 'The automation service did not answer, so nothing can be claimed about it.',
      activeCount: 0,
    };
  }

  const permissions = status.permissions ?? {};
  const activeCount = Object.values(permissions).filter(Boolean).length;
  const runtime = status.runtime;

  // No runtime block at all means an older backend. Say so rather than falling
  // back to the count that caused the complaint.
  if (!runtime) {
    return {
      tone: 'unknown',
      label: `${activeCount} permissions, liveness unknown`,
      detail:
        'This backend does not report a runtime probe, so whether the desktop can actually be ' +
        'driven was not measured.',
      activeCount,
    };
  }

  if (!runtime.gui_control) {
    const because = runtime.screen_error
      ? `Screen probe failed: ${runtime.screen_error}`
      : runtime.window_manager
        ? 'No screen was reported.'
        : 'The window manager did not answer.';
    return {
      tone: 'offline',
      label: 'Automation offline — no desktop control',
      detail: `${because} Permissions are irrelevant until this clears; ${activeCount} are on.`,
      activeCount,
    };
  }

  // Live, but with something worth surfacing. Overdue runs come first: a schedule
  // that has passed without running is the failure a user would otherwise only
  // notice by the thing not having happened.
  if (runtime.scheduled_due > 0) {
    return {
      tone: 'degraded',
      label: `Automation live · ${runtime.scheduled_due} run${
        runtime.scheduled_due === 1 ? '' : 's'
      } overdue`,
      detail:
        `Desktop control is available at ${describeScreen(runtime)}. ` +
        `${runtime.scheduled_due} of ${runtime.scheduled_total} saved runs are past their time — ` +
        'timed runs need the scheduled-run worker to have stayed alive.',
      activeCount,
    };
  }

  if (!runtime.runner_available) {
    return {
      tone: 'degraded',
      label: `Automation live · scheduling unavailable`,
      detail:
        `Desktop control is available at ${describeScreen(runtime)}, but the scheduled-run module ` +
        'is missing, so a custom time cannot be honoured.',
      activeCount,
    };
  }

  const scheduled = runtime.scheduled_total ? ` · ${runtime.scheduled_total} scheduled` : '';
  return {
    tone: 'live',
    label: `Automation live · ${activeCount} permission${activeCount === 1 ? '' : 's'}${scheduled}`,
    detail:
      `Screen ${describeScreen(runtime)}, window manager responding, scheduled-run worker present. ` +
      `Checked ${runtime.checked_at}.`,
    activeCount,
  };
}

function describeScreen(runtime: AutomationRuntime): string {
  return runtime.screen ? `${runtime.screen.width}x${runtime.screen.height}` : 'an unknown size';
}
