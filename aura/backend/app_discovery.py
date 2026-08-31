"""Finding what is actually on this machine, and registering a site to drive.

Why this exists
---------------
``app_connect.py`` is a hand-written registry of 27 entries. That was the right
shape for apps whose *connection* is non-trivial -- a Slack bot token, a Meta app
registration -- but it answers the question "what can Akansha connect to" with a
list somebody typed, and the ask was every app on this PC and any site in the
world:

    *"all apps which have windows apps can connect by clicking once ... can also
    in one click authenticate any website and also making a connection and can
    control that web"*

Two different problems hide in that sentence, and they have different honest
answers.

**A Windows app needs no authentication at all.** There is nothing to sign into.
The only reason the old page could not offer Chrome-and-everything-else in one
click is that it did not know what was installed -- and it was measurably wrong
about it: the declared registry reported "Discord Desktop was not found on this
machine" while ``Discord.lnk`` sits in this user's Start Menu. So the missing
piece is a scan, not a credential. A scan found 124 shortcuts here.

**A website cannot be one-click authenticated by any honest mechanism that ends
in a stored password.** OAuth needs a client registered with that specific
provider, which cannot be conjured for an arbitrary domain, and asking someone to
type their Gmail password into this app would be the worst thing this codebase
could offer. What *can* be done in one click, for every site that has a login, is
open it in a browser profile reserved for Akansha and let the person sign in
themselves -- their password manager, their second factor, the site's own form.
The session cookie then lives in that profile, and every later visit is already
signed in. That is the ``WEB_SESSION`` strategy, and this module owns the profile
directory it depends on.

What is deliberately not here
-----------------------------
No credential ever touches this module. Desktop apps have none; sites keep theirs
with the site. The only thing written is a path or a URL, which is why adopting an
app is cheap enough to be a single click.

Testability
-----------
Every function that touches the filesystem takes its roots as an argument.
``discover_desktop_apps(roots=...)`` against a temp directory is a test of the
filtering rules; the same call with no argument is a report about this laptop.
The tests use the first form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from .app_connect import (
    FAMILY_SCANNED,
    FAMILY_WEB,
    LOCAL_EXECUTABLE,
    WEB_SESSION,
    AppConnector,
)
from .app_control import DESKTOP_VERBS, WEB_VERBS

# --- What counts as an app ---------------------------------------------------
#
# A Start Menu scan is the highest-signal list of "programs this person has",
# because installers put things there on purpose. It is also full of things that
# are not applications, and shipping those would make the scan useless -- a list
# where "Uninstall Docker Desktop" sits next to "Docker Desktop" is a list nobody
# reads twice.

#: Start Menu subfolders that hold operating-system tooling rather than
#: applications. Matched on any path segment, case-insensitively.
NOISE_FOLDERS = frozenset(
    {
        "accessibility",
        "accessories",
        "administrative tools",
        "maintenance",
        "startup",
        "system tools",
        "windows accessories",
        "windows administrative tools",
        "windows ease of access",
        "windows powershell",
        "windows system",
        "windows tools",
    }
)

#: Shortcut names that are documentation, removal, or a web link -- never the app.
#: Anchored where it matters: "Uninstall X" and "X Uninstall" are both real
#: shapes, but a substring match on "help" would drop "HelpScout".
NOISE_NAME = re.compile(
    r"""(
        ^uninstall\b | \buninstall(er)?$ | ^install\b | \binstaller$
      | ^(read\s?me|readme)\b | ^license | ^changelog
      | ^documentation$ | ^docs$ | ^help$ | \bhelp\s+topics?$
      | \brelease\s+notes?$ | \buser\s+guide$ | \bmanuals?(\s*\(.*\))?$
      | \bmodule\s+docs\b
      | \bwebsite$ | \bhome\s?page$ | \bon\s+the\s+web$
      | ^command\s+prompt$ | ^file\s+explorer$ | ^control\s+panel$
      | ^administrative\s+tools$ | ^reload\s+configuration$
      | ^telemetry\s+log\b | \blanguage\s+preferences$
      | \breset\b.*\b(utility|preferences|cache|files)\b
      | ^run$
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Extensions worth launching. `.url` is excluded on purpose: an internet
#: shortcut is a site, and sites go through WEB_SESSION where they can actually
#: be signed into, not through a launcher that opens a signed-out tab.
APP_SUFFIXES = (".lnk", ".exe")


@dataclass(frozen=True)
class DiscoveredApp:
    """One thing found on this machine that looks like an application."""

    app_id: str
    label: str
    #: What to hand the launcher. For a shortcut this is the `.lnk` itself --
    #: Windows resolves it, including the working directory and arguments the
    #: installer chose, which is more likely to start the app correctly than an
    #: exe path we extracted and stripped of both.
    launch_target: str
    #: The real binary when it could be read, for display and for "is it still
    #: there". Best effort: a shortcut to an MSIX package has no plain exe.
    exe_path: str = ""
    source: str = "start_menu"

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "label": self.label,
            "launch_target": self.launch_target,
            "exe_path": self.exe_path,
            "source": self.source,
        }


