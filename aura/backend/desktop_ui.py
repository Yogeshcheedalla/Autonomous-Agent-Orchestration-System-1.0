"""
Naming the thing to click, instead of guessing where it is.

`app_control._real_desktop` types with `pyautogui.typewrite` into whatever window
happens to be focused, and finds that window by substring-matching titles from
`pygetwindow`. It works until it doesn't: a different taskbar, a Windows that
refuses `activate` from a background process, a dialog that steals focus between
the check and the keystroke -- and the text goes somewhere else. Coordinates are
worse again: they encode this machine's resolution, DPI and window position.

Windows already publishes what every control is called. This module reads that
tree, so "click Send" resolves to the button named Send and typing goes into the
named edit field atomically, not as a stream of keystrokes aimed at the focus.

Three things were measured on this machine before the design settled, and each
one changed it:

    Desktop(backend="uia").windows()      7 windows   159 ms
    Desktop(backend="win32").windows()  232 windows    42 ms  (136 titled)

`uia` enumeration misses windows -- WhatsApp and Telegram were both live and both
absent from its list, and `uia.window(title_re=".*WhatsApp.*")` raised
`ElementNotFoundError` while win32 listed it. So enumeration is win32 and the
control tree is uia, attached by window handle. Neither backend does both jobs.

    hwnd 198072  "WhatsApp"          6 controls    5 named    17 ms
    hwnd 197134  "(26) WhatsApp"   449 controls  200 named   761 ms

The same app owns several top-level windows, and on that first reading only one of
them had a UI in it -- so matching "WhatsApp" by title alone picked the 6-control
window, one where nothing is clickable.

Re-measuring the same two windows later told a second, more useful story:

    hwnd 198072  "WhatsApp"        228 named
    hwnd 132692  "GDI+ Window..."    0 named
    hwnd 197134  "(30) WhatsApp"   193 named

The 6-control reading was partly a cold tree, not a frame: Chromium publishes its
accessibility tree only once something asks for it, and the first ask can arrive
before it exists. So two rules survive rather than one. A window with a UI in it
outranks a better title with nothing in it, and a first look that finds only a
frame is taken again before it is believed (see `WARM_RETRY_S`).

    hwnd 132800  "Telegram (101922)" 6 controls    5 named    13 ms

And the honest limit: Telegram is Qt and exposes no UIA content at all. Its whole
tree is the window frame. For apps like that this module reports that it cannot
see inside, and `app_control`'s keystroke path stays as the fallback -- which is
why that path is kept rather than replaced.

Nothing here moves the mouse or the keyboard unless a verb says so, and every
side effect is injected so the routing and the error text are testable on a
machine with no windows open at all.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

_log = logging.getLogger(__name__)

#: Walking a big tree is the expensive part -- 449 controls cost 761 ms above. A
#: cap keeps the worst case bounded; 400 is past every real window measured here
#: and still returns inside a second.
MAX_CONTROLS = 400

#: The budget for one more look when the first one ran out of budget. Measured on
#: WhatsApp's chat-list window: capped at 400 the walk returned 172 named controls
#: in 0.5 s, uncapped it returned 267 named out of 504 total in 2.1 s -- and the
#: extra 95 are the deep ones, because breadth-first spends its budget on a chat
#: list of a hundred rows before it reaches anything nested under them. So the cap
#: is right for "describe this window" and wrong for "find the thing called X":
#: answering *"nothing here is called X"* while we knowingly stopped looking is a
#: lie, and 2 s to avoid telling it is worth paying once.
DEEP_CONTROLS = 4000

#: How long a cached control tree stays trusted. Short on purpose: a UI changes
#: while the user is in it, and a stale tree means clicking a button that has
#: moved. Long enough that "list the controls, then click one" is a single walk.
TREE_TTL_S = 4.0

#: Window classes that are the shell rather than an app. Enumerating win32 returns
#: 232 windows on an idle desktop and almost none of them are things a person
#: would name.
SHELL_CLASSES = frozenset(
    {
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "Progman",
        "WorkerW",
        "SystemTray_Main",
        "NotifyIconOverflowWindow",
        "Windows.UI.Core.CoreWindow",
        "ApplicationFrameWindow",
        "TaskListThumbnailWnd",
        "tooltips_class32",
        "IME",
        "MSCTFIME UI",
    }
)

#: Control kinds worth offering as click targets. A `Pane` or a `Static` is
#: usually structure, not something a person means when they say "click".
ACTIONABLE_KINDS = frozenset(
    {
        "Button",
        "CheckBox",
        "RadioButton",
        "MenuItem",
        "ListItem",
        "TabItem",
        "TreeItem",
        "Hyperlink",
        "SplitButton",
        "ComboBox",
    }
)

#: Kinds that hold text a person can set.
EDITABLE_KINDS = frozenset({"Edit", "Document", "ComboBox"})

@dataclass(frozen=True)
class Capability:
    """Whether accessibility targeting is possible here, and what to run if not."""

    installed: bool
    ready: bool
    detail: str
    backend_enumerate: str = "win32"
    backend_inspect: str = "uia"

    @property
    def usable(self) -> bool:
        return self.installed and self.ready

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "ready": self.ready,
            "detail": self.detail,
            "backend_enumerate": self.backend_enumerate,
            "backend_inspect": self.backend_inspect,
        }


@dataclass(frozen=True)
class Window:
    """One top-level window, as enumerated rather than as guessed at."""

    handle: int
    title: str
    class_name: str
    process_id: int = 0
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "title": self.title,
            "class_name": self.class_name,
            "process_id": self.process_id,
            "score": round(self.score, 3),
        }


@dataclass(frozen=True)
class Control:
    """One named thing inside a window."""

    name: str
    kind: str
    automation_id: str = ""
    enabled: bool = True
    depth: int = 0
    rect: dict[str, int] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE_KINDS

    @property
    def editable(self) -> bool:
        return self.kind in EDITABLE_KINDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "automation_id": self.automation_id,
            "enabled": self.enabled,
            "depth": self.depth,
            "rect": dict(self.rect),
            "actionable": self.actionable,
            "editable": self.editable,
        }


_lock = threading.RLock()
_unavailable_reason: str | None = None

#: handle -> (walked_at, controls). Keyed by handle rather than title because a
#: title changes while the app runs -- "(26) WhatsApp" becomes "(27) WhatsApp" on
#: the next message, and a title-keyed cache would miss every time.
_tree_cache: dict[int, tuple[float, list[Control]]] = {}


def capability() -> Capability:
    """Report availability without touching a window.

    Import-only, like the voice and embedding probes. A UI asking "can you see
    inside apps?" must not enumerate 232 windows to answer.
    """
    global _unavailable_reason
    try:
        import pywinauto  # noqa: F401, PLC0415
    except Exception as exc:
        _unavailable_reason = (
            f"pywinauto is not importable ({exc}). Run: pip install pywinauto"
        )
        return Capability(installed=False, ready=False, detail=_unavailable_reason)
    try:
        import comtypes  # noqa: F401, PLC0415
    except Exception as exc:
        # The uia backend is comtypes-based. Without it enumeration still works
        # and the control tree does not, which is a different and lesser failure
        # -- say which one it is.
        return Capability(
            installed=True,
            ready=False,
            detail=f"pywinauto is present but the UIA backend is unavailable ({exc}). Window targeting works; control targeting does not.",
        )
    return Capability(installed=True, ready=True, detail="Accessibility targeting ready.")


def available() -> bool:
    return capability().installed


def _desktop(backend: str):
    """A pywinauto Desktop, or None when pywinauto is missing.

    Not cached. `Desktop` construction measured 660 ms on first import and
    microseconds afterwards -- the cost is the import, which Python caches for us.
    """
    try:
        from pywinauto import Desktop  # noqa: PLC0415

        return Desktop(backend=backend)
    except Exception as exc:
        global _unavailable_reason
        _unavailable_reason = f"pywinauto backend '{backend}' unavailable ({exc})"
        return None


def _tokens(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", (text or "").lower()) if token]


def windows(
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
) -> list[Window]:
    """Every top-level window worth naming, cheapest backend first.

    win32 rather than uia, because uia's enumeration is incomplete: measured here,
    it returned 7 windows while win32 returned 232, and two live apps (WhatsApp,
    Telegram) were in the second list and not the first. The shell windows that
    padding brings are filtered by class.
    """
    if enumerate_with is None:
        desktop = _desktop("win32")
        if desktop is None:
            return []
        enumerate_with = desktop.windows
    try:
        raw = list(enumerate_with())
    except Exception as exc:
        _log.warning("Window enumeration failed: %s", exc)
        return []

    found: list[Window] = []
    seen: set[int] = set()
    for handle in raw:
        try:
            title = (handle.window_text() or "").strip()
            class_name = handle.class_name() or ""
            hwnd = int(handle.handle)
        except Exception:
            # A window that closed between the enumeration and the read. Normal,
            # and not worth a log line on every call.
            continue
        if not title or hwnd in seen or class_name in SHELL_CLASSES:
            continue
        seen.add(hwnd)
        try:
            process_id = int(handle.process_id())
        except Exception:
            process_id = 0
        found.append(Window(handle=hwnd, title=title, class_name=class_name, process_id=process_id))
    return found


def _score_window(window: Window, label: str) -> float:
    """How well this window answers to `label`.

    Exact title beats prefix beats substring beats shared words, and a window
    whose title is *only* the label scores highest of all. The measured failure
    this exists for: "WhatsApp" matches both the 6-control window frame and the
    449-control content window, and title matching alone picks the frame.
    """
    title = window.title.lower()
    wanted = (label or "").strip().lower()
    if not wanted:
        return 0.0
    if title == wanted:
        return 1.0
    if title.startswith(wanted) or title.endswith(wanted):
        return 0.8
    if wanted in title:
        return 0.7
    label_tokens = set(_tokens(wanted))
    title_tokens = set(_tokens(title))
    if not label_tokens:
        return 0.0
    overlap = len(label_tokens & title_tokens) / len(label_tokens)
    return 0.6 * overlap


def _content_weight(handle: int, *, inspect: Callable[[int], list[Control]] | None = None) -> int:
    """How many named controls this window actually has.

    The tiebreak that matters. Two windows called WhatsApp: one is the frame with
    5 named controls, one is the UI with 200. Only the second is a window you can
    do anything in, and title similarity cannot tell them apart.
    """
    reader = inspect or (lambda hwnd: _walk(hwnd))
    try:
        return len([control for control in reader(handle) if control.name])
    except Exception:
        return 0


#: How many title-matched candidates get their control tree counted. Each walk is
#: up to ~760 ms, so this is the ceiling on how much a lookup can cost: three
#: candidates is enough to separate an app's frame from its content window, and
#: counting all 136 titled windows would take a minute.
CONTENT_PROBE_LIMIT = 3

#: A title match at least this good is worth paying a tree walk to check. 0.7 is
#: `substring`: "WhatsApp" appearing anywhere in the title. Below that the match is
#: shared words, and counting controls on a coincidence is a second wasted.
CONTENT_PROBE_SCORE = 0.7


def find_window(
    label: str,
    *,
    candidates: Sequence[Window] | None = None,
    inspect: Callable[[int], list[Control]] | None = None,
) -> Window | None:
    """The window `label` means, or None.

    Title similarity alone gets this wrong, and the measurement says so: hwnd 198072
    is titled exactly "WhatsApp" and holds 5 named controls, while hwnd 197134 is
    titled "(26) WhatsApp" -- a *worse* match -- and holds the 200 that the person
    is looking at. So a window with a UI in it outranks a better title with nothing
    in it, and among windows that both have a UI the better title wins.

    The content probe costs a tree walk each, so it runs only when two or more
    candidates match the title properly, and only over the top few. A unique match
    stays free.
    """
    pool = list(candidates if candidates is not None else windows())
    if not pool:
        return None
    scored = [
        Window(
            handle=window.handle,
            title=window.title,
            class_name=window.class_name,
            process_id=window.process_id,
            score=_score_window(window, label),
        )
        for window in pool
    ]
    scored = [window for window in scored if window.score > 0.0]
    if not scored:
        return None
    scored.sort(key=lambda window: (-window.score, len(window.title)))
    contenders = [window for window in scored if window.score >= CONTENT_PROBE_SCORE][
        :CONTENT_PROBE_LIMIT
    ]
    if len(contenders) < 2:
        return scored[0]
    weighed = [(window, _content_weight(window.handle, inspect=inspect)) for window in contenders]
    weighed.sort(
        key=lambda pair: (-int(pair[1] > FRAME_ONLY_NAMED), -pair[0].score, -pair[1])
    )
    return weighed[0][0]


def _connect(handle: int) -> Any | None:
    """A uia wrapper for an already-running window, attached by handle.

    By handle rather than by title because titles move: the window measured above
    was "(26) WhatsApp" and is "(27) WhatsApp" one message later. `uia` is used
    here and only here -- its enumeration is unreliable, its control tree is the
    only one there is.
    """
    try:
        from pywinauto import Application  # noqa: PLC0415

        return Application(backend="uia").connect(handle=handle).window(handle=handle)
    except Exception as exc:
        _log.debug("Cannot attach uia to hwnd %s: %s", handle, exc)
        return None


def _element_control(element: Any, depth: int) -> Control | None:
    """The data half of a live element: everything a caller can be shown."""
    try:
        info = element.element_info
        name = (info.name or "").strip()
        kind = str(info.control_type or "")
        automation_id = str(getattr(info, "automation_id", "") or "")
    except Exception:
        return None
    try:
        rect = info.rectangle
        box = {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }
    except Exception:
        box = {}
    # `element_info.enabled` is a property read; `element.is_enabled()` is another
    # COM round trip, and this runs once per control on a tree of up to 400.
    try:
        enabled = bool(getattr(info, "enabled", True))
    except Exception:
        enabled = True
    return Control(
        name=name,
        kind=kind,
        automation_id=automation_id,
        enabled=enabled,
        depth=depth,
        rect=box,
    )


def _walk_live(
    handle: int,
    *,
    connect: Callable[[int], Any] | None = None,
    limit: int | None = None,
) -> list[tuple[Control, Any]]:
    """Breadth-first over one window's control tree, capped, elements kept.

    Level by level via `children()` rather than one `descendants()` call, because
    `descendants()` walks the entire tree before it returns anything -- 761 ms on
    the 449-control window -- so capping its output saves nothing. Breadth-first
    makes MAX_CONTROLS a real budget, reaches the shallow controls (which is where
    the buttons a person names live) first, and gives each control its depth free.
    """
    budget = limit or MAX_CONTROLS
    root = (connect or _connect)(handle)
    if root is None:
        return []
    found: list[tuple[Control, Any]] = []
    queue: list[tuple[Any, int]] = [(root, 0)]
    while queue and len(found) < budget:
        node, depth = queue.pop(0)
        try:
            children = list(node.children())
        except Exception:
            # One unreadable branch is not a failed walk. Windows hands out
            # elements that die mid-enumeration when the app repaints.
            continue
        for child in children:
            control = _element_control(child, depth + 1)
            if control is None:
                continue
            found.append((control, child))
            if len(found) >= budget:
                break
            queue.append((child, depth + 1))
    return found


def _walk(handle: int, *, connect: Callable[[int], Any] | None = None, fresh: bool = False) -> list[Control]:
    """The cached, data-only control tree for one window.

    Cached because "list what's in there, then click one of them" would otherwise
    walk twice, and only for TREE_TTL_S because a UI the user is sitting in front
    of moves underneath us. Actions never read this cache -- they re-resolve
    against live elements -- so a stale entry can mislead a description, never a
    click.
    """
    now = time.monotonic()
    if not fresh:
        with _lock:
            cached = _tree_cache.get(handle)
        if cached and now - cached[0] <= TREE_TTL_S:
            return cached[1]
    walked = [control for control, _ in _walk_live_warm(handle, connect=connect)]
    with _lock:
        _tree_cache[handle] = (now, walked)
    return walked


def _invalidate_tree(handle: int | None = None) -> None:
    """Forget a cached tree, because something just changed it."""
    with _lock:
        if handle is None:
            _tree_cache.clear()
        else:
            _tree_cache.pop(handle, None)


#: Above this many named controls a window has a UI in it; at or below it, all we
#: are looking at is the frame. Measured: the WhatsApp frame window and the
#: Telegram window both report exactly 5 named controls (the caption buttons),
#: while the WhatsApp window a person actually uses reports 200.
FRAME_ONLY_NAMED = 5

#: Chromium turns its accessibility tree on when something asks for it, so the
#: first walk of an Electron window can arrive before the tree does. Measured on
#: this machine, same window, walks 250 ms apart:
#:
#:     Claude       walk 1:   4 named      walk 2: 204 named
#:     Status       walk 1:   1 named      walk 2:   2 named
#:     GenOffice    walk 1: 204 named      walk 2: 204 named   (already warm)
#:
#: Without the retry, "can you see inside Claude?" answers no the first time and
#: yes forever after -- the worst kind of wrong. Paid only when the first walk
#: looks like a bare frame, which for a genuinely frame-only window costs one
#: 10 ms re-walk on top of the wait.
WARM_RETRY_S = 0.25


def _walk_live_warm(
    handle: int,
    *,
    connect: Callable[[int], Any] | None = None,
    limit: int | None = None,
) -> list[tuple[Control, Any]]:
    """`_walk_live`, plus one retry when the first look found only a frame."""
    live = _walk_live(handle, connect=connect, limit=limit)
    if sum(1 for control, _ in live if control.name) > FRAME_ONLY_NAMED:
        return live
    time.sleep(WARM_RETRY_S)
    second = _walk_live(handle, connect=connect, limit=limit)
    return second if len(second) >= len(live) else live



def _resolve_window(
    label_or_handle: str | int,
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> Window | None:
    """The window a verb should act on, by name or by handle."""
    pool = windows(enumerate_with=enumerate_with)
    if isinstance(label_or_handle, int) or str(label_or_handle).isdigit():
        wanted = int(label_or_handle)
        for candidate in pool:
            if candidate.handle == wanted:
                return candidate
        return None
    return find_window(
        str(label_or_handle),
        candidates=pool,
        inspect=lambda handle: _walk(handle, connect=connect),
    )


def _distinct(names: Iterable[str]) -> list[str]:
    """The visible names, once each, in the order they were found.

    Chromium names a list row and the group inside it identically, so a raw list of
    what is clickable in WhatsApp reads "Chats, Chats, Minimize, Minimize" -- which
    looks like a choice between two things and is one thing seen twice. The
    ambiguity check is unaffected: it runs against live elements, not this summary.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def controls(
    label_or_handle: str | int,
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Everything named inside one window, and whether we can see in at all.

    `sees_inside` is the honest half. Telegram answers every question here with a
    window frame and five caption buttons, and a caller that treats that as "no
    Send button exists" would be wrong -- the button exists, UIA just cannot see
    it. Say which of the two it is, so `app_control` can fall back to keystrokes
    instead of reporting a missing control.
    """
    window = _resolve_window(label_or_handle, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return {
            "ok": False,
            "window": None,
            "controls": [],
            "sees_inside": False,
            "detail": f'No open window answers to "{label_or_handle}".',
        }
    named = [control for control in _walk(window.handle, connect=connect, fresh=fresh) if control.name]
    sees_inside = len(named) > FRAME_ONLY_NAMED
    return {
        "ok": True,
        "window": window.as_dict(),
        "controls": [control.as_dict() for control in named],
        "actionable": _distinct(control.name for control in named if control.actionable),
        "editable": _distinct(control.name for control in named if control.editable),
        "sees_inside": sees_inside,
        "detail": (
            f'{len(named)} named controls in "{window.title}".'
            if sees_inside
            else (
                f'"{window.title}" publishes only its window frame ({len(named)} named '
                "controls), so its buttons cannot be targeted by name. Qt and some game "
                "UIs draw themselves and expose nothing; use the keystroke path."
            )
        ),
    }


#: Below this, the best-named control is not what was asked for. Set so that a
#: single shared word out of two ("send" against "Send later") does not count as a
#: match on its own: 0.6 * 0.5 = 0.30.
MIN_NAME_SCORE = 0.45

#: Two scores this close are a tie, and a tie is a question, not a coin toss.
AMBIGUOUS_MARGIN = 0.08

#: At or above this, the match is the thing itself -- an exact name, an
#: automation_id, or a prefix that is most of the name. Below it the match is
#: circumstantial, which is what makes it worth spending a second walk on.
STRONG_NAME_SCORE = 0.85


def _score_name(control: Control, wanted: str) -> float:
    """How well this control answers to `wanted`.

    Partial matches are scaled by how much of the control's name the request
    actually covers, because a substring is only evidence in proportion to its
    share of the whole. Measured on WhatsApp: asking for "Maybe Heydevops" found
    that text inside a 200-character chat-list row -- 7% of it, the rest being a
    timestamp and a forwarded advert -- and scored it 0.70, the same as a genuine
    partial name. Clicking a chat row because an unrelated message quoted the name
    of another one is the exact wrong-conversation failure this module exists to
    avoid.
    """
    name = control.name.lower()
    target = (wanted or "").strip().lower()
    if not target:
        return 0.0
    if control.automation_id and control.automation_id.lower() == target:
        return 0.95
    if not name:
        return 0.0
    if name == target:
        return 1.0
    coverage = len(target) / len(name)
    if name.startswith(target) or name.endswith(target):
        return 0.85 if coverage >= 0.5 else 0.55 + 0.6 * coverage
    if target in name:
        return 0.7 if coverage >= 0.5 else 0.45 + 0.5 * coverage
    wanted_tokens = set(_tokens(target))
    if not wanted_tokens:
        return 0.0
    return 0.6 * (len(wanted_tokens & set(_tokens(name))) / len(wanted_tokens))


@dataclass(frozen=True)
class Resolution:
    """Which control was meant -- or, when that is unclear, the question to ask."""

    control: Control | None = None
    element: Any = None
    candidates: tuple[Control, ...] = ()
    question: str = ""
    detail: str = ""
    #: How strongly the name matched, on `_score_name`'s scale. Kept because a
    #: caller deciding whether to look harder needs to know how good this was, and
    #: 0.49 ("that text appears somewhere in a 200-character label") and 1.00 ("that
    #: is its name") are otherwise the same answer.
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.element is not None

    @property
    def ambiguous(self) -> bool:
        return bool(self.question)

    def as_dict(self) -> dict[str, Any]:
        return {
            "control": self.control.as_dict() if self.control else None,
            "candidates": [control.as_dict() for control in self.candidates],
            "question": self.question,
            "detail": self.detail,
            "score": round(self.score, 2),
        }


def _rank(
    pool: Iterable[tuple[Control, Any]],
    name: str,
) -> list[tuple[float, Control, Any]]:
    """Candidates for `name`, best first, shallowest breaking equal scores."""
    scored = [(_score_name(control, name), control, element) for control, element in pool]
    scored.sort(key=lambda row: (-row[0], row[1].depth, len(row[1].name)))
    return scored


def resolve(
    handle: int,
    name: str,
    *,
    want: frozenset[str] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> Resolution:
    """Which control `name` means, or a question when more than one answers to it.

    This is the whole point of the module. Guessing between two controls called
    "Send" is how an automation sends a message to the wrong conversation, so a tie
    comes back as something to ask rather than something resolved by tree order.

    Two passes, and only ever two. The first is the ordinary capped walk. If that
    walk stopped because it hit the cap, and what it came back with is either
    nothing or only a circumstantial match, then the answer has not been earned
    yet -- so it looks again with `DEEP_CONTROLS`. A window smaller than the cap is
    answered on one walk, which is almost every window, and so is a window where the
    first pass found the thing by name; the second pass is paid only where it could
    change the answer.
    """
    live = _walk_live_warm(handle, connect=connect)
    if not live:
        return Resolution(detail="Nothing inside this window is readable through UIA.")
    found = _resolve_from(live, name, want=want)
    if len(live) >= MAX_CONTROLS and not _good_enough(found):
        deeper = _walk_live_warm(handle, connect=connect, limit=DEEP_CONTROLS)
        if len(deeper) > len(live):
            better = _resolve_from(deeper, name, want=want)
            if _good_enough(better) or _looked_but_missed(found):
                return better
    return found


def _good_enough(found: Resolution) -> bool:
    """True when the first pass matched something by name, tie or disabled included.

    A question counts: two controls that both answer to the name is a finding, and
    walking deeper to add a third option to a question the user already has to
    answer makes it worse rather than better.
    """
    if found.question:
        return True
    if found.control is None:
        return False
    return found.score >= STRONG_NAME_SCORE


def _looked_but_missed(found: Resolution) -> bool:
    """True for "no control by that name" -- not for a match, a tie or a disabled one."""
    return found.control is None and found.element is None and not found.question


def _resolve_from(
    live: list[tuple[Control, Any]],
    name: str,
    *,
    want: frozenset[str] | None = None,
) -> Resolution:
    """Pick `name` out of an already-walked tree."""
    pool = [pair for pair in live if pair[0].name or pair[0].automation_id]
    if want:
        # A `Button` called Send and a `Text` called Send are both matches by name;
        # only one of them is clickable. Narrow by kind first, and only if that
        # leaves anything -- an app that mislabels its control types should still
        # be operable.
        preferred = [pair for pair in pool if pair[0].kind in want]
        if preferred:
            pool = preferred
    ranked = _rank(pool, name)
    if not ranked or ranked[0][0] < MIN_NAME_SCORE:
        visible: list[str] = []
        for _, control, _ in ranked:
            if control.name and control.name not in visible:
                visible.append(control.name)
            if len(visible) >= 8:
                break
        return Resolution(
            detail=(
                f'Nothing in this window is called "{name}". What I can see: '
                + (", ".join(visible) if visible else "nothing named")
                + "."
            )
        )
    best = ranked[0]
    tied = [row for row in ranked if row[0] >= best[0] - AMBIGUOUS_MARGIN]
    # Two controls with the same visible label are not a choice a person can make:
    # File Explorer's search box is an `Edit` called "Search Downloads" next to a
    # `Text` called "Search Downloads", and asking which one is a question about
    # UIA rather than about intent. Same label -> prefer whichever can be operated.
    # Different labels -> that is a real choice, and it gets asked.
    labels = {control.name.strip().lower() for _, control, _ in tied}
    if len(labels) > 1:
        candidates = tuple(control for _, control, _ in tied[:4])
        listed = "; ".join(f'the {control.kind} "{control.name}"' for control in candidates)
        return Resolution(
            candidates=candidates,
            question=f'More than one thing here answers to "{name}": {listed}. Which one did you mean?',
            detail="Ambiguous target, so nothing was touched.",
        )
    operable = [row for row in tied if row[1].actionable or row[1].editable]
    score, control, element = operable[0] if operable else tied[0]
    if not control.enabled:
        return Resolution(
            control=control, score=score, detail=f'"{control.name}" is disabled right now.'
        )
    return Resolution(
        control=control,
        element=element,
        score=score,
        detail=f'Matched "{control.name}" ({control.kind or "unknown kind"}) at {score:.2f}.',
    )


def _method(element: Any, name: str) -> Callable[..., Any] | None:
    """`getattr`, except that a pywinauto wrapper can raise on attribute *access*.

    Measured: `hasattr(element, "iface_value")` raises `NoPatternInterfaceError`
    rather than returning False, because the interface is a lazy property that goes
    to COM when it is read. A plain `getattr(..., None)` only swallows
    AttributeError, so it would let that escape and abort the whole attempt list.
    """
    try:
        found = getattr(element, name, None)
    except Exception:
        return None
    return found if callable(found) else None


def _no_window(action: str, label: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "window": None,
        "detail": f'No open window answers to "{label}".',
    }


def _blocked(action: str, window: Window, resolution: Resolution) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "action": action, "window": window.as_dict()}
    payload.update(resolution.as_dict())
    return payload


def focus(
    label: str | int,
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Bring a window forward. Real side effect, so only from an explicit verb.

    `app_control`'s focus path matches window titles by substring through
    `pygetwindow`; this one has already scored the candidates, so "WhatsApp"
    resolves to the window with the conversation in it rather than the empty frame
    that sorts first.
    """
    window = _resolve_window(label, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return _no_window("focus", label)
    root = (connect or _connect)(window.handle)
    if root is None:
        return {
            "ok": False,
            "action": "focus",
            "window": window.as_dict(),
            "detail": "Could not attach to that window through UIA.",
        }
    try:
        root.set_focus()
    except Exception as exc:
        # Windows refuses SetForegroundWindow from a process that does not own the
        # foreground. That is a real answer, not a bug to swallow.
        return {
            "ok": False,
            "action": "focus",
            "window": window.as_dict(),
            "detail": f"Windows refused to bring that window forward ({exc}).",
        }
    _invalidate_tree(window.handle)
    return {
        "ok": True,
        "action": "focus",
        "window": window.as_dict(),
        "detail": f'Focused "{window.title}".',
    }


#: How to press a control, in order of preference. The first three are UIA
#: patterns: they tell the control it was activated and never touch the pointer, so
#: they work on a window that is behind another one and they cannot land on
#: whatever the mouse happens to be over. `click` is pywinauto's own wrapper, which
#: prefers the pattern too and falls back to the pointer itself.
INVOKE_ORDER = ("invoke", "select", "toggle", "click")


def click(
    label: str | int,
    name: str,
    *,
    allow_pointer: bool = False,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Press the control called `name` in the window called `label`.

    `allow_pointer` is off by default and it is the difference between this and the
    coordinate path: with it off, nothing here moves the user's mouse. If no
    invocation pattern is available the call fails and says so, rather than
    silently driving the pointer across the desktop to a rectangle it read a moment
    ago.
    """
    window = _resolve_window(label, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return _no_window("click", label)
    resolution = resolve(window.handle, name, want=ACTIONABLE_KINDS, connect=connect)
    if not resolution.ok:
        return _blocked("click", window, resolution)
    attempts: list[str] = list(INVOKE_ORDER)
    if allow_pointer:
        attempts.append("click_input")
    failures: list[str] = []
    for attempt in attempts:
        method = _method(resolution.element, attempt)
        if method is None:
            continue
        try:
            method()
        except Exception as exc:
            failures.append(f"{attempt}: {exc}")
            continue
        _invalidate_tree(window.handle)
        return {
            "ok": True,
            "action": "click",
            "window": window.as_dict(),
            "control": resolution.control.as_dict() if resolution.control else None,
            "via": attempt,
            "detail": f'Clicked "{resolution.control.name if resolution.control else name}" via {attempt}.',
        }
    return {
        "ok": False,
        "action": "click",
        "window": window.as_dict(),
        "control": resolution.control.as_dict() if resolution.control else None,
        "detail": (
            "That control exposes no way to be activated"
            + (f" ({'; '.join(failures)})" if failures else "")
            + ("." if allow_pointer else ", and the pointer was not allowed.")
        ),
    }


#: Ways to put text into a field, in order of preference. The first three set the
#: whole value in one call, which is the reason this module exists: `typewrite`
#: sends N keystrokes to whatever has focus at each of N moments, and a dialog
#: appearing halfway through splits the message across two windows.
SET_TEXT_ORDER = ("set_edit_text", "set_text")


def _read_back(element: Any) -> str | None:
    """What the field says now, when it will tell us. None when it won't."""
    for reader in ("get_value", "window_text"):
        method = _method(element, reader)
        if method is None:
            continue
        try:
            value = method()
        except Exception:
            continue
        if isinstance(value, str):
            return value
    try:
        texts = element.texts()
    except Exception:
        return None
    if isinstance(texts, (list, tuple)):
        joined = "".join(str(part) for part in texts if part)
        return joined or None
    return None


def _set_value_pattern(element: Any, text: str) -> None:
    """The raw UIA ValuePattern, for wrappers pywinauto typed as something else.

    A `Document` in Electron apps often has no `set_edit_text` on its wrapper while
    the underlying element supports SetValue perfectly well.
    """
    element.iface_value.SetValue(text)


def _has_focus(element: Any) -> bool:
    """Whether this control holds the keyboard focus. False when it won't say."""
    method = _method(element, "has_keyboard_focus")
    if method is None:
        return False
    try:
        return bool(method())
    except Exception:
        return False


def _write_text(
    window: Window,
    control: Control | None,
    element: Any,
    text: str,
    *,
    allow_keys: bool,
) -> dict[str, Any]:
    """Set one field's whole value, best method first.

    `verified` is reported separately from `ok`: some fields read their value back
    and some do not, and "I set it and the field agrees" is a different claim from
    "the call returned without raising". A caller about to press Send can check.
    """
    setters: list[tuple[str, Callable[[], Any]]] = []
    for attempt in SET_TEXT_ORDER:
        found = _method(element, attempt)
        if found is not None:
            setters.append((attempt, (lambda method=found: method(text))))
    setters.append(("value_pattern", lambda: _set_value_pattern(element, text)))
    keys = _method(element, "type_keys") if allow_keys else None
    if keys is not None:
        # Keystrokes again, but aimed at this control rather than at the focus, and
        # only after the field has been resolved by name. Strictly better than the
        # pyautogui path, strictly worse than setting the value.
        setters.append(("type_keys", lambda: keys(text, with_spaces=True, with_newlines=False)))
    label = control.name if control else "that field"
    failures: list[str] = []
    for attempt, setter in setters:
        try:
            setter()
        except Exception as exc:
            failures.append(f"{attempt}: {exc}")
            continue
        _invalidate_tree(window.handle)
        seen = _read_back(element)
        return {
            "ok": True,
            "action": "set_text",
            "window": window.as_dict(),
            "control": control.as_dict() if control else None,
            "via": attempt,
            "verified": seen is not None and text.strip() in seen,
            "detail": f'Set "{label}" via {attempt}.',
        }
    return {
        "ok": False,
        "action": "set_text",
        "window": window.as_dict(),
        "control": control.as_dict() if control else None,
        "detail": f'"{label}" would not take a value directly (' + "; ".join(failures) + ").",
    }


def set_text(
    label: str | int,
    name: str,
    text: str,
    *,
    allow_keys: bool = False,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Put `text` into the named field, in one call rather than as keystrokes."""
    window = _resolve_window(label, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return _no_window("set_text", label)
    resolution = resolve(window.handle, name, want=EDITABLE_KINDS, connect=connect)
    if not resolution.ok:
        return _blocked("set_text", window, resolution)
    return _write_text(
        window,
        resolution.control,
        resolution.element,
        text,
        allow_keys=allow_keys,
    )


def type_into(
    label: str | int,
    text: str,
    *,
    name: str = "",
    allow_keys: bool = False,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Type into the right field when the caller never named one.

    `app_control`'s `type_text` verb carries no target, because it was written for
    `pyautogui.typewrite`, which has no notion of one. So the field is chosen here,
    and only when the choice is unambiguous: whatever holds the keyboard focus, or
    the single editable control in the window. Two boxes and no focus is a question
    rather than a coin toss -- putting a message into the search bar instead of the
    conversation is exactly the failure this module exists to stop.
    """
    if name.strip():
        return set_text(
            label,
            name,
            text,
            allow_keys=allow_keys,
            enumerate_with=enumerate_with,
            connect=connect,
        )
    window = _resolve_window(label, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return _no_window("type_text", label)
    live = _walk_live_warm(window.handle, connect=connect)
    named = sum(1 for control, _ in live if control.name)
    fields = [pair for pair in live if pair[0].editable and pair[0].enabled]
    if not fields:
        return {
            "ok": False,
            "action": "type_text",
            "window": window.as_dict(),
            "sees_inside": named > FRAME_ONLY_NAMED,
            "detail": (
                f'No text field is visible inside "{window.title}" through UIA'
                + (
                    "; only its window frame is published, so keystrokes are the only way in."
                    if named <= FRAME_ONLY_NAMED
                    else ", though the rest of its controls are readable."
                )
            ),
        }
    chosen = fields[0] if len(fields) == 1 else None
    if chosen is None:
        focused = [pair for pair in fields if _has_focus(pair[1])]
        if len(focused) == 1:
            chosen = focused[0]
    if chosen is None:
        candidates = tuple(control for control, _ in fields[:4])
        listed = "; ".join(f'"{control.name or control.kind}"' for control in candidates)
        return {
            "ok": False,
            "action": "type_text",
            "window": window.as_dict(),
            "sees_inside": True,
            "candidates": [control.as_dict() for control in candidates],
            "question": (
                f'There are {len(fields)} places to type in "{window.title}" and none of them '
                f"has the cursor: {listed}. Which one?"
            ),
            "detail": "Ambiguous field, so nothing was typed.",
        }
    return _write_text(window, chosen[0], chosen[1], text, allow_keys=allow_keys)


def close(
    label: str | int,
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
    connect: Callable[[int], Any] | None = None,
) -> dict[str, Any]:
    """Ask a window to close. WM_CLOSE, so the app can still refuse or prompt."""
    window = _resolve_window(label, enumerate_with=enumerate_with, connect=connect)
    if window is None:
        return _no_window("close", label)
    root = (connect or _connect)(window.handle)
    if root is None:
        return {
            "ok": False,
            "action": "close",
            "window": window.as_dict(),
            "detail": "Could not attach to that window through UIA.",
        }
    try:
        root.close()
    except Exception as exc:
        return {
            "ok": False,
            "action": "close",
            "window": window.as_dict(),
            "detail": f"That window would not close ({exc}).",
        }
    _invalidate_tree(window.handle)
    return {
        "ok": True,
        "action": "close",
        "window": window.as_dict(),
        "detail": f'Asked "{window.title}" to close.',
    }


def overview(
    *,
    enumerate_with: Callable[[], Sequence[Any]] | None = None,
) -> dict[str, Any]:
    """Capability plus the open windows, without reading a single control tree.

    Cheap on purpose -- 42 ms measured -- so a UI can list what is open without
    paying the ~760 ms a tree walk can cost.
    """
    probe = capability()
    found = windows(enumerate_with=enumerate_with) if probe.installed else []
    return {
        "capability": probe.as_dict(),
        "windows": [window.as_dict() for window in found],
        "count": len(found),
    }


def reset_for_tests() -> None:
    """Drop every cached tree. Tests share a process; caches should not."""
    _invalidate_tree(None)
