"""Twenty real-world scenarios, run against the real code.
==========================================================

Every scenario here is a sentence somebody actually typed at Akansha, or a
consequence of one, and every one of them is checked against the objects that
would run it -- not against a mock of the thing under test. Nothing in this file
starts a browser, moves the mouse, launches an app or opens a socket: the only
seam that is faked is Playwright's own entry point, because the assertion is
about *what we ask Chromium for*, and asking is observable without answering.

The three failures these scenarios were written around, in the order they bite:

1. **The second command in a session did nothing.** `dispatch_playwright` held a
   process-global browser and entered it with `asyncio.run`, which closes the loop
   it made. The browser survived; its transport did not. `is_connected()` still
   said True, so the relaunch guard never fired.
2. **Signing in never helped.** `/connections` signs the person into
   `app_discovery.browser_profile_dir()`. The chat automation browser used
   `chromium.launch()` with no `user_data_dir` -- a different, empty profile every
   time. Two browsers, one of which had never heard of the sign-in.
3. **Two windows.** `BrowserDomainExecutor` built its own driver beside the
   dispatcher's, at construction time, whether or not anybody browsed.

Scenario numbers are stable; do not renumber them when adding more.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.browser import owned_session
from backend.browser.playwright_driver import PROFILE_IN_USE_DETAIL, PlaywrightDriver
from backend.browser.skills import site_dispatcher


# ── Fakes for Playwright's entry point ────────────────────────────────────────


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_: object) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def title(self) -> str:
        return "fake"


class _FakeContext:
    """Stands in for both a persistent context and a plain context.

    A persistent context *is* the browser handle -- there is no separate Browser
    object in that mode -- so this answers `is_connected` and `close` as well as
    `new_page`, exactly as Playwright's does.
    """

    def __init__(self, pages: int = 0) -> None:
        self.pages = [_FakePage() for _ in range(pages)]
        self.closed = False
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def new_context(self, **_: object) -> "_FakeContext":
        return self

    async def close(self) -> None:
        self.closed = True
        self._connected = False


class _FakeChromium:
    def __init__(self, fail_persistent: str = "") -> None:
        self.persistent_calls: list[tuple[str, dict]] = []
        self.plain_calls: list[dict] = []
        self.fail_persistent = fail_persistent

    async def launch_persistent_context(self, user_data_dir: str, **kwargs: object):
        self.persistent_calls.append((user_data_dir, dict(kwargs)))
        if self.fail_persistent:
            raise RuntimeError(self.fail_persistent)
        return _FakeContext(pages=1)

    async def launch(self, **kwargs: object):
        self.plain_calls.append(dict(kwargs))
        return _FakeContext()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _FakeEntryPoint:
    """What `async_playwright` is: a callable returning an awaitable-starter."""

    def __init__(self, playwright: _FakePlaywright) -> None:
        self._playwright = playwright
        self.started = 0

    def __call__(self) -> "_FakeEntryPoint":
        return self

    async def start(self) -> _FakePlaywright:
        self.started += 1
        return self._playwright


@pytest.fixture
def fake_chromium(monkeypatch, tmp_path):
    """Playwright faked at its front door, with the profile pointed at tmp_path."""
    chromium = _FakeChromium()
    entry = _FakeEntryPoint(_FakePlaywright(chromium))
    monkeypatch.setattr("playwright.async_api.async_playwright", entry)
    monkeypatch.setattr(owned_session, "profile_dir", lambda: tmp_path / "profile")
    monkeypatch.setattr(owned_session, "browser_executable", lambda: r"C:\fake\chrome.exe")
    return chromium


@pytest.fixture(autouse=True)
def _no_leaked_singleton():
    """No scenario may leave a driver behind for the next one to find."""
    yield
    owned_session._driver = None
    owned_session._driver_lock = None


# ── 1-4: does this sentence belong to the browser at all? ─────────────────────


def test_scenario_01_open_youtube_reaches_the_real_browser():
    """"open youtube"

    The narrowest possible site command, and the one that used to answer
    "✅ Opened YouTube" from a headless browser nobody could see. It has no action
    keyword at all, so a router that required one sent it to a Google search
    instead of to YouTube.
    """
    assert site_dispatcher.should_use_playwright("open youtube") is True
    assert site_dispatcher._detect_site("open youtube") == "youtube"


def test_scenario_02_play_a_named_song_keeps_the_song_name():
    """"play Varsham songs on youtube"

    The query has to survive extraction. Dropping it turns a specific request into
    YouTube's front page, which is the kind of near-miss that reads as success.
    """
    prompt = "play Varsham songs on youtube"
    assert site_dispatcher.should_use_playwright(prompt) is True
    query = site_dispatcher._extract_search_query(prompt, "youtube")
    assert "varsham" in query.lower()


def test_scenario_03_a_writing_request_must_not_open_a_browser():
    """"write me a poem about the rain"

    The counterweight to scenario 1. A router keen enough to catch "open youtube"
    with no action word must still leave ordinary conversation alone -- opening a
    browser window in the middle of a chat turn is worse than a slow answer.
    """
    assert site_dispatcher.should_use_playwright("write me a poem about the rain") is False


def test_scenario_04_an_artifact_request_must_not_open_a_browser():
    """"make me a pdf study plan for my exams"

    A file to generate, not a site to visit. This is the prompt shape the frontend
    routes away from automation, and the backend must not undo that decision.
    """
    assert site_dispatcher.should_use_playwright("make me a pdf study plan for my exams") is False


# ── 5-8: the second command in a session ──────────────────────────────────────


def test_scenario_05_two_commands_in_a_row_share_one_live_loop():
    """"open youtube" ... then "open leetcode"

    The headline failure. Two synchronous dispatches must land on the *same* loop
    and that loop must still be running when the second one arrives -- with
    `asyncio.run` it was a fresh loop each time and a closed one in between, which
    is what stranded the shared browser.
    """
    first = owned_session.run_owned(lambda: _which_loop())
    second = owned_session.run_owned(lambda: _which_loop())

    assert first is second
    assert first.is_running()
    assert not first.is_closed()


async def _which_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def test_scenario_06_the_dispatcher_no_longer_closes_its_own_loop():
    """The root cause, pinned in the source.

    Behaviour tests cannot see this one without a real browser: `asyncio.run`
    strands the *transport*, and a fake browser has none to strand. So the anchor
    is the call itself. `asyncio.run(` coming back to this function means every
    site command after the first is broken again.
    """
    source = Path(site_dispatcher.__file__).read_text(encoding="utf-8")
    entry = source.index("def dispatch_playwright(prompt: str)")
    window = source[entry : entry + 1200]
    assert "run_owned(" in window
    assert "asyncio.run(" not in window


def test_scenario_07_a_driver_from_a_dead_loop_is_not_reused():
    """The guard that `is_connected()` could not provide.

    A driver whose `_owned_loop` is not the loop now asking is unusable however
    healthy its socket looks, and the old check consulted only the socket.
    """
    stranded = _StubDriver(loop=asyncio.new_event_loop())

    assert owned_session.run_owned(lambda: _alive(stranded)) is False


def test_scenario_08_a_driver_on_this_loop_is_reused():
    """The other half: liveness must not be so strict that it relaunches always.

    Relaunching a persistent Chrome profile costs seconds and loses the tab the
    person is looking at, so a healthy driver has to be recognised as healthy.
    """

    async def check() -> bool:
        driver = _StubDriver(loop=asyncio.get_running_loop())
        return owned_session._is_alive(driver)

    assert owned_session.run_owned(check) is True


async def _alive(driver: object) -> bool:
    return owned_session._is_alive(driver)


class _StubDriver:
    """The two attributes `_is_alive` reads, and nothing else."""

    def __init__(self, loop: object, connected: bool = True) -> None:
        self._owned_loop = loop
        self._browser = _FakeContext()
        self._browser._connected = connected


# ── 9-12: one browser, and it is the signed-in one ────────────────────────────


def test_scenario_09_one_browser_serves_every_command(fake_chromium):
    """"open youtube" ... then "open github"

    Two commands, one window. `session()` returning a second driver would mean two
    Chromium windows on screen and no way for the person to tell which one their
    sign-in landed in.
    """

    async def twice():
        return await owned_session.session(), await owned_session.session()

    first, second = owned_session.run_owned(twice)

    assert first is second
    assert len(fake_chromium.persistent_calls) == 1


def test_scenario_10_a_dead_browser_is_replaced_not_reported(fake_chromium):
    """The person closed Akansha's window, then asked for another site.

    A closed window must not become an error message. It must become a new window.
    """

    async def close_then_ask():
        first = await owned_session.session()
        await first._browser.close()
        return first, await owned_session.session()

    first, second = owned_session.run_owned(close_then_ask)

    assert second is not first
    assert len(fake_chromium.persistent_calls) == 2


def test_scenario_11_closing_the_session_clears_the_handle(fake_chromium):
    """Shutdown must not leave a handle that the next call trusts."""

    async def close_it():
        await owned_session.session()
        await owned_session.close_session()
        return owned_session._driver

    assert owned_session.run_owned(close_it) is None


def test_scenario_12_the_automation_browser_is_the_signed_in_browser():
    """"connect gmail" on /connections, then "open gmail" in chat.

    The connections page promises "a website opens in Akansha's own browser window
    so you sign in yourself". If the automation browser reads a different profile
    directory, that sign-in buys the person nothing and every later command meets a
    login wall. One directory is the entire mechanism.
    """
    from backend.app_discovery import browser_profile_dir

    assert owned_session.profile_dir() == browser_profile_dir()


# ── 13-16: what we ask Chromium for ───────────────────────────────────────────


def test_scenario_13_the_window_opens_on_the_real_profile_and_real_chrome(fake_chromium, tmp_path):
    """The launch arguments, which are the whole of the fix for scenario 12.

    `launch_persistent_context` on the sign-in directory, with the person's own
    Chrome rather than Playwright's bundled Chromium -- a profile built by one
    executable is not portable to the other, and passkeys live in theirs.
    """
    owned_session.run_owned(lambda: owned_session.session())

    directory, kwargs = fake_chromium.persistent_calls[0]

    assert Path(directory) == tmp_path / "profile"
    assert kwargs["executable_path"] == r"C:\fake\chrome.exe"
    assert kwargs["headless"] is False
    assert fake_chromium.plain_calls == []


def test_scenario_14_ci_can_still_have_a_throwaway_profile(monkeypatch, tmp_path):
    """Headless CI must not open, or lock, the person's real profile.

    `persistent=False` is the opt-out, and it has to stay a real opt-out: a test
    run that grabbed the profile directory would take the lock out from under a
    sign-in window.
    """
    chromium = _FakeChromium()
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", _FakeEntryPoint(_FakePlaywright(chromium))
    )
    monkeypatch.setattr(owned_session, "profile_dir", lambda: tmp_path / "never")

    async def launch():
        driver = PlaywrightDriver(headless=True, persistent=False)
        await driver.launch()
        return driver

    driver = owned_session.run_owned(launch)

    assert chromium.persistent_calls == []
    assert chromium.plain_calls and chromium.plain_calls[0]["headless"] is True
    assert not (tmp_path / "never").exists()
    assert driver._page is not None


def test_scenario_15_a_locked_profile_says_something_the_person_can_act_on(monkeypatch, tmp_path):
    """The sign-in window is still open, and then a command arrives.

    Chrome allows one process per user-data-dir, so this failure is routine rather
    than exotic. Playwright's own message names a lock file. "Close the sign-in
    window" is the sentence that gets the person unstuck, and it is the same
    sentence `app_control` already uses for the same collision.
    """
    chromium = _FakeChromium(fail_persistent="ProcessSingleton: failed to create lock")
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", _FakeEntryPoint(_FakePlaywright(chromium))
    )
    monkeypatch.setattr(owned_session, "profile_dir", lambda: tmp_path / "profile")
    monkeypatch.setattr(owned_session, "browser_executable", lambda: "")

    async def launch():
        await PlaywrightDriver().launch()

    with pytest.raises(RuntimeError) as caught:
        owned_session.run_owned(launch)

    assert str(caught.value) == PROFILE_IN_USE_DETAIL


def test_scenario_16_a_failed_launch_leaves_no_playwright_behind(monkeypatch, tmp_path):
    """Retrying after scenario 15 must not stack up node processes.

    Every `async_playwright().start()` is a node subprocess. Bailing out of a
    failed launch without stopping it leaks one per attempt, and a person who is
    told to close a window and try again will attempt it more than once.
    """
    playwright = _FakePlaywright(_FakeChromium(fail_persistent="ProcessSingleton"))
    monkeypatch.setattr("playwright.async_api.async_playwright", _FakeEntryPoint(playwright))
    monkeypatch.setattr(owned_session, "profile_dir", lambda: tmp_path / "profile")

    driver = PlaywrightDriver()

    async def launch():
        await driver.launch()

    with pytest.raises(RuntimeError):
        owned_session.run_owned(launch)

    assert playwright.stopped is True
    assert driver._playwright is None


# ── 17-20: the rest of the app agrees ─────────────────────────────────────────


def test_scenario_17_building_the_agent_executor_opens_no_window(monkeypatch, tmp_path):
    """Starting the backend must not open a browser.

    `BrowserDomainExecutor.__init__` used to construct a real `PlaywrightDriver`,
    so a second window existed from process start -- on a second, empty profile,
    beside the dispatcher's. Construction is not a browsing request.
    """
    chromium = _FakeChromium()
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", _FakeEntryPoint(_FakePlaywright(chromium))
    )
    monkeypatch.setattr(owned_session, "profile_dir", lambda: tmp_path / "profile")
    from backend.agent_modules import domain_executors

    if not domain_executors.PLAYWRIGHT_AVAILABLE:  # pragma: no cover - depends on install
        pytest.skip("Playwright is not installed, so the real stack is not constructed")

    executor = domain_executors.BrowserDomainExecutor()

    assert isinstance(executor._driver, owned_session.LazyOwnedDriver)
    assert chromium.persistent_calls == []
    assert chromium.plain_calls == []


def test_scenario_18_the_agent_and_the_chat_share_the_same_window(fake_chromium):
    """"open youtube" from chat, then a goal-engine step that navigates.

    Two entry points, one window. The handle the executor holds has to resolve to
    the shared session rather than to a browser of its own.
    """

    async def both():
        shared = await owned_session.session()
        await owned_session.LazyOwnedDriver().navigate("https://www.youtube.com")
        return shared

    shared = owned_session.run_owned(both)

    assert len(fake_chromium.persistent_calls) == 1
    assert shared._page.goto_calls == ["https://www.youtube.com"]


def test_scenario_19_the_window_is_visible_unless_something_asks_otherwise():
    """A success the person cannot see is not a success.

    "✅ Opened YouTube" with nothing on screen was a real reply from a real
    session. Headless has to be opt-in, and the opt-in has to be the documented
    environment variable rather than a scattered default.
    """
    import os

    assert owned_session.HEADLESS == (
        os.getenv("AKANSHA_BROWSER_HEADLESS", "false").lower() == "true"
    )
    driver = PlaywrightDriver()
    assert driver.headless is False
    assert driver.persistent is True


def test_scenario_20_a_slow_web_lookup_cannot_outlast_the_answer():
    """"who is the prime minister right now" -- the latency scenario.

    `_needs_live_web_context` is broad on purpose, so a large share of ordinary
    turns pay a live web walk before the first token. The three budgets have to
    stay ordered: the fallback apology must arrive sooner than the voice answer it
    replaces, and voice sooner than text, because a listener has nothing to look at
    while it waits.
    """
    from backend import ai_engine
    from backend.ai_engine import (
        _FALLBACK_LIVE_BUDGET_S,
        _PRE_MODEL_LIVE_BUDGET_TEXT_S,
        _PRE_MODEL_LIVE_BUDGET_VOICE_S,
        _live_context_within_budget,
    )

    assert _FALLBACK_LIVE_BUDGET_S < _PRE_MODEL_LIVE_BUDGET_VOICE_S
    assert _PRE_MODEL_LIVE_BUDGET_VOICE_S < _PRE_MODEL_LIVE_BUDGET_TEXT_S

    # And the bound is real, not just declared. A lookup that never returns has to
    # give the turn back, and it has to give it back as "no sources" rather than as
    # an exception -- the caller's honest answer to that is to answer locally.
    started = threading.Event()
    original = ai_engine._build_multi_question_live_context
    try:
        ai_engine._build_multi_question_live_context = lambda *_a, **_k: (
            started.set() or threading.Event().wait(30) or "too late"
        )
        assert _live_context_within_budget("who is the prime minister right now", 0.2) == ""
    finally:
        ai_engine._build_multi_question_live_context = original
    assert started.is_set(), "the lookup should have been attempted, not skipped"
