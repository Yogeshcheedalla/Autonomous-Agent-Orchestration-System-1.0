"""
app_operator — what a connection is *for*, once the sign-in is done.
====================================================================

The three modules before this one stop one step short of being useful.
`app_discovery` finds the things, `app_connect` decides whether a connection
exists, `app_control` can do one verb to one target. What none of them do is
answer the only question the user actually asks, which is never "is X connected"
but *"do this, using X"*.

Until this module existed, the answer was: it could not. `/api/apps/control` had
exactly one caller in the whole repository -- the buttons on the connections page
-- so a site the user had signed into could be clicked by hand on that page and in
no other way. Saying "post this on X" out loud went down
`build_browser_prompt_plan`, which knows nothing about the registry, and so
launched an application or opened a file. The sign-in bought a green dot and
nothing else.

So this module is the hand-off. Two objects carry it:

  * **`ControlGrant`** is the control information a connection passes onward: the
    route this app is reachable by, the target that route acts on, the verbs it
    will accept, and whether it is live *right now*. Built from a `plan_connect`
    result, so it cannot claim a connection the status endpoints would deny. A
    grant is the whole of what a caller needs -- nothing downstream re-derives
    "is it signed in", which is how two code paths end up disagreeing.

  * **`ControlDecision`** is the reasoning. Which route was chosen and why, which
    verb sequence was chosen and why, what is blocking it if anything, and what it
    would need in order to stop being blocked. `reasoning` is a list of sentences
    because this gets *spoken*: "X is signed in in the browser profile, so I am
    going through the browser rather than launching an app" is the difference
    between an assistant and a black box, and it is also the log line that
    explains a wrong action afterwards.

Three decisions in here are load-bearing, and each one is a rule rather than a
heuristic:

**A URL beats a click.** Where an operation can be expressed as a URL --
searching GitHub, searching Spotify -- the plan navigates instead of typing into a
box. Selectors rot; a query string does not. So `Playbook.url` is templated and
`Playbook.steps` is allowed to be empty.

**A declared selector is a guess with a date on it.** The playbooks below hold
real selectors for real sites, and sites redesign. A step that fails to find its
selector therefore reports *that*, names the playbook as possibly stale, and the
decision carries `read_page` as the fallback so the model can look at the page and
decide again. What it must never do is report success.

**Nothing here performs a side effect.** `decide()` is pure. `execute()` takes the
two runners as arguments. That is not test hygiene for its own sake: the verbs on
the other side move the real mouse and open real browser windows on the user's
machine, so every branch and every message in here is exercised without doing
either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus

from .app_connect import (
    APP_CONNECTORS,
    CONNECTED,
    LOCAL_EXECUTABLE,
    WEB_SESSION,
    AppConnector,
    effective_capabilities,
)
from .app_control import DESKTOP_VERBS, WEB_VERBS

# ── routes ───────────────────────────────────────────────────────────────────
#: Driven through Akansha's own browser profile -- the one the cookie is in.
ROUTE_BROWSER = "browser"
#: Driven through the window manager and the keyboard on this machine.
ROUTE_DESKTOP = "desktop"
#: Connected, but not by anything with verbs. An API key is a credential the model
#: layer uses; there is no window to focus and no page to click.
ROUTE_API = "api"
#: Not reachable at all yet.
ROUTE_NONE = "none"

# ── operations ───────────────────────────────────────────────────────────────
#: The operations an utterance can be understood as. Deliberately small: each one
#: has to have a real plan on at least one route, and an operation with no plan is
#: worse than no operation, because it reports understanding and then fails.
OP_OPEN = "open"
OP_SEND = "send_message"
OP_POST = "post"
OP_SEARCH = "search"
OP_READ = "read"
OP_CLOSE = "close"
OP_UNKNOWN = "unknown"

OPERATIONS = (OP_OPEN, OP_SEND, OP_POST, OP_SEARCH, OP_READ, OP_CLOSE, OP_UNKNOWN)


@dataclass(frozen=True)
class ControlStep:
    """One verb, and why it is in the plan.

    `why` exists so a failure is explainable without re-reading this file: a step
    that fails prints its own reason for existing next to the error.
    """

    verb: str
    selector: str = ""
    text: str = ""
    url: str = ""
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "selector": self.selector,
            "text": self.text,
            "url": self.url,
            "why": self.why,
        }


@dataclass(frozen=True)
class ControlGrant:
    """The control information a signed-in connection hands onward.

    `live` is the whole point of passing this around rather than an `app_id`: it is
    a snapshot of what `plan_connect` said, so a caller cannot accidentally act on
    an app that stopped being connected between the status read and the action.
    """

    app_id: str
    label: str
    route: str
    #: URL for the browser route, launch target for the desktop route.
    target: str
    verbs: tuple[str, ...]
    live: bool
    detail: str = ""
    account: str = ""

    @property
    def host(self) -> str:
        return self.target.split("://", 1)[-1].split("/", 1)[0].lower() if self.target else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "label": self.label,
            "route": self.route,
            "target": self.target,
            "verbs": list(self.verbs),
            "live": self.live,
            "detail": self.detail,
            "account": self.account,
        }


@dataclass(frozen=True)
class Intent:
    """What an utterance was understood to mean. No connection knowledge here."""

    utterance: str
    operation: str
    #: The phrase in the utterance that named the app, kept so the refusal can quote
    #: the user's own words back rather than a normalised id they never typed.
    app_phrase: str = ""
    app_id: str | None = None
    #: The message, the post body, or the search query -- whichever the operation
    #: takes. One field rather than three, because an utterance only ever carries one.
    payload: str = ""
    recipient: str = ""
    url: str = ""
    #: True when the app was inferred from the verb ("tweet") rather than named.
    #: Surfaced so the spoken reply can say "on X" and be corrected in one word.
    assumed_app: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "utterance": self.utterance,
            "operation": self.operation,
            "app_phrase": self.app_phrase,
            "app_id": self.app_id,
            "payload": self.payload,
            "recipient": self.recipient,
            "url": self.url,
            "assumed_app": self.assumed_app,
        }


@dataclass
class ControlDecision:
    """The plan, the reasoning behind it, and what is stopping it if anything."""

    intent: Intent
    grant: ControlGrant | None
    steps: list[ControlStep] = field(default_factory=list)
    #: Why this route and this sequence, in the order the decisions were made.
    #: Spoken, so each entry is a sentence.
    reasoning: list[str] = field(default_factory=list)
    #: Set when the plan cannot run. A blocked decision still carries `reasoning`
    #: and `needs`, because "I can't" is only useful with "because" and "unless".
    blocked: str = ""
    #: What would unblock it, in the user's terms.
    needs: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def runnable(self) -> bool:
        return not self.blocked and bool(self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.as_dict(),
            "grant": self.grant.as_dict() if self.grant else None,
            "steps": [s.as_dict() for s in self.steps],
            "reasoning": list(self.reasoning),
            "blocked": self.blocked,
            "needs": list(self.needs),
            "summary": self.summary,
            "runnable": self.runnable,
        }


# ── the playbooks ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Playbook:
    """How to do one operation on one site.

    `url` may carry `{q}` for a URL-encoded query and `{to}` for a recipient. When
    `steps` is empty the navigation *is* the operation, which is the sturdiest
    version of it -- see the "a URL beats a click" rule at the top of this file.

    `steps` templates may carry `{text}` and `{to}`. `confirm` names the step that
    actually publishes something, if any: it is split out so the caller can plan
    everything up to it and stop, which is what "draft it, don't send it" means.
    """

    operation: str
    url: str = ""
    steps: tuple[ControlStep, ...] = ()
    #: Index into `steps` of the irreversible one, or -1. Sending a message and
    #: posting publicly are the two things in here that cannot be undone by closing
    #: a tab, so they are marked rather than left for the caller to guess.
    commit_step: int = -1
    #: Dated, because these are CSS selectors against sites that redesign. A stale
    #: playbook has to be diagnosable as stale rather than as "the app is broken".
    checked: str = "2026-08"


#: Keyed by host. A host absent from here is not unsupported -- it still gets
#: `open` and `read_page`, which is genuinely all you need for a site whose layout
#: nobody has written down. What it does not get is a fabricated selector.
SITE_PLAYBOOKS: dict[str, tuple[Playbook, ...]] = {
    "x.com": (
        Playbook(
            OP_POST,
            url="https://x.com/compose/post",
            steps=(
                ControlStep("click", '[data-testid="tweetTextarea_0"]',
                            why="Focus the composer; X does not accept typing until it has focus."),
                ControlStep("type", '[data-testid="tweetTextarea_0"]', text="{text}",
                            why="Type the post body."),
                ControlStep("click", '[data-testid="tweetButton"]',
                            why="Publish. This is public and immediate."),
            ),
            commit_step=2,
        ),
        Playbook(OP_SEARCH, url="https://x.com/search?q={q}",
                 steps=(ControlStep("read_page", why="Read the results the search URL returned."),)),
    ),
    "web.whatsapp.com": (
        Playbook(
            OP_SEND,
            url="https://web.whatsapp.com",
            steps=(
                ControlStep("type", 'div[contenteditable="true"][data-tab="3"]', text="{to}",
                            why="Search for the contact by name in the chat list."),
                ControlStep("read_page",
                            why="Confirm which chat the search selected before typing into it."),
                ControlStep("type", 'div[contenteditable="true"][data-tab="10"]', text="{text}",
                            why="Type the message into the open chat's composer."),
            ),
            commit_step=-1,
        ),
    ),
    # No send step for WhatsApp, and `commit_step=-1` says so. The verb set has no
    # "press Enter", and adding one would mean a misheard contact name sends a real
    # message to the wrong person -- the failure this codebase already guards
    # elsewhere with `_whatsapp_safety_clarification`. The message is typed into the
    # right chat and the person presses send. That is a deliberate stop, not a gap.
    "web.telegram.org": (
        Playbook(
            OP_SEND,
            url="https://web.telegram.org/a/",
            steps=(
                ControlStep("type", 'input[type="search"]', text="{to}",
                            why="Search the chat list for the contact."),
                ControlStep("read_page", why="Confirm which chat is open before typing."),
                ControlStep("type", "div.input-message-input", text="{text}",
                            why="Type the message into the open chat."),
            ),
        ),
    ),
    "discord.com": (
        Playbook(
            OP_SEND,
            url="https://discord.com/app",
            steps=(
                ControlStep("read_page",
                            why="Discord opens on the last channel; read it so the message is not "
                                "typed into whichever server happened to be selected."),
                ControlStep("type", 'div[role="textbox"]', text="{text}",
                            why="Type into the channel composer."),
            ),
        ),
    ),
    "mail.google.com": (
        Playbook(
            OP_SEND,
            url="https://mail.google.com/mail/u/0/#inbox?compose=new",
            steps=(
                ControlStep("type", 'textarea[name="to"]', text="{to}",
                            why="Address the draft."),
                ControlStep("type", 'div[aria-label="Message Body"]', text="{text}",
                            why="Type the body. The draft is left unsent."),
            ),
        ),
        Playbook(OP_SEARCH, url="https://mail.google.com/mail/u/0/#search/{q}",
                 steps=(ControlStep("read_page", why="Read the matching mail."),)),
    ),
    "github.com": (
        Playbook(OP_SEARCH, url="https://github.com/search?q={q}",
                 steps=(ControlStep("read_page", why="Read the search results."),)),
    ),
    "open.spotify.com": (
        Playbook(OP_SEARCH, url="https://open.spotify.com/search/{q}",
                 steps=(ControlStep("read_page", why="Read what the search found."),)),
    ),
    "app.slack.com": (
        Playbook(
            OP_SEND,
            url="https://app.slack.com/client",
            steps=(
                ControlStep("read_page", why="Confirm which channel is open."),
                ControlStep("type", 'div[data-qa="message_input"] div[contenteditable="true"]',
                            text="{text}", why="Type into the message box, unsent."),
            ),
        ),
    ),
    "www.instagram.com": (
        Playbook(OP_SEARCH, url="https://www.instagram.com/explore/search/keyword/?q={q}",
                 steps=(ControlStep("read_page", why="Read the search results."),)),
    ),
    "www.notion.so": (
        Playbook(OP_SEARCH, url="https://www.notion.so/search?q={q}",
                 steps=(ControlStep("read_page", why="Read what the workspace search found."),)),
    ),
}


def playbook_for(host: str, operation: str) -> Playbook | None:
    for book in SITE_PLAYBOOKS.get(host, ()):
        if book.operation == operation:
            return book
    return None


# ── understanding the sentence ───────────────────────────────────────────────
#: Operation cues, most specific first. Order matters: "send a message on X" is a
#: post, not a DM, and "search for X on GitHub" is a search even though it contains
#: "for". Each entry is (operation, pattern).
_OPERATION_CUES: tuple[tuple[str, str], ...] = (
    (OP_POST, r"\b(?:post|tweet|publish|share)\b"),
    (OP_SEND, r"\b(?:send|message|text|dm|email|mail|reply|write|type)\b"),
    (OP_SEARCH, r"\b(?:search|look\s+up|find|look\s+for|play)\b"),
    (OP_READ, r"\b(?:read|check|what(?:'s| is| does)|show\s+me|any\s+new|unread)\b"),
    (OP_CLOSE, r"\b(?:close|quit|exit)\b"),
    (OP_OPEN, r"\b(?:open|launch|start|go\s+to|bring\s+up|switch\s+to)\b"),
)

#: The payload, in the shapes people actually say it. Quoted first, because a
#: quoted string is the only unambiguous one.
_PAYLOAD_PATTERNS = (
    r"[\"“']([^\"”']{2,})[\"”']",
    r"\b(?:saying|that\s+says|which\s+says)\s+(.+)$",
    r"\b(?:message|post|tweet|text)\s*:\s*(.+)$",
    r"\b(?:search(?:\s+for)?|look\s+up|look\s+for|find|play)\s+(.+)$",
)

#: "to Amma", "to the team channel". Stopped before the clause that carries the
#: message as well as before the "on <app>" clause -- otherwise "send hi to Amma
#: saying I'm late" makes the recipient "Amma saying I'm".
_RECIPIENT_PATTERN = (
    r"\bto\s+([A-Za-z0-9 ._@+-]{2,40}?)"
    r"(?=\s+(?:on|in|via|using|with|through|saying|that\s+says|which\s+says|about)\b|[,.;:]|$)"
)

_URL_PATTERN = r"\bhttps?://[^\s\"'<>]+"


def _strip_app_clause(text: str, app_phrase: str) -> str:
    """Remove the "on WhatsApp" / "using Chrome" clause naming the app.

    The clause is how the user selects the route, and leaving it in the payload is
    how a tweet ends up reading "hello everyone on X".
    """
    if not app_phrase:
        return text.strip()
    pattern = rf"\s*\b(?:on|into|in|via|using|with|through|from)\s+{re.escape(app_phrase)}\b"
    return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()


def _names_of(connector: AppConnector) -> set[str]:
    """Every string in a sentence that should be read as naming this connector.

    The label has to be *split*, not used whole. "X / Twitter" never appears in a
    sentence; "X" and "Twitter" both do, and until the label was split neither
    matched anything — the most-used connector in the registry was unreachable by
    voice. Same for "Google (Gmail, Calendar, Drive)": "read my gmail" names Google,
    and the only place that fact is written down is inside those brackets.
    """
    parts: set[str] = {connector.app_id.replace("_", " ").lower()}
    parts.update(a.lower() for a in connector.aliases)
    for piece in re.split(r"[/(),]", connector.label.lower()):
        parts.add(piece.strip())
    return {p for p in parts if p and p not in _GENERIC_LABEL_PARTS}


#: Label fragments that name a *kind* of connection rather than an app. "Discord
#: (your account)" and "Discord (bot)" would otherwise both offer "bot" and "your
#: account" as things a sentence could be about.
_GENERIC_LABEL_PARTS = frozenset(
    {"your account", "account", "bot", "app", "desktop", "web", "browser",
     "workspace", "personal", "cloud", "beta", "self hosted", "bridge"}
)

#: Verbs that *are* the name of a service. "Tweet the release notes" names no app
#: and everyone knows which one it means. Recorded as an assumption on the intent
#: rather than silently, so the reply can say which app it picked.
_IMPLIED_APP: tuple[tuple[str, str], ...] = (
    (r"\b(?:tweet|retweet)\b", "twitter"),
    (r"\b(?:e-?mail|gmail)\b", "google"),
)


def _prefers_browser(connector: AppConnector) -> bool:
    return connector.strategy == WEB_SESSION or bool(connector.web_url)


def find_app(
    utterance: str, extra: Iterable[AppConnector] = ()
) -> tuple[str | None, str]:
    """Which app this sentence names, and the phrase that named it.

    Not `resolve_app_id`: that one answers "is this string the name of an app",
    which is the right question for a form field and the wrong one for a sentence.
    "send amma a whatsapp message" contains the name in the middle, where a
    prefix/suffix match cannot see it.

    Longest name wins, so "telegram web" is not read as "telegram". Among equally
    long matches the browser route wins, because the whole point of the sign-in work
    is that "message X on Telegram" means the user's own account, not a bot token.
    """
    lowered = (utterance or "").lower()
    if not lowered:
        return None, ""
    best: tuple[int, bool, str, str] | None = None  # (len, browser, name, app_id)
    for connector in (*APP_CONNECTORS.values(), *extra):
        for name in _names_of(connector):
            if not re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered):
                continue
            key = (len(name), _prefers_browser(connector), name, connector.app_id)
            if best is None or key[:2] > best[:2]:
                best = key
    if best is None:
        return None, ""
    return best[3], best[2]


def understand(utterance: str, extra: Iterable[AppConnector] = ()) -> Intent:
    """Read one sentence as an operation against one app.

    Regexes, not a model call, and that is a deliberate limit rather than a
    shortcut: this runs on the voice path before anything is spoken, so it has to
    answer in microseconds, and it has to answer the same way twice. When it comes
    back `OP_UNKNOWN` or with no `app_id`, the caller falls through to the older
    plan builder -- nothing is lost by this being narrow, and a model that
    hallucinated an app id here would act on the wrong account.
    """
    text = (utterance or "").strip()
    app_id, phrase = find_app(text, extra)
    lowered = text.lower()
    assumed = False
    if app_id is None:
        for pattern, implied in _IMPLIED_APP:
            hit = re.search(pattern, lowered)
            if hit and implied in APP_CONNECTORS:
                app_id, phrase, assumed = implied, hit.group(0), True
                break

    operation = OP_UNKNOWN
    for candidate, pattern in _OPERATION_CUES:
        if re.search(pattern, lowered):
            operation = candidate
            break
    # "post" and "send" both appear in "send a post to X". The cue order settles
    # that, but a bare "search ... on x" must not be read as a send just because it
    # contains "for". Searching is checked before reading for the same reason.

    url_match = re.search(_URL_PATTERN, text)
    url = url_match.group(0) if url_match else ""

    recipient = ""
    if operation == OP_SEND:
        rec = re.search(_RECIPIENT_PATTERN, text, flags=re.IGNORECASE)
        if rec:
            candidate = rec.group(1).strip()
            # "to the team" is a recipient; "to say hello" is not.
            if not re.match(r"^(?:say|tell|the\s+effect)\b", candidate, flags=re.IGNORECASE):
                recipient = candidate

    payload = ""
    for pattern in _PAYLOAD_PATTERNS:
        found = re.search(pattern, text, flags=re.IGNORECASE)
        if found:
            payload = found.group(1).strip(" .")
            break
    if not payload and operation in (OP_POST, OP_SEND):
        # Last resort: everything after the operation word, minus the recipient and
        # app clauses. Worse than a quoted string, better than sending nothing.
        tail = re.split(
            r"\b(?:post|tweet|publish|share|send|message|text|dm|email|mail|write|type)\b",
            text, maxsplit=1, flags=re.IGNORECASE,
        )
        if len(tail) > 1:
            payload = tail[1].strip(" .:,")
    payload = _strip_app_clause(payload, phrase)
    if recipient:
        payload = re.sub(
            rf"^\s*(?:to\s+)?{re.escape(recipient)}\b[\s,:-]*", "", payload, flags=re.IGNORECASE
        ).strip()
        payload = re.sub(
            rf"\s*\bto\s+{re.escape(recipient)}\b", "", payload, flags=re.IGNORECASE
        ).strip()
    payload = re.sub(r"^(?:a|an|the)\s+(?:message|post|tweet|email|mail)\s*", "", payload,
                     flags=re.IGNORECASE).strip()
    payload = re.sub(r"^(?:saying|that\s+says)\s+", "", payload, flags=re.IGNORECASE).strip()
    # "tweet that the build is green" -> "the build is green". Only a leading "that",
    # and only when something follows it, so a message that genuinely starts with the
    # word survives as long as it is quoted.
    payload = re.sub(r"^that\s+(?=\S)", "", payload, flags=re.IGNORECASE).strip()

    return Intent(
        utterance=text,
        operation=operation,
        app_phrase=phrase,
        app_id=app_id,
        payload=payload,
        recipient=recipient,
        url=url,
        assumed_app=assumed,
    )


# ── the hand-off ─────────────────────────────────────────────────────────────
def grant_for(
    connector: AppConnector,
    state: dict[str, Any],
    *,
    launch_target: str = "",
) -> ControlGrant:
    """Turn a `plan_connect` result into the control information it implies.

    `state` is a `ConnectResult.as_dict()` and nothing else, which is what stops
    this from becoming a second opinion about whether an app is connected. The
    route is decided here rather than at the call site because it is the same rule
    `/api/apps/control` applies, and two copies of it would eventually disagree
    about a `web_url` connector.
    """
    outcome = str(state.get("outcome") or "")
    live = outcome == CONNECTED
    detail = str(state.get("detail") or "")
    account = str(state.get("connected_to") or "")
    site = connector.site_url or connector.web_url

    if connector.strategy == WEB_SESSION or connector.web_url:
        verbs = tuple(v for v in effective_capabilities(connector) if v in WEB_VERBS)
        return ControlGrant(connector.app_id, connector.label, ROUTE_BROWSER, site,
                            verbs, live, detail, account)
    if connector.strategy == LOCAL_EXECUTABLE:
        verbs = tuple(v for v in effective_capabilities(connector) if v in DESKTOP_VERBS)
        return ControlGrant(connector.app_id, connector.label, ROUTE_DESKTOP,
                            launch_target or connector.exe_path, verbs, live, detail, account)
    # An API key or a bridge. Connected, possibly usefully so -- but by the model
    # layer, not by a verb. Saying ROUTE_API rather than ROUTE_NONE matters: the
    # refusal is "there is nothing to click here", not "it is not connected".
    return ControlGrant(connector.app_id, connector.label, ROUTE_API, "",
                        tuple(connector.capabilities), live, detail, account)


# ── the reasoning ────────────────────────────────────────────────────────────
def _fill(template: str, *, text: str, to: str) -> str:
    return (
        template.replace("{text}", text)
        .replace("{to}", to)
        .replace("{q}", quote_plus(text or to))
    )


def _browser_plan(intent: Intent, grant: ControlGrant, decision: ControlDecision) -> None:
    """The browser route. Mutates `decision` -- it is the accumulator for reasoning."""
    host = grant.host
    book = playbook_for(host, intent.operation)
    decision.reasoning.append(
        f"{grant.label} is reachable in the browser profile Akansha drives, at {host}."
    )

    if intent.operation == OP_OPEN:
        decision.steps = [ControlStep("open", url=intent.url or grant.target,
                                      why=f"Open {host} in the window that is signed in.")]
        decision.summary = f"Opening {host}."
        decision.reasoning.append("Nothing beyond opening it was asked for.")
        return

    if intent.operation in (OP_READ, OP_UNKNOWN):
        decision.steps = [
            ControlStep("open", url=intent.url or grant.target, why=f"Open {host}."),
            ControlStep("read_page", why="Read the page text back so it can be summarised."),
        ]
        decision.summary = f"Reading {host}."
        if intent.operation == OP_UNKNOWN:
            # Reading is the safe interpretation of an unclear sentence. It has no
            # side effect, and the text it returns is what lets the model work out
            # what was actually wanted -- much better than guessing a verb that types.
            decision.reasoning.append(
                "I could not tell which operation you meant, so I opened it and read it "
                "rather than guessing at something that types or clicks."
            )
        return

    if intent.operation == OP_CLOSE:
        decision.blocked = (
            f"{grant.label} is a browser session, and closing it is not something I do from here "
            "-- closing the window would end every other site signed in to the same profile."
        )
        decision.needs = ["close the browser window yourself, or ask me to open something else"]
        decision.reasoning.append("Refused rather than closing a shared profile window.")
        return

    if book is None:
        # The honest fallback, and the reason it is not a failure: opening the site
        # and reading it is a real, useful capability, and the model can decide what
        # to click next from the text it gets back. Inventing a selector for a site
        # nobody wrote down would fail *after* opening a window, which is worse.
        decision.steps = [
            ControlStep("open", url=intent.url or grant.target, why=f"Open {host}."),
            ControlStep("read_page", why="No declared way to do this here, so read the page and decide."),
        ]
        decision.summary = f"Opening {host} and reading it -- I have no declared {intent.operation} recipe for that site."
        decision.needs = [f"a {intent.operation} recipe for {host}, or tell me what to click"]
        decision.reasoning.append(
            f"No playbook declares how to {intent.operation} on {host}, so the plan stops at reading it."
        )
        return

    if intent.operation in (OP_POST, OP_SEND) and not intent.payload:
        decision.blocked = f"I did not catch what to {intent.operation.replace('_', ' ')} on {grant.label}."
        decision.needs = ["the text to send"]
        decision.reasoning.append("Refused: an empty body would post nothing, visibly.")
        return
    if intent.operation == OP_SEND and book.steps and any(
        "{to}" in s.text for s in book.steps
    ) and not intent.recipient:
        decision.blocked = f"{grant.label} needs to know who to send that to."
        decision.needs = ["the contact or channel name"]
        decision.reasoning.append(
            "Refused: this playbook searches the chat list, and a blank search would leave "
            "whichever chat was already open selected."
        )
        return

    url = _fill(book.url or grant.target, text=intent.payload, to=intent.recipient)
    steps = [ControlStep("open", url=url, why=f"Go straight to {url.split('://', 1)[-1]}.")]
    if "{q}" in book.url:
        # The condition is the templated query, not an empty step list: Spotify's
        # search still reads the page afterwards, and that read is not a click.
        decision.reasoning.append(
            "The search itself fits in a URL, so nothing is typed into a search box -- "
            "a query string cannot go stale the way a selector can."
        )
    for step in book.steps:
        steps.append(
            ControlStep(
                step.verb,
                selector=step.selector,
                text=_fill(step.text, text=intent.payload, to=intent.recipient),
                url="",
                why=step.why,
            )
        )
    decision.steps = steps

    unsupported = [s.verb for s in steps if s.verb not in grant.verbs]
    if unsupported:
        # A declared capability list that does not cover its own playbook is a bug in
        # the registry, and it must surface as one rather than as a 400 from the verb
        # layer halfway through a sequence.
        decision.blocked = (
            f"{grant.label} is not declared as able to {', '.join(sorted(set(unsupported)))}."
        )
        decision.needs = [f"add {', '.join(sorted(set(unsupported)))} to {grant.app_id}'s capabilities"]
        decision.steps = []
        return

    if book.commit_step >= 0:
        decision.reasoning.append(
            f"Step {book.commit_step + 2} is the irreversible one: {book.steps[book.commit_step].why}"
        )
    elif intent.operation in (OP_POST, OP_SEND):
        decision.reasoning.append(
            "Nothing in this plan publishes or sends -- it stops with the text in place, "
            "so you press send."
        )
    verb_summary = {OP_POST: "Posting to", OP_SEND: "Composing a message in",
                    OP_SEARCH: "Searching"}.get(intent.operation, "Working in")
    decision.summary = f"{verb_summary} {grant.label} through the signed-in browser session."


#: The field a search goes into, when the window publishes one under this name.
#: Left as a name rather than a coordinate because `desktop_ui` resolves it, asks
#: when two things answer to it, and refuses when nothing does.
DESKTOP_SEARCH_FIELD = "Search"


def _desktop_plan(intent: Intent, grant: ControlGrant, decision: ControlDecision) -> None:
    """The desktop route: six verbs, and no pretending there are more.

    Read and click arrived with `desktop_ui`, and they changed two of the answers
    here. `OP_READ` used to be refused outright -- "it has no way to see the screen
    contents" -- which stopped being true for every window that publishes an
    accessibility tree. It is still true for the ones that do not, so the refusal
    moved: it is now the runner that says a particular window cannot be read, which
    is a statement about that window rather than about the route.
    """
    decision.reasoning.append(
        f"{grant.label} is an application installed on this machine, so this goes through "
        "the window manager, not a browser."
    )
    if intent.operation in (OP_OPEN, OP_UNKNOWN):
        decision.steps = [ControlStep("launch", why=f"Start {grant.label}.")]
        decision.summary = f"Launching {grant.label}."
    elif intent.operation == OP_CLOSE:
        decision.steps = [ControlStep("close", why=f"Close {grant.label}'s window.")]
        decision.summary = f"Closing {grant.label}."
    elif intent.operation in (OP_SEND, OP_POST):
        if not intent.payload:
            decision.blocked = f"I did not catch what to type into {grant.label}."
            decision.needs = ["the text to type"]
            return
        decision.steps = [
            ControlStep("focus", why=f"Bring {grant.label} to the front; typing goes to whatever "
                                     "has focus, so this must succeed first."),
            ControlStep("type_text", text=intent.payload, why="Type the text."),
        ]
        decision.summary = f"Typing into {grant.label}."
        decision.reasoning.append(
            "Aimed at the field that has the cursor, or at the only text field in that window "
            "if none does. Two candidates and no cursor is a question rather than a guess, and "
            "a window that publishes no fields falls back to keystrokes."
        )
    elif intent.operation == OP_READ:
        decision.steps = [
            ControlStep("read_window", why=f"Read what is on screen in {grant.label}.")
        ]
        decision.summary = f"Reading {grant.label}'s window."
        decision.reasoning.append(
            "Read through the accessibility tree, so it is the controls the app publishes -- "
            "not a screenshot, and not everything a person can see."
        )
    elif intent.operation == OP_SEARCH:
        if not intent.payload:
            decision.blocked = f"I did not catch what to search for in {grant.label}."
            decision.needs = ["what to search for"]
            return
        decision.steps = [
            ControlStep("focus", why=f"Bring {grant.label} to the front."),
            ControlStep(
                "type_text",
                selector=DESKTOP_SEARCH_FIELD,
                text=intent.payload,
                why=f'Put the words in {grant.label}\'s "{DESKTOP_SEARCH_FIELD}" field.',
            ),
        ]
        decision.summary = f"Searching in {grant.label}."
        decision.reasoning.append(
            f'Typed into the field called "{DESKTOP_SEARCH_FIELD}" and left there -- nothing here '
            "presses Enter, so you can see what it found before it commits to anything."
        )

    unsupported = [s.verb for s in decision.steps if s.verb not in grant.verbs]
    if unsupported:
        decision.blocked = (
            f"{grant.label} is declared as able to {', '.join(grant.verbs) or 'nothing'} -- "
            f"not {', '.join(sorted(set(unsupported)))}."
        )
        decision.steps = []


def decide(intent: Intent, grant: ControlGrant | None) -> ControlDecision:
    """Choose the route and the verb sequence. Pure -- nothing here acts.

    The order of the guards is the reasoning: *is there an app*, *is it reachable at
    all*, *is it live*, and only then *what would we do*. Reversed, a plan gets built
    and discarded, and the user hears "I'll post that on X" followed by silence.
    """
    decision = ControlDecision(intent=intent, grant=grant)
    if intent.assumed_app and grant is not None:
        decision.reasoning.append(
            f'You said "{intent.app_phrase}", so I took that to mean {grant.label}.'
        )

    if grant is None:
        decision.blocked = (
            f"I don't have a connection called \"{intent.app_phrase}\"."
            if intent.app_phrase
            else "That didn't name an app or site I'm connected to."
        )
        decision.needs = ["connect it on the Connections page first"]
        return decision

    if grant.route == ROUTE_API:
        decision.blocked = (
            f"{grant.label} is connected by an API key, which lets me use it for models -- "
            "there is no window or page to drive."
        )
        decision.reasoning.append(
            f"{grant.label}'s route is an API key, so it has no verbs; refused instead of "
            "launching something unrelated."
        )
        decision.needs = [f"a website route for {grant.label}, if one would help"]
        return decision
    if grant.route == ROUTE_NONE or not grant.verbs:
        decision.blocked = f"{grant.label} has nothing I can do to it yet. {grant.detail}".strip()
        decision.needs = ["connect it first"]
        return decision

    # `open` is allowed cold, exactly as `/api/apps/control` allows it: opening the
    # sign-in page *is* how you stop being disconnected, so refusing it would make
    # the disconnected state unrecoverable by voice.
    if not grant.live and intent.operation not in (OP_OPEN, OP_UNKNOWN):
        decision.blocked = f"{grant.label} isn't connected right now. {grant.detail}".strip()
        decision.needs = (
            [f'sign in once -- say "open {grant.label}" and I will bring up the window']
            if grant.route == ROUTE_BROWSER
            else ["install or launch it first"]
        )
        decision.reasoning.append(
            "Checked the live connection state before planning: "
            f"{(grant.detail or 'not connected').rstrip('.')}."
        )
        return decision

    if grant.route == ROUTE_BROWSER:
        _browser_plan(intent, grant, decision)
    else:
        _desktop_plan(intent, grant, decision)
    return decision


# ── doing it ─────────────────────────────────────────────────────────────────
def execute(
    decision: ControlDecision,
    *,
    run_web: Callable[..., dict[str, Any]],
    run_desktop: Callable[..., dict[str, Any]],
    stop: Callable[[], bool] = lambda: False,
    up_to: int | None = None,
) -> dict[str, Any]:
    """Run a decision's steps, stopping at the first failure.

    Both runners are arguments with no defaults. That is on purpose: importing
    `web_action` and `desktop_action` at the top of this module would make the
    honest unit test one import away from launching Chrome on the user's desktop.
    The two call sites that mean it pass them in.

    `up_to` truncates the plan, which is how "draft it, don't send it" is honoured:
    pass `commit_step` and the irreversible step is never reached.
    """
    grant = decision.grant
    if not decision.runnable or grant is None:
        return {
            "ok": False,
            "blocked": decision.blocked or "Nothing to do.",
            "summary": decision.summary or decision.blocked,
            "steps": [],
            "reasoning": list(decision.reasoning),
            "needs": list(decision.needs),
        }

    steps = decision.steps if up_to is None else decision.steps[:up_to]
    done: list[dict[str, Any]] = []
    current_url = grant.target
    for index, step in enumerate(steps):
        if stop():
            return {
                "ok": False,
                "cancelled": True,
                "summary": f"Stopped after {len(done)} of {len(steps)} step(s).",
                "steps": done,
                "reasoning": list(decision.reasoning),
                "app_id": grant.app_id,
                "route": grant.route,
            }
        if grant.route == ROUTE_BROWSER:
            current_url = step.url or current_url
            result = run_web(
                step.verb, url=current_url, selector=step.selector, text=step.text
            )
        else:
            result = run_desktop(step.verb, text=step.text, target=step.selector)
        record = {"step": step.as_dict(), "result": result, "index": index}
        done.append(record)
        if not result.get("ok"):
            detail = str(result.get("detail") or f"{step.verb} failed.")
            if step.selector and "selector" not in detail.lower():
                # The specific diagnosis. A missing selector is almost always a site
                # redesign rather than a broken connection, and saying so is what
                # stops the user re-authenticating an account that was fine.
                detail += (
                    f" The step was looking for `{step.selector}` on {grant.host}; that page may "
                    "have changed since this recipe was written."
                )
            return {
                "ok": False,
                "summary": detail,
                "failed_step": step.as_dict(),
                "steps": done,
                "reasoning": list(decision.reasoning),
                "app_id": grant.app_id,
                "route": grant.route,
            }

    text = ""
    for record in reversed(done):
        if record["result"].get("text"):
            text = str(record["result"]["text"])
            break
    return {
        "ok": True,
        "summary": decision.summary,
        "steps": done,
        "text": text,
        "reasoning": list(decision.reasoning),
        "app_id": grant.app_id,
        "route": grant.route,
        "truncated": up_to is not None and up_to < len(decision.steps),
    }