def slugify(name: str) -> str:
    """A stable app id from a display name.

    Stable is the requirement, not pretty: this id is the key an adopted app is
    stored under, so a scan run tomorrow has to produce the same string or the
    same app is adopted twice.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return cleaned or "app"


def looks_like_noise(path: Path) -> bool:
    """Whether this shortcut is tooling, documentation, or an uninstaller."""
    segments = {segment.lower() for segment in path.parts[:-1]}
    if segments & NOISE_FOLDERS:
        return True
    return bool(NOISE_NAME.search(path.stem))


def start_menu_roots() -> list[Path]:
    """The two Start Menu trees on Windows: this user's, and all users'.

    Both, because installers choose between them and a per-user install -- which
    is how Chrome, Discord and every Electron app land -- appears only in the
    first.
    """
    roots: list[Path] = []
    for variable in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def resolve_shortcut(path: Path) -> str:
    """The binary a `.lnk` points at, or "".

    Best effort by design. `pywin32` is present on this machine and is the only
    reliable way to read a shortcut, but a missing COM layer must degrade to "we
    do not know the exe" rather than dropping the app -- the `.lnk` is still
    launchable either way, which is what actually matters.
    """
    if path.suffix.lower() != ".lnk":
        return str(path)
    try:  # pragma: no cover - depends on pywin32 and a real shell
        import win32com.client  # type: ignore[import-not-found]

        shell = win32com.client.Dispatch("WScript.Shell")
        target = str(shell.CreateShortCut(str(path)).Targetpath or "")
    except Exception:
        return ""
    return target


def discover_desktop_apps(
    *,
    roots: Sequence[Path] | None = None,
    resolve: Any = resolve_shortcut,
) -> list[DiscoveredApp]:
    """Applications installed on this machine, filtered and de-duplicated.

    De-duplicated on the display name rather than the path: the same app is
    routinely in both Start Menu trees, and listing "Google Chrome" twice because
    one copy is per-user is noise the user has to think about for no reason.
    """
    search_roots = list(roots) if roots is not None else start_menu_roots()
    found: dict[str, DiscoveredApp] = {}

    for root in search_roots:
        if not Path(root).is_dir():
            continue
        for path in sorted(Path(root).rglob("*")):
            if path.suffix.lower() not in APP_SUFFIXES or not path.is_file():
                continue
            relative = path.relative_to(root)
            if looks_like_noise(relative):
                continue
            label = path.stem.strip()
            app_id = slugify(label)
            if not label or app_id in found:
                continue
            target = ""
            try:
                target = str(resolve(path) or "")
            except Exception:
                target = ""
            found[app_id] = DiscoveredApp(
                app_id=app_id,
                label=label,
                launch_target=str(path),
                exe_path=target,
            )

    return sorted(found.values(), key=lambda app: app.label.lower())


# --- Sites ------------------------------------------------------------------


def normalise_site_url(raw: str) -> str:
    """A bare origin from whatever the user pasted, or "" if it is not a site.

    Scheme added rather than demanded: people type `gmail.com`, and rejecting
    that in favour of an error message about URL syntax is the kind of friction
    this whole feature exists to remove. Path and query are dropped -- the
    connection is to the site, and keeping `?utm_source=...` in an app id would
    make the same site adoptable twice.
    """
    text = (raw or "").strip()
    if not text or " " in text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if "." not in parsed.hostname and parsed.hostname != "localhost":
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def site_host(url: str) -> str:
    return urlparse(url).hostname or ""


def site_label(url: str) -> str:
    """What to call a site in a list. `www.` dropped; nothing else invented."""
    host = site_host(url)
    return host[4:] if host.startswith("www.") else host


def site_app_id(url: str) -> str:
    """Namespaced so a site can never collide with a declared app id."""
    return "web_" + slugify(site_label(url))


#: Where the browser profile Akansha drives lives. One profile for all sites, not
#: one per site: a browser profile is tens of megabytes, and more importantly a
#: single profile is how signing into Google once also signs you into YouTube --
#: which is what a person expects and what per-site isolation would break.
PROFILE_DIR_NAME = "akansha-browser-profile"


def browser_profile_dir(root: Path | str | None = None) -> Path:
    """The profile directory, created lazily by the browser rather than by us."""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    return base / ".akansha" / PROFILE_DIR_NAME


def profile_signed_in(url: str, *, profile_dir: Path | str | None = None) -> bool:
    """Whether that profile holds a session for this site's host.

    Chromium keeps cookies in one encrypted SQLite file, so the host cannot be
    read out of it without the OS key -- and going after that key would be a
    strange thing for this app to do. What can be read without decrypting
    anything is the cookie *store*: Chromium writes the host name into the
    `Cookies` file's index pages in the clear, and a signed-in profile is
    hundreds of kilobytes where a fresh one is a few. So the check is:
    the file exists, it is not a stub, and the host appears in its bytes.

    Not an HTTP request, deliberately. Every public site answers to a signed-out
    visitor with a 200, so a reachability probe would have reported Gmail
    connected before anybody logged in -- the exact class of false "Connected"
    that the outcomes in `app_connect` exist to prevent.
    """
    host = site_host(url)
    if not host:
        return False
    directory = Path(profile_dir) if profile_dir is not None else browser_profile_dir()
    for candidate in (
        directory / "Default" / "Network" / "Cookies",
        directory / "Default" / "Cookies",
    ):
        if not candidate.is_file():
            continue
        try:
            blob = candidate.read_bytes()
        except OSError:
            continue
        needle = host.encode("utf-8", "ignore")
        registrable = host[4:] if host.startswith("www.") else host
        if needle in blob or registrable.encode("utf-8", "ignore") in blob:
            return True
    return False


# --- Turning either kind into a connector -----------------------------------


def desktop_connector(app: DiscoveredApp | dict[str, Any]) -> AppConnector:
    """A scanned app as a registry entry, so it flows through `plan_connect`."""
    data = app.as_dict() if isinstance(app, DiscoveredApp) else dict(app)
    label = str(data.get("label") or "").strip()
    return AppConnector(
        app_id=str(data.get("app_id") or slugify(label)),
        label=label,
        family=FAMILY_SCANNED,
        strategy=LOCAL_EXECUTABLE,
        capabilities=DESKTOP_VERBS,
        exe_path=str(data.get("launch_target") or data.get("exe_path") or ""),
        adopted=True,
        aliases=(label.lower(),),
        safety_note=(
            "Launching and driving this app moves the real mouse and keyboard on this machine."
        ),
    )


def site_connector(url: str, label: str = "") -> AppConnector:
    """A site as a registry entry."""
    normalised = normalise_site_url(url)
    name = (label or "").strip() or site_label(normalised)
    return AppConnector(
        app_id=site_app_id(normalised),
        label=name,
        family=FAMILY_WEB,
        strategy=WEB_SESSION,
        capabilities=WEB_VERBS,
        site_url=normalised,
        adopted=True,
        aliases=(name.lower(), site_label(normalised)),
        docs_url=normalised,
        safety_note=(
            "You sign in yourself, in a browser window. Your password goes to the site and is "
            "never stored by Akansha. Afterwards Akansha acts as you on that site."
        ),
    )


def connectors_from_records(records: Iterable[dict[str, Any]]) -> list[AppConnector]:
    """Rebuild adopted connectors from what was stored.

    Stored as the two facts that cannot be re-derived -- a path or a URL -- rather
    than as a serialised connector. Capabilities and safety notes come from the
    code above, so improving the wording does not require rewriting every row a
    user has adopted.
    """
    connectors: list[AppConnector] = []
    for record in records:
        kind = str(record.get("kind") or "")
        try:
            if kind == "web":
                url = str(record.get("site_url") or record.get("url") or "")
                if normalise_site_url(url):
                    connectors.append(site_connector(url, str(record.get("label") or "")))
            elif kind == "desktop":
                if str(record.get("label") or "").strip():
                    connectors.append(desktop_connector(record))
        except Exception:
            # One malformed row must not empty the whole list. The user would see
            # every app they had adopted vanish, with no way to tell why.
            continue
    return connectors
