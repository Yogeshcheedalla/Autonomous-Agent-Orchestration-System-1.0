"""Akansha's own browser: one window, one profile, one event loop.

Three defects live in the seam this module replaces, and all three are visible
from the user's chair.

**The profile was thrown away.** `PlaywrightDriver.launch` did
`chromium.launch()` + `new_context()` with no `user_data_dir`, which is a fresh
incognito-equivalent profile on every launch. Meanwhile `/connections` tells the
person "a website opens in Akansha's own browser window so you sign in yourself"
and signs them in to `app_discovery.browser_profile_dir()`. Those were two
different browsers. Signing into YouTube on the connections page did nothing
whatsoever for "play Varsham songs" in chat, because the chat command opened a
browser that had never heard of the sign-in.

**The event loop was closed under the singleton.** `site_dispatcher` kept a
process-global driver *and* entered it through `asyncio.run(...)` once per
request. `asyncio.run` creates a loop and closes it on return, so the second
site command in a process reached for a browser whose transport was bound to a
dead loop. `is_connected()` still answered True -- it does not consult the loop
-- so the relaunch guard did not fire and the failure surfaced as
`Event loop is closed` or a hang. First command works, every later one does not:
that is the shape of "it is not performing the task".

**Everyone launched their own.** `site_dispatcher` had a singleton;
`agent_modules/domain_executors.py` constructed its own `PlaywrightDriver`
beside it. Two windows, two profiles, and no way to tell which one the user was
looking at.

So: one loop that stays alive for the life of the process, one persistent
profile shared with the sign-in flow, one driver reached through `session()`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Headful unless something explicitly asks otherwise. A window the person cannot
#: see is how "✅ Opened YouTube" got reported for work nobody could watch, and a
#: persistent profile created headful is also less likely to be challenged when
#: driven headless later.
HEADLESS = os.getenv("AKANSHA_BROWSER_HEADLESS", "false").lower() == "true"

#: Ceiling for a single owned-browser call made from a synchronous caller. A page
#: load waits for `networkidle` with a 30s timeout of its own, and a site skill can
#: chain several, so this is generous on purpose -- it exists to stop a wedged
#: browser from holding a request open forever, not to bound normal work.
DEFAULT_CALL_TIMEOUT_S = 180.0

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()

_driver: Any = None
_driver_lock: Optional[asyncio.Lock] = None


def loop() -> asyncio.AbstractEventLoop:
    """The one event loop the browser is bound to, started on first use.

    A daemon thread, so a wedged browser cannot keep the process alive at exit,
    and `run_forever` rather than `asyncio.run` because the whole point is that
    this loop outlives the request that first needed it.
    """
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, name="akansha-browser", daemon=True
        )
        _loop_thread.start()
        logger.info("Owned browser event loop started")
        return _loop


def run_owned(factory: Callable[[], Awaitable[T]], timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> T:
    """Run one coroutine on the owned loop from a synchronous caller.

    Takes a factory rather than a coroutine so the coroutine is only created once
    it is certain to be awaited: building it here and failing to schedule it would
    leave a never-awaited coroutine and a warning that says nothing useful.

    Callers that are *already* async must not use this -- they are on the server's
    loop, and blocking it on another loop's result is a deadlock waiting to be
    reported as latency. They should be dispatched with `asyncio.to_thread` first,
    which is what `main.py` already does for the sync Playwright path.
    """
    target = loop()

    async def _wrapped() -> T:
        return await factory()

    future = asyncio.run_coroutine_threadsafe(_wrapped(), target)
    return future.result(timeout=timeout_s)


def profile_dir() -> Path:
    """The same directory the sign-in flow writes cookies into.

    Imported lazily and defensively: `browser` is also used in contexts where the
    FastAPI package tree is not importable, and a missing sibling module must not
    turn into "no browser at all". The fallback is the same path spelled locally.
    """
    try:
        from ..app_discovery import browser_profile_dir

        return browser_profile_dir()
    except Exception:  # pragma: no cover - only when imported outside the package
        return Path(__file__).resolve().parents[2] / ".akansha" / "akansha-browser-profile"


def browser_executable() -> str:
    """The person's real Chrome/Edge/Brave when there is one, else "".

    Worth preferring over Playwright's bundled Chromium for the same reason
    `app_control` prefers it: it is the browser whose passkeys and password
    manager the person already has, and a persistent profile created by one
    executable is not portable to another.
    """
    try:
        from ..app_control import find_browser

        return find_browser()
    except Exception:  # pragma: no cover - only when imported outside the package
        return ""


async def _lock() -> asyncio.Lock:
    """One lock, created on the owned loop so it binds to the right loop."""
    global _driver_lock
    if _driver_lock is None:
        _driver_lock = asyncio.Lock()
    return _driver_lock


def _is_alive(driver: Any) -> bool:
    """Whether `driver`'s browser is still usable from this loop.

    `is_connected()` alone is not enough and was the bug: it reports on the CDP
    socket, not on the loop that socket is registered with, so a driver stranded
    by a closed loop passed the old check. Comparing the loop the driver was
    launched on against the loop asking is the part that was missing.
    """
    if driver is None:
        return False
    if getattr(driver, "_owned_loop", None) is not asyncio.get_running_loop():
        return False
    browser = getattr(driver, "_browser", None)
    if browser is None:
        return False
    try:
        return bool(browser.is_connected())
    except Exception:
        return False


async def session() -> Any:
    """The one `PlaywrightDriver`, launched on first use and relaunched if it died.

    Every caller on the owned loop shares this. It is deliberately *not* closed
    between requests: relaunching a persistent Chrome profile costs seconds and
    loses the tab the person is looking at.
    """
    global _driver
    from .playwright_driver import PlaywrightDriver

    async with await _lock():
        if _is_alive(_driver):
            return _driver
        if _driver is not None:
            logger.warning("Owned browser was not usable from this loop; relaunching")
            try:
                await _driver.close()
            except Exception:
                pass
        _driver = PlaywrightDriver(headless=HEADLESS)
        await _driver.launch()
        _driver._owned_loop = asyncio.get_running_loop()
        return _driver


async def close_session() -> None:
    """Shut the window. Only for tests and process shutdown."""
    global _driver
    if _driver is None:
        return
    try:
        await _driver.close()
    finally:
        _driver = None


class LazyOwnedDriver:
    """A `PlaywrightDriver`-shaped handle that resolves to the shared one.

    Exists so a long-lived object can hold "the browser" as a constructor argument
    without a browser window opening the moment that object is built.
    `BrowserDomainExecutor` did the opposite -- it constructed a real
    `PlaywrightDriver` in `__init__`, which meant a second window beside the
    dispatcher's, on a second profile, with no way for the person to tell which
    one they were looking at.

    Only the four methods the site skills actually call are forwarded, plus the
    two used for reading state. Anything else should go through `session()` and be
    explicit about wanting the driver itself.
    """

    async def launch(self) -> None:
        await session()

    async def navigate(self, url: str) -> Any:
        return await (await session()).navigate(url)

    async def get_page(self) -> Any:
        return await (await session()).get_page()

    async def evaluate(self, script: str) -> Any:
        return await (await session()).evaluate(script)

    async def screenshot(self, path: Optional[str] = None) -> bytes:
        return await (await session()).screenshot(path)

    async def close(self) -> None:
        await close_session()

    @property
    def current_url(self) -> str:
        return _driver.current_url if _driver is not None else ""
