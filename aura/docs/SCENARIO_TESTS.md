# Twenty real-world scenarios — results, root causes, and what changed

Run: `python -m pytest backend -q` → **846 passed, 1 skipped, 647 subtests, 129s**
(the twenty new scenarios are `backend/tests/test_real_world_scenarios.py`, and
they pass in 5.8s on their own). `npx tsc --noEmit` → clean.

Nothing in this pass moved the mouse, launched an app, opened a browser window, or
started the backend. So this note is honest about the difference between *proved in
code* and *measured on screen*, and says which is which for every line.

## What was actually failing

Three defects, all of them behind sentences the user reported verbatim.

### 1. The second browser command in a session did nothing

`site_dispatcher.dispatch_playwright` held a process-global browser and entered it
with `asyncio.run(...)`. `asyncio.run` creates an event loop and **closes it on
return**. The driver object survived to the next request; the CDP transport
registered with that loop did not. The liveness guard consulted
`browser.is_connected()`, which reports on the socket and never asks which loop the
socket belongs to — so it answered `True` and the relaunch never fired.

Symptom from the user's chair: the first site command works, and every command
after it fails or hangs. `agent_modules/domain_executors.py` had the identical
pattern in `_run`, whose three fallbacks all ended in `asyncio.run(coro)`.

**Fix.** `backend/browser/owned_session.py` owns one event loop on a daemon
thread for the life of the process. Synchronous callers submit through
`run_owned(factory)` (`run_coroutine_threadsafe`), so the browser stays bound to a
loop that is still running when the next command arrives. Liveness now compares the
loop the driver launched on against the loop asking — the check that was missing.

Scenarios 5, 6, 7, 8.

### 2. Signing in on /connections never helped a chat command

The connections page promises, in its own words:

> A website opens in Akansha's own browser window so you sign in yourself; your
> password never passes through this app.

It keeps that promise into `app_discovery.browser_profile_dir()` —
`.akansha/akansha-browser-profile`, driven by `app_control` on the **sync**
Playwright API with `launch_persistent_context`.

The chat automation browser was a different browser. `PlaywrightDriver.launch` did
`chromium.launch()` + `new_context()` with **no `user_data_dir`**: an
incognito-equivalent profile, fresh on every launch, that had never heard of the
sign-in. It also spoofed a `user_agent` string onto a real Chrome build, which is a
way to get a profile flagged as inconsistent by exactly the sites worth being
signed into.

**Fix.** `PlaywrightDriver.launch` now opens a persistent context on the sign-in
directory, using the person's own Chrome/Edge/Brave via `app_control.find_browser`
when there is one (a profile built by one executable is not portable to another,
and their passkeys live in theirs). The UA override is gone. `persistent=False`
remains for CI, and it must not touch the real profile.

Scenarios 12, 13, 14, 19.

### 3. Two windows, and no way to tell which one was yours

`BrowserDomainExecutor.__init__` constructed its own `PlaywrightDriver(headless=False)`
beside the dispatcher's singleton — at construction time, whether or not anybody
browsed — and also leaked an `asyncio.new_event_loop()` it never used.

**Fix.** It now holds `owned_session.LazyOwnedDriver`, a handle that resolves to
the shared session on first use. One window, opened when something is asked of it.

Scenarios 9, 10, 11, 17, 18.

## The twenty scenarios

| # | Sentence or situation | Asserts |
|---|---|---|
| 1 | "open youtube" | reaches the real browser, site detected as youtube |
| 2 | "play Varsham songs on youtube" | the song name survives query extraction |
| 3 | "write me a poem about the rain" | no browser opens for a chat turn |
| 4 | "make me a pdf study plan for my exams" | an artifact request opens no browser |
| 5 | "open youtube" then "open leetcode" | both land on one still-running loop |
| 6 | the root cause | `dispatch_playwright` no longer calls `asyncio.run` |
| 7 | driver stranded by a closed loop | recognised as dead, not reused |
| 8 | healthy driver | reused, so the person's tab is not thrown away |
| 9 | two commands | one window, one launch |
| 10 | person closes Akansha's window | next command relaunches, no error |
| 11 | shutdown | the handle is cleared, not left dangling |
| 12 | connect gmail, then "open gmail" | automation and sign-in share one profile dir |
| 13 | the launch arguments | persistent context, real profile, real Chrome, headful |
| 14 | CI | `persistent=False` never touches or locks the real profile |
| 15 | sign-in window still open | actionable sentence, not a lock-file trace |
| 16 | retry after 15 | no leaked node subprocess per attempt |
| 17 | backend start | building the agent executor opens no window |
| 18 | chat command + goal-engine step | both drive the same window |
| 19 | visibility | headful by default; headless is opt-in via env |
| 20 | "who is the prime minister right now" | live-lookup budgets ordered, and the bound is real |

Scenario numbers are stable. Do not renumber when adding more.

## "Failed to fetch" on /connections — diagnosed

The pasted UI showed **Failed to fetch** next to *Connect & sign in*, and *Nothing
scanned yet*.

All nine endpoints the page calls exist server-side: `/api/apps/catalog`,
`/connect/{id}`, `/credentials/{id}`, `/disconnect/{id}`, `/discover`, `/adopt`,
`/web/login/{id}`, `/control/{id}`, `/operate` (`backend/main.py:5810`–`6330`).
CORS is `allow_origins=["*"]`, so it is not an origin rejection. `src/lib/apiBase.ts`
resolves to `http://localhost:8000` unless `NEXT_PUBLIC_API_BASE_URL` says
otherwise.

So `Failed to fetch` is the browser's wording for *connection refused*: **the
backend was not running**. No backend was started in this session or the previous
two — that is consistent with what the screenshot shows, and it is the only
remaining explanation once the routes and CORS are ruled out.

One latent hazard found while ruling CORS out: `allow_origins=["*"]` together with
`allow_credentials=True` is rejected by browsers for any request that actually
sets `credentials: 'include'`. Nothing in `appConnect.ts` does today, so it is not
the current failure — but the first request that does will fail in a way that looks
identical to this one.

## Known tradeoff, stated rather than hidden

Chrome allows one process per user-data-dir. Now that the automation browser holds
Akansha's profile, a `web_action` call made while the automation window is open
collides — and `app_control` already fails that with the sentence the person needs
("close the sign-in window, then try again"). `PlaywrightDriver` now fails the
mirror-image case with the same sentence. The two paths cooperate rather than
crash, but they do not yet share one window: doing that means routing
`app_control`'s injected `runner=` through the owned session, which is a separate
change to a separately tested module.

## Not verified, and not claimed

- **No end-to-end latency number.** The budgets and the heartbeat are bounds in
  code plus source-level regressions, not a stopwatch reading. Starting the backend
  runs the scheduled-automation runner, which can launch real apps.
- **No real browser was opened.** Scenarios 13–16 assert what we *ask* Chromium
  for, with Playwright faked at its front door. The persistent profile is proved to
  be requested; it is not proved to have loaded a signed-in page.
- **Flake to watch:** `backend/tests/test_avatar_stream.py::test_speak_audio_does_not_resynthesize`
  failed once in a full run in a previous window and passed in isolation and in
  every run since, including both runs today. Undiagnosed; it touches TTS, which
  nothing in this pass changed.
