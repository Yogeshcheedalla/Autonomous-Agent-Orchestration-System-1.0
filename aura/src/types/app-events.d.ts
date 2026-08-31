/**
 * The app's own `window` CustomEvents, declared so the compiler knows them.
 *
 * Five events are dispatched by string literal across ChatThread, ChatWorkspace,
 * ConversationSidebar, ConversationHistoryScreen and Sidebar, and listened for by
 * string literal somewhere else. Nothing tied the two ends together: a typo in
 * either name produced a listener that never fires and a dispatch nobody hears,
 * with no error at build time and no error at runtime — just a button that
 * quietly stops working. Augmenting `WindowEventMap` makes the name a checked
 * identifier and gives every handler a typed `detail` instead of `(e: any)`.
 *
 * `detail` types come from the dispatch sites, not from what a listener wishes
 * were there:
 *   - `akansha-apply-prompt`  ChatWorkspace sends the prompt text.
 *   - `akansha-new-chat`      Sidebar sends the new session id; ChatThread
 *                             dispatches it bare, hence `| undefined`.
 *   - `akansha-toggle-panel`  Sidebar sends a panel key ('context').
 *   - `akansha-select-session` Sidebar sends the session id to switch to.
 *   - `akansha-toggle-automation` ChatThread's live status badge opens the
 *                             automation panel that replaced /browser-automation.
 *   - the other two carry no payload.
 *
 * No imports or exports on purpose — that keeps this a global script file so the
 * interface below merges with the real `WindowEventMap` rather than declaring an
 * unrelated one inside a module.
 */

interface WindowEventMap {
  'akansha-apply-prompt': CustomEvent<string>;
  'akansha-history-updated': CustomEvent<undefined>;
  'akansha-new-chat': CustomEvent<string | undefined>;
  'akansha-select-session': CustomEvent<string>;
  'akansha-toggle-automation': CustomEvent<undefined>;
  'akansha-toggle-panel': CustomEvent<string>;
  'akansha-toggle-prompts': CustomEvent<undefined>;
}
