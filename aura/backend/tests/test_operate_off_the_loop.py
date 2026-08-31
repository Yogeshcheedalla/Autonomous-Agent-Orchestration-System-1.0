"""
The connected-app route must not run Playwright on the event loop thread.
========================================================================

Reported from a real session, verbatim: ``open google.com in Brave`` came back as

    open failed: It looks like you are using Playwright Sync API inside the
    asyncio loop. Please use the Async API instead.

That message is assembled in `app_control.web_action`'s exception handler, so the
chain is unambiguous: `/api/automation/browser/prompt` is `async def`, it called
`_operate_if_connected` directly, and that ends in `web_action` ->
`_playwright_web` -> `sync_playwright().start()` -- a sync Playwright call on a
thread that already has a running event loop, which Playwright refuses outright.

The same file already knew the answer twenty lines further down, where the
site-dispatcher bypass hands its work to a threadpool. A *sync* FastAPI endpoint
reaching the same code works fine, because FastAPI runs those in a threadpool
itself; this route was the one place that lost that property.

So the fix is a thread, not a rewrite to the async API, and this test asserts the
property that matters: whatever `_operate_if_connected` does, it does it somewhere
`asyncio.get_running_loop()` raises. Playwright's own check is exactly that call.
No browser is started here.
"""

from __future__ import annotations

import asyncio
import threading

from backend import main


class _Recorder:
    """Stands in for `_operate_if_connected` and reports where it was run."""

    def __init__(self, answer: dict | None):
        self.answer = answer
        self.saw_running_loop: bool | None = None
        self.thread: str | None = None

    def __call__(self, db, prompt):
        self.thread = threading.current_thread().name
        try:
            asyncio.get_running_loop()
            self.saw_running_loop = True
        except RuntimeError:
            # What Playwright's sync API checks for, and the only state in which it
            # agrees to start.
            self.saw_running_loop = False
        return self.answer


def _call(recorder: _Recorder, monkeypatch) -> dict:
    monkeypatch.setattr(main, "_operate_if_connected", recorder)
    request = main.BrowserAutomationPromptRequest(prompt="open google.com in Brave")
    return asyncio.run(main.run_browser_automation_prompt(request, db=object()))


def test_the_registry_route_runs_off_the_event_loop(monkeypatch):
    """The regression, in one assertion.

    `saw_running_loop is True` here would mean `sync_playwright().start()` is back
    to raising "use the Async API instead" for every connected-app action.
    """
    recorder = _Recorder({"success": True, "message": "Opened https://google.com", "steps": []})

    result = _call(recorder, monkeypatch)

    assert recorder.saw_running_loop is False
    assert recorder.thread != threading.current_thread().name
    assert result["message"] == "Opened https://google.com"


#: What the route reaches when the registry declines. Stubbed at the site-dispatcher
#: bypass, which is the next branch in the function -- deliberately not further: the
#: legacy path past it builds a pyautogui plan and runs `execute_desktop_command`,
#: which launches real applications. A test proving where a call runs must not open a
#: browser to do it.
FELL_THROUGH = "[fell through to the dispatcher]"


def _stub_fall_through(monkeypatch) -> None:
    import backend.browser.skills.site_dispatcher as dispatcher

    monkeypatch.setattr(dispatcher, "should_use_playwright", lambda prompt: True)
    monkeypatch.setattr(
        dispatcher, "dispatch_playwright", lambda prompt: {"success": True, "message": FELL_THROUGH}
    )


def test_an_unrecognised_prompt_still_falls_through(monkeypatch):
    """`None` means "not mine", and that has to keep meaning it.

    Moving the call into a thread must not change the routing: an app the registry
    does not know is supposed to reach the paths after it untouched.
    """
    recorder = _Recorder(None)
    _stub_fall_through(monkeypatch)

    result = _call(recorder, monkeypatch)

    assert recorder.saw_running_loop is False
    assert result["message"] == FELL_THROUGH


def test_a_failure_inside_the_thread_does_not_take_the_request_down(monkeypatch):
    """The route logs and falls through, as it did when the call was inline.

    Worth pinning: an exception raised in a worker thread surfaces at the `await`,
    so it is catchable in the same place -- but only because the `try` was left
    wrapped around the `await` rather than around the old inline call.
    """

    def explode(db, prompt):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(main, "_operate_if_connected", explode)
    _stub_fall_through(monkeypatch)
    request = main.BrowserAutomationPromptRequest(prompt="open google.com in Brave")

    result = asyncio.run(main.run_browser_automation_prompt(request, db=object()))

    assert result["message"] == FELL_THROUGH
