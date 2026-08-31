"""One registry and one dispatcher for connecting Akansha to an app -- any app.

Before this module there was no single answer to "what can Akansha connect to,
how, and is it connected right now." There were three unrelated mechanisms:

* social platforms went through ``/api/social/setup/{platform}`` against a
  hardcoded five-entry catalog in ``social_connectors.py``;
* desktop applications were launch commands in ``automation.py`` with no concept
  of being connected at all -- the only question that code could answer was
  "did the process start";
* model providers were a third surface again, and ``/api-keys`` turned out to be
  display-only: the keys it shows are hardcoded placeholder strings, the reveal
  toggle unmasks a dummy, and the add button adds nothing.

Three families, three mechanisms, no shared vocabulary, and adding a fourth kind
of app meant inventing a fourth. So the design here is deliberately not "one more
integration": it is a registry of declarative entries plus a *closed set* of
connect strategies. Adding an app is adding data. Adding a strategy is the rare
event, and there are four.

What "one click" can honestly mean
----------------------------------
One click cannot mean zero credentials, and pretending otherwise is the failure
mode this module is built to avoid. A button that reports "Connected" without a
credential is a lie that costs more than the friction it saved, because the next
thing the user does is rely on it.

So a click resolves to exactly one of six outcomes, and the ones that are not
success say precisely what is missing:

``connected``            verified just now, with a label for what it connected to
``needs_input``          the exact credential fields still required, by name
``needs_authorization``  an authorize URL for the client to open
``not_installed``        the app is not on this machine, and what was looked for
``unreachable``          a self-hosted bridge is configured but did not answer
``disconnected``         nothing stored yet and nothing attempted

The distinction between ``needs_input`` and ``not_installed`` matters: the first
is answerable by typing, the second is not answerable by this application at all.
Collapsing both into a generic failure is what makes an integrations page useless.

Storage
-------
Nothing new. Credentials live where the social connectors already put them --
the ``integration_connections`` row for that provider, with the config blob
encrypted by ``encrypt_social_config`` (Windows DPAPI where available, local
scheme otherwise). A second credential store would be a second thing to leak.

Testability
-----------
Probing asks two questions about the world: does this executable exist, and does
this URL answer. Both are injected rather than imported, so the tests exercise
the real dispatch logic without touching the filesystem or the network. That is
not ceremony -- a test that shells out to find Chrome passes or fails based on
what the machine happens to have installed, which makes it a machine report
rather than a test of this code.

Authority
---------
Connecting an app grants Akansha standing authority over an external account or
this desktop. That is exactly the class of action ``speaker_identity`` exists to
gate, so the mutating endpoints require owner access. Reading the catalog does
not: knowing which apps *could* be connected leaks nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .app_control import DESKTOP_VERBS, WEB_VERBS


# --- Strategies -------------------------------------------------------------
#
# A closed set. Each answers the same two questions in a different way: "is this
# connected" and "what is the single next step toward connecting it". Resist
# adding a fifth for one app -- if an app does not fit, it almost always fits one
# of these with different data.

#: Installed on this machine. Connected iff the binary resolves. There is no
#: credential and no network; the only failure is absence.
LOCAL_EXECUTABLE = "local_executable"

#: A long-lived secret the user pastes once. Connected iff every required field
#: is stored non-empty and the verify probe accepts it.
API_KEY = "api_key"

#: Delegated authorization. Connected iff a token is stored. The one click is
#: "open the consent screen", which is genuinely one click -- the rest happens
#: at the callback.
OAUTH2 = "oauth2"

#: A self-hosted gateway the user runs (OpenWA, a whatsapp-web.js bridge, an
#: Ollama server). Connected iff the health URL answers. Distinct from API_KEY
#: because the failure mode is "not running", not "wrong secret".
LOCAL_BRIDGE = "local_bridge"

#: A website Akansha drives through a real browser profile it owns.
#:
#: This is the fifth strategy, and the docstring above says to resist adding one.
#: It earns the exception because it is not a variation on the other four -- it is
#: the only one where *we store no credential at all*. The click opens the site in
#: a browser profile reserved for Akansha; the person signs in themselves, in a
#: real browser, with their own password manager and their own second factor. What
#: persists afterwards is the site's own session cookie, in that profile, exactly
#: as it would in any browser.
#:
#: That distinction is the whole reason this exists. The ask was *"in one click can
#: authenticate any website and also making a connection and can control that
#: web"*, and the honest form of it is this one: OAuth clients cannot be conjured
#: for an arbitrary site, and asking someone to paste their Gmail password into
#: this app would be the worst thing this codebase could offer. A browser profile
#: needs no registration with anybody, works on every site that has a login, and
#: leaves the secret where it belongs -- with the user and the site.
#:
#: Connected iff that profile holds a cookie store for the host. Not "iff the URL
#: answers": every public site answers to a signed-out visitor, so an HTTP probe
#: would report Gmail connected before anyone had logged in.
WEB_SESSION = "web_session"

STRATEGIES = (LOCAL_EXECUTABLE, API_KEY, OAUTH2, LOCAL_BRIDGE, WEB_SESSION)


# --- Outcomes ---------------------------------------------------------------

CONNECTED = "connected"
NEEDS_INPUT = "needs_input"
NEEDS_AUTHORIZATION = "needs_authorization"
NOT_INSTALLED = "not_installed"
UNREACHABLE = "unreachable"
DISCONNECTED = "disconnected"

#: WEB_SESSION only: the site is registered, nobody has signed in yet. Separate
#: from NEEDS_AUTHORIZATION because there is no consent URL and no callback -- the
#: next step is a browser window, not an OAuth round trip.
NEEDS_SIGN_IN = "needs_sign_in"

OUTCOMES = (
    CONNECTED,
    NEEDS_INPUT,
    NEEDS_AUTHORIZATION,
    NEEDS_SIGN_IN,
    NOT_INSTALLED,
    UNREACHABLE,
    DISCONNECTED,
)

#: Outcomes the user can resolve by typing something. Used by the UI to decide
#: whether to show a form or a message, and by `plan_connect` to decide whether
#: naming the missing fields is useful or merely noise.
ACTIONABLE_BY_INPUT = (NEEDS_INPUT,)


# --- Families ---------------------------------------------------------------
#
# Presentation grouping only. Deliberately not the same axis as strategy: Discord
# appears as both a desktop app and a bot API, and a user looking for "Discord"
# should find both without needing to know that one is an executable probe and
# the other is a token.

FAMILY_DESKTOP = "desktop"
FAMILY_MESSAGING = "messaging"
FAMILY_MODEL = "model_provider"
FAMILY_PRODUCTIVITY = "productivity"
FAMILY_DEVELOPER = "developer"
FAMILY_MEDIA = "media"
#: Sites the user connected by signing in once. Its own family rather than folded
#: into an existing one: a site is not a desktop app and not a model provider, and
#: burying gmail.com under "productivity" would make the one list the user built
#: themselves the hardest thing on the page to find.
FAMILY_WEB = "website"
#: Apps found by scanning this machine that matched no declared entry. Kept apart
#: from `desktop` so the hand-written entries -- the ones with real capabilities
#: beyond launch and focus -- stay visible instead of being lost in a list of 60.
FAMILY_SCANNED = "on_this_pc"


@dataclass(frozen=True)
class CredentialField:
    """One thing the user has to supply, described well enough to be findable.

    `where` is not documentation garnish. The reason integration pages stall is
    that the user cannot find the value being asked for, and "Client secret" with
    no path is a dead end.
    """

    key: str
    label: str
    where: str = ""
    secret: bool = True
    optional: bool = False


@dataclass(frozen=True)
class AppConnector:
    """One app, declared. No behaviour here -- the strategy supplies that."""

    app_id: str
    label: str
    family: str
    strategy: str
    #: What Akansha can do once connected. Shown to the user before they connect,
    #: because that is the only moment the answer changes their decision.
    capabilities: tuple[str, ...] = ()
    #: API_KEY / LOCAL_BRIDGE / OAUTH2 only.
    fields: tuple[CredentialField, ...] = ()
    #: LOCAL_EXECUTABLE only: the `automation` app name whose launch commands are
    #: probed. Kept as a reference rather than a copied path list so this registry
    #: and the launcher cannot disagree about where Chrome lives.
    launch_key: str = ""
    #: LOCAL_BRIDGE only: config key holding the base URL, and the health path.
    bridge_url_key: str = "base_url"
    bridge_health_path: str = "/"
    #: OAUTH2 only: the existing `/api/social/oauth/start/{platform}` provider, or
    #: a full URL. Named rather than constructed so a provider that already has a
    #: working flow keeps using it.
    oauth_provider: str = ""
    docs_url: str = ""
    #: Stated at the point of connection, not buried in a policy page.
    safety_note: str = ""
    aliases: tuple[str, ...] = ()
    #: LOCAL_EXECUTABLE only, and only for apps found by scanning this machine:
    #: the thing to launch, verbatim. A scanned app has no entry in
    #: `APP_LAUNCH_COMMANDS` to point a `launch_key` at, and inventing one would
    #: mean writing to that table at runtime. Checked before `launch_key` so a
    #: scanned app is judged on the path it was actually found at.
    exe_path: str = ""
    #: WEB_SESSION only: the site's origin, normalised.
    site_url: str = ""
    #: The site a person can simply *sign into*, when the declared alternative is
    #: registering a developer application. This is the one-click route, and for most
    #: of these apps it is the only route anybody will ever finish: *"not oauth client
    #: secret all processes complex one click browser login make connection yes"*.
    #: Registering an X app to post a tweet is four screens, a developer agreement and
    #: a client secret; signing into x.com is one click in a browser window.
    #:
    #: Deliberately absent on the model providers. A cookie at chatgpt.com does not
    #: let this application request a completion -- that needs an API key, and
    #: pretending otherwise would put a "Sign in" button here that connects nothing.
    #: The rule is: `web_url` is set only where being signed in as the user genuinely
    #: lets Akansha do the listed capabilities.
    web_url: str = ""
    #: True for anything the user added at runtime -- a scanned app or a site.
    #: Only these can be removed; deleting a declared entry would need a code
    #: change to come back, so the UI must not offer it.
    adopted: bool = False


def _desktop(app_id: str, label: str, launch_key: str, *, family: str = FAMILY_DESKTOP,
             capabilities: Iterable[str] = ("launch", "focus"), aliases: Iterable[str] = ()) -> AppConnector:
    """Shorthand for the many desktop entries, which differ only in name."""
    return AppConnector(
        app_id=app_id,
        label=label,
        family=family,
        strategy=LOCAL_EXECUTABLE,
        capabilities=tuple(capabilities),
        launch_key=launch_key,
        aliases=tuple(aliases),
        safety_note="Launching and driving this app moves the real mouse and keyboard on this machine.",
    )


APP_CONNECTORS: dict[str, AppConnector] = {
    # --- Desktop applications on this machine -------------------------------
    "chrome": _desktop("chrome", "Google Chrome", "chrome", capabilities=("launch", "open_url", "new_tab", "close_tab"), aliases=("google chrome",)),
    "brave": _desktop("brave", "Brave", "brave", capabilities=("launch", "open_url", "new_tab", "close_tab"), aliases=("brave browser",)),
    "edge": _desktop("edge", "Microsoft Edge", "microsoft edge", capabilities=("launch", "open_url", "new_tab", "close_tab"), aliases=("ms edge", "microsoft edge")),
    "vscode": _desktop("vscode", "Visual Studio Code", "vscode", family=FAMILY_DEVELOPER, capabilities=("launch", "open_folder"), aliases=("visual studio code", "code")),
    "antigravity": _desktop("antigravity", "Antigravity IDE", "antigravity", family=FAMILY_DEVELOPER, aliases=("antigravity ide",)),
    "notepad": _desktop("notepad", "Notepad", "notepad", family=FAMILY_PRODUCTIVITY, capabilities=("launch", "type_text")),
    "word": _desktop("word", "Microsoft Word", "word", family=FAMILY_PRODUCTIVITY, capabilities=("launch", "type_text")),
    "excel": _desktop("excel", "Microsoft Excel", "excel", family=FAMILY_PRODUCTIVITY),
    "powerpoint": _desktop("powerpoint", "Microsoft PowerPoint", "powerpoint", family=FAMILY_PRODUCTIVITY),
    "whatsapp_desktop": _desktop("whatsapp_desktop", "WhatsApp Desktop", "whatsapp", family=FAMILY_MESSAGING, capabilities=("launch", "open_chat", "type_message"), aliases=("whatsapp desktop", "whatsapp", "whats app", "whatsup", "watsup", "whatsap")),
    "telegram_desktop": _desktop("telegram_desktop", "Telegram Desktop", "telegram", family=FAMILY_MESSAGING, capabilities=("launch", "open_chat", "type_message"), aliases=("telegram desktop", "telegram")),
    "discord_desktop": _desktop("discord_desktop", "Discord Desktop", "discord", family=FAMILY_MESSAGING, capabilities=("launch", "open_channel"), aliases=("discord desktop", "discord")),
    # --- Model providers ----------------------------------------------------
    "openrouter": AppConnector(
        app_id="openrouter",
        label="OpenRouter",
        family=FAMILY_MODEL,
        strategy=API_KEY,
        capabilities=("chat", "streaming", "model_routing"),
        fields=(
            CredentialField("api_key", "API key", where="openrouter.ai/keys"),
        ),
        docs_url="https://openrouter.ai/docs",
        safety_note="Conversation text is sent to OpenRouter and on to the model you route to.",
    ),
    "openai": AppConnector(
        app_id="openai",
        label="OpenAI",
        family=FAMILY_MODEL,
        strategy=API_KEY,
        capabilities=("chat", "streaming", "embeddings", "speech"),
        fields=(
            CredentialField("api_key", "API key", where="platform.openai.com/api-keys"),
            CredentialField("organization", "Organization id", where="platform.openai.com/settings", secret=False, optional=True),
        ),
        docs_url="https://platform.openai.com/docs",
        safety_note="Conversation text is sent to OpenAI.",
    ),
    "anthropic": AppConnector(
        app_id="anthropic",
        label="Anthropic",
        family=FAMILY_MODEL,
        strategy=API_KEY,
        capabilities=("chat", "streaming", "tool_use"),
        fields=(
            CredentialField("api_key", "API key", where="console.anthropic.com/settings/keys"),
        ),
        docs_url="https://docs.anthropic.com",
        safety_note="Conversation text is sent to Anthropic.",
    ),
    "gemini": AppConnector(
        app_id="gemini",
        label="Google Gemini",
        family=FAMILY_MODEL,
        strategy=API_KEY,
        capabilities=("chat", "streaming", "multimodal"),
        fields=(
            CredentialField("api_key", "API key", where="aistudio.google.com/apikey"),
        ),
        docs_url="https://ai.google.dev/docs",
        safety_note="Conversation text is sent to Google.",
    ),
    "ollama": AppConnector(
        app_id="ollama",
        label="Ollama (local models)",
        family=FAMILY_MODEL,
        strategy=LOCAL_BRIDGE,
        capabilities=("chat", "streaming", "offline"),
        fields=(
            CredentialField("base_url", "Server URL", where="usually http://localhost:11434", secret=False),
        ),
        bridge_health_path="/api/tags",
        docs_url="https://github.com/ollama/ollama",
        safety_note="Runs on this machine, so nothing leaves it. Needs the Ollama server running.",
    ),
    # --- Messaging APIs and bridges ----------------------------------------
    "telegram_bot": AppConnector(
        app_id="telegram_bot",
        label="Telegram (bot)",
        family=FAMILY_MESSAGING,
        strategy=API_KEY,
        capabilities=("read_bot_messages", "send_messages", "webhooks"),
        fields=(
            CredentialField("bot_token", "Bot token", where="talk to @BotFather in Telegram"),
        ),
        docs_url="https://core.telegram.org/bots/api",
        safety_note="A bot only sees messages sent to it or to groups it joins, which is the point -- it cannot read your whole account.",
    ),
    "discord_bot": AppConnector(
        app_id="discord_bot",
        label="Discord (bot)",
        family=FAMILY_MESSAGING,
        strategy=API_KEY,
        capabilities=("read_allowed_channels", "send_messages", "slash_commands"),
        fields=(
            CredentialField("bot_token", "Bot token", where="discord.com/developers > your app > Bot"),
        ),
        docs_url="https://discord.com/developers/docs",
        safety_note="Use a bot account. Automating a personal Discord account is against their terms.",
    ),
    "twitter": AppConnector(
        app_id="twitter",
        label="X / Twitter",
        family=FAMILY_MESSAGING,
        strategy=OAUTH2,
        capabilities=("read_posts", "send_posts", "dm_where_permitted"),
        fields=(
            CredentialField("client_id", "OAuth client id", where="developer.x.com > your app > Keys", secret=False),
            CredentialField("client_secret", "OAuth client secret", where="developer.x.com > your app > Keys"),
        ),
        oauth_provider="twitter",
        docs_url="https://docs.x.com/x-api",
        safety_note="DM access depends on your API plan; posting does not.",
        web_url="https://x.com",
    ),
    "whatsapp_bridge": AppConnector(
        app_id="whatsapp_bridge",
        label="WhatsApp",
        family=FAMILY_MESSAGING,
        strategy=LOCAL_BRIDGE,
        capabilities=("read_messages", "send_messages", "media", "webhooks"),
        fields=(
            CredentialField("base_url", "Bridge URL", where="wherever you run OpenWA or whatsapp-web.js", secret=False),
            CredentialField("api_key", "Bridge API key", where="whatever you configured on the bridge", optional=True),
        ),
        bridge_health_path="/health",
        docs_url="https://github.com/rmyndharis/OpenWA",
        safety_note=(
            "You sign in at web.whatsapp.com by scanning the QR code with your phone, "
            "or run your own bridge. Never auto-send without a saved permission level -- "
            "WhatsApp has no undo."
        ),
        # Relabelled from "WhatsApp (self-hosted bridge)". Hosting a bridge is the most
        # complex process on this page and it was the only route offered; web.whatsapp.com
        # is the same account, one sign-in, no server to run.
        #
        # One row with two routes rather than a second `whatsapp_web` connector,
        # unlike Telegram and Discord: those two split because a bot token is a
        # *different account* from yours. A bridge drives the same WhatsApp account
        # the QR code signs into, so two rows would be two buttons for one login and
        # no way to tell which one is the real connection.
        web_url="https://web.whatsapp.com",
    ),
    "meta": AppConnector(
        app_id="meta",
        label="Meta (WhatsApp Cloud / Instagram)",
        family=FAMILY_MESSAGING,
        strategy=OAUTH2,
        capabilities=("read_messages", "send_messages", "webhooks", "business_accounts"),
        fields=(
            CredentialField("app_id", "Meta app id", where="developers.facebook.com > your app > Settings", secret=False),
            CredentialField("app_secret", "Meta app secret", where="developers.facebook.com > your app > Settings"),
        ),
        oauth_provider="meta",
        docs_url="https://developers.facebook.com/docs/messenger-platform/",
        safety_note="Instagram messaging needs a business account. Unofficial Instagram APIs are fragile and can get the account banned.",
        web_url="https://www.instagram.com",
    ),
    # --- Productivity and developer OAuth ----------------------------------
    "google": AppConnector(
        app_id="google",
        label="Google (Gmail, Calendar, Drive)",
        family=FAMILY_PRODUCTIVITY,
        strategy=OAUTH2,
        capabilities=("read_mail", "send_mail", "calendar", "drive"),
        fields=(
            CredentialField("client_id", "OAuth client id", where="console.cloud.google.com > Credentials", secret=False),
            CredentialField("client_secret", "OAuth client secret", where="console.cloud.google.com > Credentials"),
        ),
        oauth_provider="google",
        docs_url="https://developers.google.com/identity/protocols/oauth2",
        safety_note="Grant the narrowest scopes that work. Mail send is the one that cannot be undone.",
        web_url="https://mail.google.com",
    ),
    "github": AppConnector(
        app_id="github",
        label="GitHub",
        family=FAMILY_DEVELOPER,
        strategy=OAUTH2,
        capabilities=("read_repos", "issues", "pull_requests"),
        fields=(
            CredentialField("client_id", "OAuth client id", where="github.com/settings/developers", secret=False),
            CredentialField("client_secret", "OAuth client secret", where="github.com/settings/developers"),
        ),
        oauth_provider="github",
        docs_url="https://docs.github.com/apps/oauth-apps",
        safety_note="Write scopes let Akansha push and comment as you.",
        web_url="https://github.com",
    ),
    "notion": AppConnector(
        app_id="notion",
        label="Notion",
        family=FAMILY_PRODUCTIVITY,
        strategy=API_KEY,
        capabilities=("read_pages", "write_pages", "databases"),
        fields=(
            CredentialField("api_key", "Internal integration secret", where="notion.so/my-integrations"),
        ),
        docs_url="https://developers.notion.com",
        safety_note="A Notion integration only sees pages you explicitly share with it.",
        web_url="https://www.notion.so",
    ),
    "slack": AppConnector(
        app_id="slack",
        label="Slack",
        family=FAMILY_MESSAGING,
        strategy=API_KEY,
        capabilities=("read_allowed_channels", "send_messages"),
        fields=(
            CredentialField("bot_token", "Bot user OAuth token", where="api.slack.com/apps > OAuth & Permissions"),
        ),
        docs_url="https://api.slack.com/authentication/token-types",
        safety_note="Scoped to the channels the bot is invited to.",
        web_url="https://app.slack.com/client",
    ),
    "spotify": AppConnector(
        app_id="spotify",
        label="Spotify",
        family=FAMILY_MEDIA,
        strategy=OAUTH2,
        capabilities=("playback_control", "search", "playlists"),
        fields=(
            CredentialField("client_id", "Client id", where="developer.spotify.com/dashboard", secret=False),
            CredentialField("client_secret", "Client secret", where="developer.spotify.com/dashboard"),
        ),
        oauth_provider="spotify",
        docs_url="https://developer.spotify.com/documentation/web-api",
        safety_note="Playback control needs an active device and Spotify Premium.",
        web_url="https://open.spotify.com",
    ),
    "telegram_web": AppConnector(
        app_id="telegram_web",
        label="Telegram",
        family=FAMILY_MESSAGING,
        strategy=WEB_SESSION,
        capabilities=("open", "read_page", "click", "type"),
        site_url="https://web.telegram.org",
        docs_url="https://web.telegram.org",
        safety_note=(
            "This is your own Telegram account in a browser window you can watch, not a bot. "
            "Nothing is sent without you asking for it."
        ),
        aliases=("telegram web", "telegram browser"),
        # A row of its own rather than a `web_url` on `telegram_bot`, because they are
        # genuinely different accounts: a bot cannot read your chats, and this can.
        # Collapsing them would make one button mean two things.
    ),
    "discord_web": AppConnector(
        app_id="discord_web",
        label="Discord (your account)",
        family=FAMILY_MESSAGING,
        strategy=WEB_SESSION,
        capabilities=("open", "read_page", "click", "type"),
        site_url="https://discord.com/app",
        docs_url="https://discord.com/app",
        safety_note=(
            "Your own Discord account in a browser window you can watch, not a bot. "
            "It can read the servers and DMs you can read, so keep it to what you would "
            "read yourself."
        ),
        aliases=("discord web", "discord browser", "discord account"),
        # Same split as Telegram, and for the same reason: `discord_bot` is a bot
        # token that cannot see your DMs, and this is you.
    ),
}


#: Spoken and typed names mapped to app ids, so "open whats app" and a click on the
#: same card reach the same connector. Built once rather than scanned per lookup.
_ALIAS_INDEX: dict[str, str] = {}
for _app_id, _connector in APP_CONNECTORS.items():
    _ALIAS_INDEX[_app_id.replace("_", " ")] = _app_id
    _ALIAS_INDEX[_app_id] = _app_id
    _ALIAS_INDEX[_connector.label.lower()] = _app_id
    for _alias in _connector.aliases:
        _ALIAS_INDEX[_alias.lower()] = _app_id


def resolve_app_id(name: str | None, extra: Iterable[AppConnector] = ()) -> str | None:
    """App id for a typed or spoken name, or None.

    Falls back to a prefix match so "chrome browser" and "notion workspace" land
    somewhere sensible instead of nowhere -- the recogniser adds trailing words
    far more often than it drops leading ones.

    `extra` is the runtime-added set. It is matched *after* the declared index so
    a scanned "Google Chrome" shortcut cannot take over the name from the entry
    that knows how to open a tab.
    """
    cleaned = (name or "").strip().lower()
    if not cleaned:
        return None
    if cleaned in _ALIAS_INDEX:
        return _ALIAS_INDEX[cleaned]
    for alias, app_id in _ALIAS_INDEX.items():
        if cleaned.startswith(alias + " ") or cleaned.endswith(" " + alias):
            return app_id
    for connector in extra:
        names = {connector.app_id.lower(), connector.label.lower()}
        names.update(alias.lower() for alias in connector.aliases)
        if cleaned in names:
            return connector.app_id
    for connector in extra:
        for alias in {connector.label.lower(), *(a.lower() for a in connector.aliases)}:
            if alias and (cleaned.startswith(alias + " ") or cleaned.endswith(" " + alias)):
                return connector.app_id
    return None


def missing_fields(connector: AppConnector, stored: dict[str, Any] | None) -> list[str]:
    """Required field keys that are absent or blank in `stored`.

    Blank counts as absent. A stored empty string is what you get from submitting
    the form without filling it in, and treating that as present is how an
    integration reports connected and then fails on first use.
    """
    values = stored or {}
    return [
        f.key
        for f in connector.fields
        if not f.optional and not str(values.get(f.key) or "").strip()
    ]


def describe_field(connector: AppConnector, key: str) -> dict[str, Any]:
    for f in connector.fields:
        if f.key == key:
            return {"key": f.key, "label": f.label, "where": f.where, "secret": f.secret}
    return {"key": key, "label": key, "where": "", "secret": True}


@dataclass
class ConnectResult:
    """The answer to one click. `outcome` is one of `OUTCOMES`."""

    app_id: str
    outcome: str
    #: One sentence for the user. Says what is missing, not that something failed.
    detail: str = ""
    #: NEEDS_INPUT: the fields still required, described.
    required: list[dict[str, Any]] = field(default_factory=list)
    #: NEEDS_AUTHORIZATION: where the client should send the user.
    authorize_url: str = ""
    #: CONNECTED: what it connected to -- an account, a path, a URL.
    connected_to: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "app_id": self.app_id,
            "outcome": self.outcome,
            "connected": self.outcome == CONNECTED,
            "detail": self.detail,
        }
        if self.required:
            payload["required"] = self.required
        if self.authorize_url:
            payload["authorize_url"] = self.authorize_url
        if self.connected_to:
            payload["connected_to"] = self.connected_to
        return payload


#: Injected probes. `resolve_executable(launch_key) -> path or ""` and
#: `http_ok(url) -> bool`. Injected rather than imported so the dispatch logic can
#: be tested without depending on what this machine happens to have installed.
ExecutableProbe = Callable[[str], str]
HttpProbe = Callable[[str], bool]


def _no_executable(_launch_key: str) -> str:
    return ""


def _no_http(_url: str) -> bool:
    return False


#: `path_exists(path) -> bool` for a scanned app's own launch target, and
#: `session_ok(app_id) -> bool` for whether Akansha's browser profile holds a
#: signed-in session for a site. Injected for the same reason the other two are:
#: a test that asserts "Gmail is connected" by looking at this machine's real
#: browser profile is a report about this machine, not a test of this code.
PathProbe = Callable[[str], bool]
SessionProbe = Callable[[str], bool]


def _no_path(_path: str) -> bool:
    return False


def _no_session(_app_id: str) -> bool:
    return False


def plan_connect(
    connector: AppConnector,
    stored: dict[str, Any] | None,
    *,
    resolve_executable: ExecutableProbe = _no_executable,
    http_ok: HttpProbe = _no_http,
    path_exists: PathProbe = _no_path,
    session_ok: SessionProbe = _no_session,
    has_token: bool = False,
    account_label: str = "",
) -> ConnectResult:
    """What happens when the user clicks Connect on `connector`.

    Every branch returns a `ConnectResult` -- there is no path that returns a bare
    boolean, because "false" is the answer that made the old integrations surface
    unusable. The caller needs to know *which* of the five ways it is not
    connected in order to render anything useful.
    """
    stored = stored or {}
    app_id = connector.app_id

    if connector.strategy == LOCAL_EXECUTABLE:
        # A scanned app is judged on the path it was found at. Going through the
        # name resolver instead would ask `APP_LAUNCH_COMMANDS` about a name it has
        # never heard of, and every scanned app would read as not installed.
        if connector.exe_path:
            if path_exists(connector.exe_path):
                return ConnectResult(
                    app_id, CONNECTED, "Found on this machine.", connected_to=connector.exe_path
                )
            return ConnectResult(
                app_id,
                NOT_INSTALLED,
                f"{connector.label} was here when it was added and is not now: "
                f"{connector.exe_path} no longer exists. Re-scan, or remove it.",
            )
        path = resolve_executable(connector.launch_key or app_id)
        if path:
            return ConnectResult(app_id, CONNECTED, "Found on this machine.", connected_to=path)
        return ConnectResult(
            app_id,
            NOT_INSTALLED,
            f"{connector.label} was not found on this machine. Installing it is the only fix -- "
            "there is no credential that would help.",
        )

    if connector.strategy == WEB_SESSION:
        if session_ok(app_id):
            return ConnectResult(
                app_id,
                CONNECTED,
                "Signed in, in the browser profile Akansha drives.",
                connected_to=connector.site_url,
            )
        return ConnectResult(
            app_id,
            NEEDS_SIGN_IN,
            f"Opens {connector.site_url} in Akansha's own browser profile for you to sign in "
            "once. Your password goes to the site, never through this app.",
            connected_to=connector.site_url,
        )

    absent = missing_fields(connector, stored)

    if absent and connector.web_url and not has_token:
        # The one-click route, and the reason this branch sits *before* every
        # credential branch: the developer-app path is not merely longer, it is the
        # path nobody finishes. Registering an X application to post a tweet is a
        # developer agreement, an app, a project, and a client secret pasted into a
        # form; signing into x.com is one click in a browser window that is already
        # on this machine. So an unconfigured connector with a website asks for the
        # click, not for the secret.
        #
        # `required` is still carried, because the API route is better where someone
        # has already done that work -- it needs no window and no visible browser --
        # so the UI keeps offering it as the secondary option rather than hiding it.
        # `not has_token` above: a real OAuth token, once granted, beats a cookie.
        host = connector.web_url.split("://", 1)[-1].rstrip("/")
        if session_ok(app_id):
            return ConnectResult(
                app_id,
                CONNECTED,
                f"Signed in to {host} in the browser window Akansha drives. "
                "No developer app, no client secret.",
                connected_to=connector.web_url,
            )
        return ConnectResult(
            app_id,
            NEEDS_SIGN_IN,
            f"One click opens {host} in Akansha's own browser window and you sign in there as "
            "yourself. Your password goes to the site, never through this app.",
            connected_to=connector.web_url,
            required=[describe_field(connector, key) for key in absent],
        )

    if connector.strategy == API_KEY:
        if absent:
            return ConnectResult(
                app_id,
                NEEDS_INPUT,
                f"{connector.label} needs {len(absent)} more value(s) before it can be connected.",
                required=[describe_field(connector, key) for key in absent],
            )
        return ConnectResult(
            app_id,
            CONNECTED,
            "Credentials are stored for this app.",
            connected_to=account_label or connector.label,
        )

    if connector.strategy == OAUTH2:
        if has_token:
            return ConnectResult(
                app_id,
                CONNECTED,
                "Authorized.",
                connected_to=account_label or connector.label,
            )
        if absent:
            # The app registration has to exist before consent can be asked for.
            # This is the step people skip, so it is named rather than folded into
            # a generic "not connected".
            return ConnectResult(
                app_id,
                NEEDS_INPUT,
                f"Register an app with {connector.label} first, then paste its credentials here.",
                required=[describe_field(connector, key) for key in absent],
            )
        return ConnectResult(
            app_id,
            NEEDS_AUTHORIZATION,
            f"Open {connector.label} to approve access.",
            authorize_url=f"/api/social/oauth/start/{connector.oauth_provider or app_id}",
        )

    if connector.strategy == LOCAL_BRIDGE:
        if absent:
            return ConnectResult(
                app_id,
                NEEDS_INPUT,
                f"{connector.label} needs the address of the server you are running.",
                required=[describe_field(connector, key) for key in absent],
            )
        base = str(stored.get(connector.bridge_url_key) or "").rstrip("/")
        health = base + connector.bridge_health_path
        if http_ok(health):
            return ConnectResult(app_id, CONNECTED, "The server answered.", connected_to=base)
        return ConnectResult(
            app_id,
            UNREACHABLE,
            f"Nothing answered at {health}. The address is saved, so this is almost always "
            "the server not running rather than a wrong value.",
        )

    # Unreachable unless someone adds a strategy constant without a branch. Say so
    # rather than returning a misleading DISCONNECTED.
    return ConnectResult(
        app_id,
        DISCONNECTED,
        f"No connect strategy is implemented for '{connector.strategy}'.",
    )


def effective_capabilities(connector: AppConnector) -> tuple[str, ...]:
    """What this connector can actually be asked to do, route verbs included.

    A connector with a `web_url` gains the browser verbs whether or not its
    declared strategy is `web_session`, because once a person is signed in at
    x.com in Akansha's window, reading and clicking that page is exactly as
    possible as it is for a connector that was declared as a website from the
    start. Without this, `/api/apps/control` would reject `read_page` on the very
    connectors the one-click login was added for.

    A `local_executable` gains the desktop verbs for the same reason, and fixing a
    real defect: the declared tuples on those entries were written for the
    *automation engine* (`open_url`, `new_tab`, `open_chat`), not for
    `desktop_action`, so not one of the twelve desktop connectors declared `close`
    and only three declared `focus`. The connections page offers all four buttons
    on every one of them, which meant "Bring to front" on Chrome and "Close" on
    anything were buttons whose only possible outcome was a 400. What a window can
    be asked to do is a property of the route, not of the app, so the route
    contributes it.

    Declared capabilities come first and duplicates are dropped, so the order the
    catalog shows stays the order the connector chose.
    """
    route_verbs: tuple[str, ...] = ()
    if connector.web_url or connector.strategy == WEB_SESSION:
        route_verbs = WEB_VERBS
    elif connector.strategy == LOCAL_EXECUTABLE:
        route_verbs = DESKTOP_VERBS
    if not route_verbs:
        return tuple(connector.capabilities)
    merged = list(connector.capabilities)
    for verb in route_verbs:
        if verb not in merged:
            merged.append(verb)
    return tuple(merged)


def describe_connector(connector: AppConnector) -> dict[str, Any]:
    """The declarative half of one entry, without probing anything.

    Shared by `catalog()` and by the endpoints that add or list a single adopted
    app: two spellings of "what this app is" would drift, and the frontend reads
    both shapes with one renderer.
    """
    return {
        "app_id": connector.app_id,
        "label": connector.label,
        "family": connector.family,
        "strategy": connector.strategy,
        "capabilities": list(effective_capabilities(connector)),
        "docs_url": connector.docs_url,
        "safety_note": connector.safety_note,
        "adopted": connector.adopted,
        "site_url": connector.site_url,
        "web_url": connector.web_url,
        "fields": [
            {
                "key": f.key,
                "label": f.label,
                "where": f.where,
                "secret": f.secret,
                "optional": f.optional,
            }
            for f in connector.fields
        ],
    }


def catalog(
    *,
    stored_for: Callable[[str], dict[str, Any]] | None = None,
    resolve_executable: ExecutableProbe = _no_executable,
    http_ok: HttpProbe = _no_http,
    path_exists: PathProbe = _no_path,
    session_ok: SessionProbe = _no_session,
    token_for: Callable[[str], bool] | None = None,
    account_for: Callable[[str], str] | None = None,
    extra: Iterable[AppConnector] = (),
    probe: bool = True,
) -> dict[str, Any]:
    """Every app, its strategy, and -- when `probe` -- its status right now.

    `probe=False` returns the declarative half only. That is what the voice path
    wants: answering "what can you connect to" should not fire a dozen HTTP
    requests, and the answer does not change between restarts.

    `extra` carries what the user added at runtime -- apps found by scanning this
    machine, and sites. They go through the identical `plan_connect` dispatch as
    the declared entries rather than a parallel path, because a second way to be
    connected is a second way to be wrong about it.
    """
    connectors = dict(APP_CONNECTORS)
    for connector in extra:
        # Declared entries win. A scan that finds Chrome must not shadow the
        # hand-written Chrome entry and lose its four browser capabilities.
        connectors.setdefault(connector.app_id, connector)

    apps: list[dict[str, Any]] = []
    for app_id, connector in connectors.items():
        entry: dict[str, Any] = describe_connector(connector)
        if probe:
            stored = stored_for(app_id) if stored_for else {}
            entry["status"] = plan_connect(
                connector,
                stored,
                resolve_executable=resolve_executable,
                http_ok=http_ok,
                path_exists=path_exists,
                session_ok=session_ok,
                has_token=bool(token_for(app_id)) if token_for else False,
                account_label=account_for(app_id) if account_for else "",
            ).as_dict()
        apps.append(entry)

    apps.sort(key=lambda item: (item["family"], item["label"]))
    return {
        "version": 2,
        "strategies": list(STRATEGIES),
        "outcomes": list(OUTCOMES),
        "families": sorted({entry["family"] for entry in apps}),
        "apps": apps,
    }
