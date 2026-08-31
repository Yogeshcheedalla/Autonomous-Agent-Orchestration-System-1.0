"""Actually driving an adopted app or site.

`app_connect` decides whether a connection exists; `app_discovery` finds the
things to connect. This module is the third piece: doing something once the
connection is real.

Two honest constraints shape it.

**Sign-in must happen in a browser that is not being automated.** Google's
sign-in flow refuses a CDP-controlled Chrome with "this browser or app may not be
secure", and it is right to -- so the one-click *authenticate* step launches the
real Chrome as an ordinary subprocess pointed at Akansha's profile directory, with
no automation attached. The person signs in exactly as they would in their own
browser. Only *afterwards*, when the cookie is already in that profile, does
Playwright attach to the same directory to drive the site. Doing it the other way
round would produce a login page that cannot be logged into.

**Chrome allows one process per profile directory.** So a control call while a
sign-in window is still open fails, and it has to fail with that sentence rather
than a Playwright stack trace, because "close the sign-in window" is something
the user can act on.

**Typing is aimed, not sprayed.** The desktop verbs go through `desktop_ui` first,
which resolves "the Message field" to a named control and sets its whole value in
one call. `pyautogui.typewrite` into the focused window is kept underneath, because
an app that publishes no accessibility tree leaves nothing else -- but it is the
fallback now, and the module says which of the two it used.

Every side effect is injected (`spawn`, `runner`), so the routing and the error
messages are testable without launching a browser or moving the mouse.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

#: Real-Chrome candidates, most-preferred first. The person's own Chrome is worth
#: preferring over Playwright's bundled Chromium for the sign-in step: it is the
#: browser their password manager and their passkeys already live in.
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

WEB_VERBS = ("open", "navigate", "type", "click", "read_page")
DESKTOP_VERBS = ("launch", "focus", "type_text", "click", "read_window", "close")

#: Desktop verbs with no keystroke fallback, because there is no honest one. A
#: blind `click` is a pair of screen coordinates, which is the guessing this module
#: was rewritten to stop; a blind `read_window` is not possible at all. When the
#: accessibility tree cannot answer, these two say so instead of approximating.
UIA_ONLY_VERBS = ("click", "read_window")

def find_browser(candidates: tuple[str, ...] = CHROME_CANDIDATES) -> str:
    """A browser executable for the sign-in step, or "".

    Falls back to Playwright's bundled Chromium only if no installed browser is
    found: it works, but a person signing into Google in it is more likely to be
    challenged, so it is the last resort rather than the default.
    """
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    for name in ("chrome", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    try:  # pragma: no cover - depends on a downloaded browser
        from playwright.sync_api import sync_playwright

        driver = sync_playwright().start()
        try:
            path = driver.chromium.executable_path
        finally:
            driver.stop()
        return path if os.path.exists(path) else ""
    except Exception:
        return ""


def open_for_sign_in(
    url: str,
    *,
    profile_dir: Path | str,
    browser: str | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
    find: Callable[[], str] = find_browser,
) -> dict[str, Any]:
    """Open `url` in Akansha's own browser profile, with no automation attached.

    `--no-first-run` and `--no-default-browser-check` because a first-run wizard
    between the click and the login form is the difference between one click and
    five. Nothing else is passed: no automation flags, no remote debugging port --
    the whole point is that the site sees an ordinary browser.
    """
    executable = browser or find()
    if not executable:
        return {
            "ok": False,
            "detail": "No Chrome, Edge or Brave was found on this machine to sign in with.",
        }
    directory = Path(profile_dir)
    directory.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        f"--user-data-dir={directory}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    try:
        spawn(command, close_fds=True)
    except Exception as exc:
        return {"ok": False, "detail": f"Could not start {Path(executable).name}: {exc}"}
    return {
        "ok": True,
        "detail": (
            f"Opened {url} in Akansha's browser window. Sign in there as you normally would -- "
            "your password goes to the site, not through this app -- then close the window."
        ),
        "browser": executable,
        "profile_dir": str(directory),
    }


def _playwright_web(
    verb: str,
    *,
    url: str,
    profile_dir: Path,
    selector: str,
    text: str,
    browser: str,
    timeout_ms: int,
) -> dict[str, Any]:  # pragma: no cover - drives a real browser
    """The real driver. Persistent context, so the signed-in cookie is in scope.

    `headless=False` deliberately: a person watching Akansha work on their behalf
    should be able to see what it clicked, and a headless run on a profile that was
    created headful is also more likely to be challenged.
    """
    from playwright.sync_api import sync_playwright

    driver = sync_playwright().start()
    try:
        context = driver.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=browser or None,
            headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_ms)
            if verb in ("open", "navigate"):
                page.goto(url, wait_until="domcontentloaded")
                return {"ok": True, "detail": f"Opened {page.url}", "title": page.title()}
            page.goto(url, wait_until="domcontentloaded")
            if verb == "click":
                page.click(selector)
                return {"ok": True, "detail": f"Clicked {selector} on {page.url}"}
            if verb == "type":
                page.fill(selector, text)
                return {"ok": True, "detail": f"Typed into {selector} on {page.url}"}
            body = page.inner_text(selector or "body")
            return {
                "ok": True,
                "detail": f"Read {len(body)} characters from {page.url}",
                "text": body[:8000],
                "title": page.title(),
            }
        finally:
            context.close()
    finally:
        driver.stop()


def web_action(
    verb: str,
    *,
    url: str,
    profile_dir: Path | str,
    selector: str = "",
    text: str = "",
    browser: str | None = None,
    timeout_ms: int = 15000,
    runner: Callable[..., dict[str, Any]] = _playwright_web,
) -> dict[str, Any]:
    """Do one thing on a site in the profile that is signed into it."""
    if verb not in WEB_VERBS:
        return {"ok": False, "detail": f"'{verb}' is not one of: {', '.join(WEB_VERBS)}."}
    if verb in ("click", "type") and not selector.strip():
        return {"ok": False, "detail": f"'{verb}' needs a CSS selector saying what to act on."}
    if verb == "type" and not text:
        return {"ok": False, "detail": "'type' needs the text to type."}
    if not url:
        return {"ok": False, "detail": "No site URL is stored for this connection."}
    try:
        return runner(
            verb,
            url=url,
            profile_dir=Path(profile_dir),
            selector=selector.strip(),
            text=text,
            browser=browser or find_browser(),
            timeout_ms=timeout_ms,
        )
    except Exception as exc:
        message = str(exc)
        if "ProcessSingleton" in message or "user data directory is already in use" in message:
            # The specific, actionable failure. Worth its own sentence: the generic
            # Playwright error names a lock file, which tells the user nothing.
            return {
                "ok": False,
                "detail": (
                    "Akansha's browser window is already open -- close the sign-in window, "
                    "then try again. Chrome allows one window per profile."
                ),
            }
        return {"ok": False, "detail": f"{verb} failed: {message}"}


def _accessible_desktop(
    verb: str, *, label: str, text: str, target: str = ""
) -> dict[str, Any] | None:
    """The named-control path, or None when this window needs the keystroke path.

    Tried first because it is the difference between "typed into the field called
    Message" and "sent N keystrokes to whatever had focus at each of N moments".
    It declines rather than guesses, and there are three ways it declines:

    * pywinauto or its UIA backend is missing -- nothing to try;
    * the window publishes only its frame, which is what Qt apps like Telegram do,
      so there is no named field to aim at;
    * more than one field answers and none has the cursor.

    The first two fall through to keystrokes. The third does not: an ambiguous
    target comes back as a question, and retrying it blind is the exact failure the
    question exists to prevent.

    `click` and `read_window` never fall through -- see `UIA_ONLY_VERBS` -- so for
    those the failure itself is the answer, and it is returned rather than hidden.
    """
    required = verb in UIA_ONLY_VERBS

    def refused(detail: str) -> dict[str, Any] | None:
        return {"ok": False, "detail": detail} if required else None

    try:
        from . import desktop_ui
    except Exception as exc:
        return refused(
            f"Reading {label}'s controls needs pywinauto, which is not importable here ({exc})."
        )
    capability = desktop_ui.capability()
    if not capability.usable:
        return refused(f"Cannot see inside {label}: {capability.detail}")
    try:
        if verb == "focus":
            result = desktop_ui.focus(label)
        elif verb == "close":
            result = desktop_ui.close(label)
        elif verb == "click":
            result = desktop_ui.click(label, target)
        elif verb == "read_window":
            result = _read_window(desktop_ui, label)
        elif verb == "type_text":
            result = (
                desktop_ui.set_text(label, target, text)
                if target
                else desktop_ui.type_into(label, text)
            )
        else:
            return refused(f"'{verb}' has no accessibility implementation.")
    except Exception as exc:
        return refused(f"{verb} failed on {label}: {exc}")
    if required or result.get("ok") or result.get("question"):
        return result
    return None


def _read_window(desktop_ui: Any, label: str) -> dict[str, Any]:
    """What is on screen in `label`'s window, as something a model can act on.

    `controls()` reports a frame-only window as a successful read of five caption
    buttons, which is true and useless. Read is the one verb where that has to
    count as a failure: answering "here is what is in Telegram: Minimize, Restore,
    Close" would be a confident description of not having looked.
    """
    listed = desktop_ui.controls(label)
    if not listed.get("ok"):
        return listed
    if not listed.get("sees_inside"):
        return {**listed, "ok": False}
    names = [row.get("name", "") for row in listed.get("controls", []) if row.get("name")]
    return {
        **listed,
        "text": "\n".join(names[:400]),
        "detail": (
            f"{listed['detail']} {len(listed.get('actionable', []))} of them can be clicked "
            f"by name and {len(listed.get('editable', []))} can be typed into."
        ),
    }


def _real_desktop(
    verb: str, *, launch_target: str, label: str, text: str, target: str = ""
) -> dict[str, Any]:  # pragma: no cover - moves the real mouse and keyboard
    """The real driver. `os.startfile` for launch, accessibility first for the rest.

    `os.startfile` rather than a resolved exe path: for a `.lnk` it is Windows that
    resolves the shortcut, including the working directory and the arguments the
    installer chose. Launching the extracted exe without those is how an app starts
    and then immediately complains it cannot find its own files.

    The `pygetwindow`/`pyautogui` half below is the fallback, not the plan. It is
    kept because it is the only thing that works on a window that publishes no
    accessibility tree -- measured, Telegram's whole tree is six frame controls --
    and `desktop_ui` says which case it is looking at instead of guessing.
    """
    if verb == "launch":
        os.startfile(launch_target)  # noqa: S606 - a path this user adopted by clicking it
        return {"ok": True, "detail": f"Launched {label}."}

    accessible = _accessible_desktop(verb, label=label, text=text, target=target)
    if accessible is not None:
        return accessible

    import pygetwindow as gw

    token = label.split("(")[0].strip().lower()
    matches = [w for w in gw.getAllWindows() if token and token in (w.title or "").lower()]
    if not matches:
        return {
            "ok": False,
            "detail": f"No open window looks like {label}. Launch it first.",
        }
    window = matches[0]
    if verb == "close":
        window.close()
        return {"ok": True, "detail": f"Closed {label}."}

    try:
        if window.isMinimized:
            window.restore()
        window.activate()
    except Exception:
        # Windows refuses `activate` from a background process often enough that
        # failing the whole call over it would make focus unusable. The subsequent
        # typing goes to whatever is focused, so say so rather than pretend.
        if verb == "focus":
            return {"ok": False, "detail": f"Windows would not bring {label} to the front."}
    if verb == "focus":
        return {"ok": True, "detail": f"Brought {label} to the front."}

    import pyautogui

    pyautogui.typewrite(text, interval=0.01)
    return {"ok": True, "detail": f"Typed {len(text)} characters into {label}."}


def desktop_action(
    verb: str,
    *,
    launch_target: str,
    label: str,
    text: str = "",
    target: str = "",
    runner: Callable[..., dict[str, Any]] = _real_desktop,
) -> dict[str, Any]:
    """Do one thing to an app on this machine.

    Validated before it is run, because every one of these verbs moves the real
    mouse, keyboard or window stack -- a typo should be a message, not a keystroke
    sent to whatever happened to be focused.

    `target` names the control to act on: the button for `click`, the field for
    `type_text`. Empty means "the field with the cursor, or the only one there is",
    which is `desktop_ui.type_into`'s job and the reason `type_text` still works on
    apps whose fields have no useful names.
    """
    if verb not in DESKTOP_VERBS:
        return {"ok": False, "detail": f"'{verb}' is not one of: {', '.join(DESKTOP_VERBS)}."}
    if verb == "type_text" and not text:
        return {"ok": False, "detail": "'type_text' needs the text to type."}
    if verb == "click" and not target.strip():
        return {
            "ok": False,
            "detail": (
                "'click' needs the name of the thing to click -- read the window first "
                "and use one of the names it lists."
            ),
        }
    if verb == "launch":
        if not launch_target:
            return {"ok": False, "detail": f"No launch path is stored for {label}."}
        if not os.path.exists(launch_target):
            return {
                "ok": False,
                "detail": (
                    f"{label} was here when it was added and is not now: {launch_target} "
                    "no longer exists. Re-scan this PC, or remove it."
                ),
            }
    try:
        return runner(
            verb, launch_target=launch_target, label=label, text=text, target=target.strip()
        )
    except Exception as exc:
        return {"ok": False, "detail": f"{verb} failed on {label}: {exc}"}
