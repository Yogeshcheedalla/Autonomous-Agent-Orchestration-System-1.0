"""Slot-filling and confirmation for spoken actions that leave the machine.

Three behaviours, all of them answers to the same problem: **voice has no undo.**
A typed message can be re-read before you press send. A spoken one is gone the
moment it is understood, and it was understood by a recogniser that guesses. So
before anything is sent on the user's behalf, this module insists on knowing
*where* it is going, *to whom*, and on hearing a yes.

  1. **Ask which platform.** "Send Ravi a message" names no channel. Picking one
     silently means the message can land somewhere the user never intended, and
     they find out from the other person.

  2. **Confirm before sending.** She reads the resolved message back and waits.
     One extra turn, and the only point in the flow where a mistranscription is
     still recoverable.

  3. **Spell it out.** Recognisers mangle names — they are exactly the words with
     no language model behind them. When a recipient comes back as something that
     is not plausibly a name, she asks for it letter by letter, and understands
     the answer ("R-A-V-I", "r a v i") as a name rather than as six words.

Nothing here sends anything. On confirmation it returns a resolved instruction
for the existing automation path, which is the part of the system that actually
drives an app — so the gain is that the planner no longer has to guess at slots
the user was never asked about.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

#: How long a half-finished send stays open. Past this, the next thing said is a
#: new request rather than an answer -- otherwise an abandoned dialog swallows an
#: unrelated utterance minutes later, and "yes" means something the user has
#: forgotten agreeing to.
PENDING_TTL_SECONDS = 180

#: Channels she can be asked to send on, and what people actually call them out
#: loud. The aliases include mistranscriptions that really happen ("whats app"
#: split in two, "telegraph" for telegram); without them the question loops
#: forever while the user repeats a word she has decided she cannot hear.
SEND_PLATFORMS: Dict[str, Tuple[str, ...]] = {
    "whatsapp": ("whatsapp", "whats app", "what's app", "watsapp", "whatsup", "whats up"),
    "telegram": ("telegram", "telegraph", "tele gram"),
    "email": ("email", "e mail", "mail", "gmail", "outlook"),
    "sms": ("sms", "text message", "text msg", "messages app", "regular text"),
    "slack": ("slack",),
    "instagram": ("instagram", "insta"),
    "linkedin": ("linkedin", "linked in"),
    "discord": ("discord",),
}

#: Human-readable, in the order she offers them. Only the four people actually
#: use for one-to-one messages; listing all eight out loud is a menu, not a
#: question.
_OFFERED_PLATFORMS = ("WhatsApp", "Telegram", "email", "SMS")

_PLATFORM_LABELS = {
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "email": "email",
    "sms": "SMS",
    "slack": "Slack",
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "discord": "Discord",
}


def platform_label(platform: Optional[str]) -> str:
    """Display name for a canonical platform key."""
    if not platform:
        return ""
    return _PLATFORM_LABELS.get(platform, platform.title())


# ─────────────────────────────────────────────────────────────────────────────
#  Recognising a send request
# ─────────────────────────────────────────────────────────────────────────────

_SEND_VERB = re.compile(
    r"\b(send|sent|message|msg|text|mail|email|ping|forward|whatsapp)\b",
    re.IGNORECASE,
)

#: The send word has to be the *head* of the instruction, not merely present.
#: "search for the best email marketing tools" contains `email`; treating that as
#: a request to send an email is worse than missing a real one, because it
#: hijacks the turn with a question about a platform the user never mentioned.
_SEND_HEAD = re.compile(
    r"^(?:send|sent|message|msg|text|mail|email|ping|forward|whatsapp)\b",
    re.IGNORECASE,
)

#: Framing that comes before the instruction and carries no meaning of its own.
#: Stripped so "can you send ravi a message" is recognised as the same request as
#: "send ravi a message" -- and so the question test below sees the instruction
#: rather than the politeness.
_POLITE_OPENER = re.compile(
    r"^\s*(?:(?:hey|hi|ok|okay|so|umm?|akansha|jarvis)[\s,]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?",
    re.IGNORECASE,
)

#: Questions that merely mention messaging are not requests to send one.
#: "what did ravi text me", "did you send it" -- both match `_SEND_VERB`.
_QUESTION_OPENER = re.compile(
    r"^\s*(what|who|when|where|why|how|which|did|do|does|is|are|was|were|can|could|"
    r"should|would|has|have|had|any)\b",
    re.IGNORECASE,
)

#: Where the message body starts, if it was given in the same breath.
_MESSAGE_MARKER = re.compile(
    r"\b(saying|that\s+says|which\s+says|telling\s+(?:him|her|them)|"
    r"and\s+say|to\s+say|message\s*:|:)\s*",
    re.IGNORECASE,
)

_QUOTED = re.compile(r"[\"“']([^\"”']{2,})[\"”']")

#: A bare `that` also introduces a message body -- "text ravi that I'm late" --
#: but only once a recipient has been named. Kept out of `_MESSAGE_MARKER` because
#: that pattern takes the *earliest* match, and in "forward that to ravi" the
#: earliest `that` is the thing being forwarded, so the body would come back as
#: "to ravi". Applied second, and only with two words in front of it.
_LOOSE_MESSAGE_MARKER = re.compile(r"\bthat\s+(?=\S)", re.IGNORECASE)

#: The send word itself, wherever it sits. Removed once when the recipient
#: follows the verb directly ("message ravi", "whatsapp ravi").
_SEND_WORD = re.compile(
    r"\b(?:send|sent|message|msg|text|mail|email|ping|forward|whatsapp)\b",
    re.IGNORECASE,
)

#: Words that survive alias-stripping and are never part of a name. `that`,
#: `saying` and friends are here as well as in the body markers: they bound the
#: name even when the body was extracted some other way.
_RECIPIENT_STOPWORDS = {
    "a", "an", "the", "my", "his", "her", "their", "our",
    "message", "msg", "text", "mail", "note", "quick", "please",
    "on", "via", "through", "using", "in", "over",
    "send", "sent", "ping", "forward",
    "that", "which", "saying", "about", "regarding", "and", "asking",
}

_CANCEL = re.compile(
    r"\b(cancel|never\s*mind|nevermind|forget\s*it|stop|don'?t\s*send|abort|"
    r"vadhu|vaddu|chalu|rahne\s*do|nahi\s*bhejo)\b",
    re.IGNORECASE,
)

_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|okay|ok|correct|right|confirm|confirmed|do\s*it|"
    r"send\s*it|go\s*ahead|please\s*do|avunu|sare|pampu|haan|haa|bhej\s*do)\b",
    re.IGNORECASE,
)

_NO = re.compile(
    r"\b(no|nope|nah|wrong|not\s*right|incorrect|change\s*it|wait|hold\s*on|"
    r"kaadu|ledu|nahi|nahin)\b",
    re.IGNORECASE,
)

#: Lead-ins people put in front of a spelling. Stripped before tokenising, or
#: "it's R-A-V-I" tokenises as a sentence and the spelling is missed.
_SPELL_LEAD_IN = re.compile(
    r"^\s*(?:it'?s|its|that'?s|thats|the\s+name\s+is|name\s+is|spelled?|"
    r"spelling\s+is|it\s+is|capital)\s+",
    re.IGNORECASE,
)

_SPELL_REQUEST = re.compile(
    r"\b(spell|spelling|letter\s*by\s*letter|one\s*letter\s*at\s*a\s*time)\b",
    re.IGNORECASE,
)


def parse_spelled_letters(text: str) -> Optional[str]:
    """Read "R-A-V-I" or "r a v i" as the single word `Ravi`.

    Returns `None` when the utterance is not a spelling, which is the common
    case -- so callers can try this first and fall through.

    Three or more single letters is the bar. Two would catch real speech ("a
    bit", "I know"); three consecutive single-letter tokens essentially only
    happens when someone is spelling.
    """
    if not text:
        return None
    stripped = _SPELL_LEAD_IN.sub("", text.strip())
    # Hyphens and periods are separators here, not characters: "R-A-V-I" and
    # "R. A. V. I" are the same act.
    cleaned = re.sub(r"[^A-Za-z\s]", " ", stripped)
    tokens = [token for token in cleaned.split() if token]
    if len(tokens) < 3:
        return None
    if not all(len(token) == 1 for token in tokens):
        return None
    return "".join(tokens).capitalize()


def looks_unintelligible(name: Optional[str]) -> bool:
    """Is this too unlikely to be a real name to act on?

    Deliberately narrow. The cost of a false positive is one extra question; the
    cost of a false negative is a message to the wrong person. But asking someone
    to spell a name she heard perfectly well is its own kind of broken, so this
    only fires on things that are not name-shaped at all: too short to say, no
    vowel to carry a syllable, or digits where letters belong.
    """
    if not name:
        return True
    candidate = name.strip()
    if len(candidate) < 3:
        return True
    if any(ch.isdigit() for ch in candidate):
        return True
    letters = [ch for ch in candidate.lower() if ch.isalpha()]
    if not letters:
        return True
    # Non-Latin scripts carry vowels differently -- Telugu and Devanagari names
    # would all fail an ASCII vowel test, so only apply it to ASCII.
    if all(ch.isascii() for ch in letters) and not any(
        ch in "aeiouy" for ch in letters
    ):
        return True
    return False


def detect_platform(text: str) -> Optional[str]:
    """Canonical platform named in this utterance, if any."""
    lowered = f" {(text or '').lower()} "
    for canonical, aliases in SEND_PLATFORMS.items():
        for alias in aliases:
            if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", lowered):
                return canonical
    return None


def _message_split(text: str) -> Optional[int]:
    """Index where the message body starts, or `None` if none was given.

    One function for both extractions, because the recipient is everything
    *before* this point and the body is everything after. Computing the two
    independently is how they end up disagreeing — the body starting at `saying`
    while the name still contains it.
    """
    if not text:
        return None
    quoted = _QUOTED.search(text)
    marker = _MESSAGE_MARKER.search(text)
    # Whichever comes first wins: a quote after `saying` is still the body, and
    # `saying` inside a quote is part of what the user wants said.
    candidates = [m.start() for m in (quoted, marker) if m]
    if candidates:
        return min(candidates)
    # `that` only introduces a body once something has been named before it.
    loose = _LOOSE_MESSAGE_MARKER.search(text)
    if loose and len(text[: loose.start()].split()) >= 2:
        return loose.start()
    return None


def _extract_message_body(text: str) -> Optional[str]:
    """The message itself, when it was given in the same breath as the request."""
    split = _message_split(text)
    if split is None:
        return None
    quoted = _QUOTED.search(text)
    if quoted and quoted.start() == split:
        return quoted.group(1).strip()
    tail = text[split:]
    for pattern in (_MESSAGE_MARKER, _LOOSE_MESSAGE_MARKER):
        opener = pattern.match(tail)
        if opener:
            tail = tail[opener.end():]
            break
    body = tail.strip(" .,")
    # "saying 'running late'" splits on `saying`, so the quotes are still
    # attached. They were punctuation around the message, not part of it, and
    # reading them back aloud is nonsense.
    if len(body) >= 2 and body[0] in "\"“'" and body[-1] in "\"”'":
        body = body[1:-1].strip()
    return body or None


def _extract_recipient(text: str, platform: Optional[str]) -> Optional[str]:
    """Who this is going to, from `to <name>` / `message <name>` phrasing."""
    if not text:
        return None
    split = _message_split(text)
    head = text[:split] if split is not None else text
    # Drop the platform's own name so "message ravi on whatsapp" does not come
    # back with a recipient of "ravi on whatsapp".
    if platform:
        for alias in SEND_PLATFORMS.get(platform, ()):
            head = re.sub(rf"(?<![\w]){re.escape(alias)}(?![\w])", " ", head, flags=re.IGNORECASE)

    # An explicit "to <name>" / "for <name>" is the strongest signal.
    after_to = re.search(r"\b(?:to|for)\s+(.+)$", head, re.IGNORECASE)
    if after_to:
        return _clean_recipient(after_to.group(1))
    # Otherwise the name follows the send word directly -- "message ravi",
    # "send ravi a note", "whatsapp ravi" (where the verb *is* the platform and
    # was already stripped above, leaving the bare name).
    return _clean_recipient(_SEND_WORD.sub(" ", head, count=1))


def _clean_recipient(candidate: str) -> Optional[str]:
    """Trim a captured span down to something name-shaped."""
    words = [w for w in re.split(r"[\s,]+", (candidate or "").strip(" .,!?")) if w]
    # Leading filler ("a quick message to..."). Trailing filler is dropped by the
    # stopword filter below.
    while words and words[0].lower() in _RECIPIENT_STOPWORDS:
        words.pop(0)
    kept: list[str] = []
    for word in words:
        if word.lower() in _RECIPIENT_STOPWORDS:
            break
        kept.append(word)
        if len(kept) == 3:  # first, middle, last -- past that it is a sentence
            break
    if not kept:
        return None
    return " ".join(kept)


# ─────────────────────────────────────────────────────────────────────────────
#  The dialog
# ─────────────────────────────────────────────────────────────────────────────

#: Slots in the order she asks for them. `confirm` is a stage rather than a slot
#: because it has no value -- it is the gate.
_STAGES = ("platform", "recipient", "message", "confirm")


@dataclass
class PendingSend:
    """A send request that is not yet safe to act on."""

    platform: Optional[str] = None
    recipient: Optional[str] = None
    message: Optional[str] = None
    language: str = "english"
    stage: str = "platform"
    #: Repeats of the current question. Bounded so a recogniser that cannot hear
    #: the answer produces a way out instead of an infinite loop.
    attempts: int = 0
    #: Whether the recipient has already been spelled. Asking twice reads as not
    #: listening.
    spelling_requested: bool = False
    asked_at: float = 0.0

    def is_stale(self, now: Optional[float] = None) -> bool:
        reference = time.time() if now is None else now
        return (reference - self.asked_at) > PENDING_TTL_SECONDS

    def resolved_instruction(self) -> str:
        """A fully-slotted instruction for the automation planner.

        Written out in full rather than passed as fields because the planner
        takes a natural-language goal, and every slot in it is now something the
        user said out loud rather than something inferred.
        """
        return (
            f'Send a {platform_label(self.platform)} message to {self.recipient} '
            f'saying "{self.message}"'
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "recipient": self.recipient,
            "message": self.message,
            "stage": self.stage,
        }


@dataclass
class SendDialogTurn:
    """What she says next, and what the caller should do about it."""

    prompt: str
    pending: Optional[PendingSend]
    #: Confirmed. `instruction` is ready to execute.
    ready: bool = False
    cancelled: bool = False
    instruction: str = ""


_PROMPTS: Dict[str, Dict[str, str]] = {
    "english": {
        "platform": "Which app should I send it on — {options}?",
        "platform_retry": "I didn't catch which app. You can say {options}.",
        "recipient": "Who should I send it to?",
        "recipient_retry": "Sorry, who should it go to?",
        "spell": "I want to get the name right — can you spell it for me, letter by letter?",
        "message": "What should the message say?",
        "message_retry": "What would you like it to say?",
        "confirm": 'Sending a {platform} message to {recipient}: "{message}". Should I send it?',
        "confirm_retry": "Say yes to send it, or no to cancel.",
        "cancelled": "Okay, I won't send it.",
        "sending": "Sending it now.",
        "gave_up": "I'm not getting that one. Let's try again from the top when you're ready.",
    },
    "telugu": {
        "platform": "ఏ యాప్‌లో పంపాలి — {options}?",
        "platform_retry": "ఏ యాప్ అని వినిపించలేదు. {options} అని చెప్పండి.",
        "recipient": "ఎవరికి పంపాలి?",
        "recipient_retry": "క్షమించండి, ఎవరికి పంపాలి?",
        "spell": "పేరు సరిగ్గా రాయాలి — ఒక్కో అక్షరం చెప్తారా?",
        "message": "మెసేజ్‌లో ఏమి రాయాలి?",
        "message_retry": "ఏమి రాయమంటారు?",
        "confirm": '{recipient} కి {platform} లో "{message}" అని పంపుతున్నాను. పంపమంటారా?',
        "confirm_retry": "పంపాలంటే అవును, వద్దంటే కాదు అని చెప్పండి.",
        "cancelled": "సరే, పంపను.",
        "sending": "ఇప్పుడే పంపుతున్నాను.",
        "gave_up": "ఇది అర్థం కావట్లేదు. మళ్ళీ మొదటి నుంచి చెప్పండి.",
    },
    "hindi": {
        "platform": "किस ऐप पर भेजूँ — {options}?",
        "platform_retry": "कौन सा ऐप, सुनाई नहीं दिया. {options} बोल सकते हैं.",
        "recipient": "किसे भेजना है?",
        "recipient_retry": "माफ़ कीजिए, किसे भेजना है?",
        "spell": "नाम सही चाहिए — एक-एक अक्षर बोल देंगे?",
        "message": "मैसेज में क्या लिखूँ?",
        "message_retry": "क्या लिखवाना चाहेंगे?",
        "confirm": '{recipient} को {platform} पर "{message}" भेज रही हूँ. भेज दूँ?',
        "confirm_retry": "भेजने के लिए हाँ, रोकने के लिए ना बोलिए.",
        "cancelled": "ठीक है, नहीं भेजती.",
        "sending": "अभी भेज रही हूँ.",
        "gave_up": "यह समझ नहीं आ रहा. फिर से शुरू से बताइए.",
    },
}

#: Repeats of one question before she stops rather than loop.
MAX_ATTEMPTS = 3


def _say(key: str, pending: PendingSend) -> str:
    language = pending.language if pending.language in _PROMPTS else "english"
    template = _PROMPTS[language].get(key) or _PROMPTS["english"][key]
    if language == "telugu":
        options = "వాట్సాప్, టెలిగ్రామ్, ఇమెయిల్ లేదా SMS"
    elif language == "hindi":
        options = "व्हाट्सएप, टेलीग्राम, ईमेल या SMS"
    else:
        options = ", ".join(_OFFERED_PLATFORMS[:-1]) + f" or {_OFFERED_PLATFORMS[-1]}"
    return template.format(
        options=options,
        platform=platform_label(pending.platform),
        recipient=pending.recipient or "",
        message=pending.message or "",
    )


def _next_stage(pending: PendingSend) -> str:
    """First slot still empty, or `confirm` when they are all filled."""
    if not pending.platform:
        return "platform"
    if not pending.recipient:
        return "recipient"
    if not pending.message:
        return "message"
    return "confirm"


def _ask_next(pending: PendingSend, now: Optional[float] = None) -> SendDialogTurn:
    """Move to whichever slot is still missing and ask for it."""
    stage = _next_stage(pending)
    # The name came through as something unsayable. Ask for it letter by letter,
    # once -- and clear it, so the spelling has somewhere to land.
    if (
        stage != "platform"
        and pending.recipient
        and not pending.spelling_requested
        and looks_unintelligible(pending.recipient)
    ):
        pending.spelling_requested = True
        pending.recipient = None
        pending.stage = "recipient"
        pending.attempts = 0
        pending.asked_at = time.time() if now is None else now
        return SendDialogTurn(prompt=_say("spell", pending), pending=pending)

    if stage != pending.stage:
        pending.attempts = 0
    pending.stage = stage
    pending.asked_at = time.time() if now is None else now
    return SendDialogTurn(prompt=_say(stage, pending), pending=pending)


def _retry(pending: PendingSend, now: Optional[float] = None) -> SendDialogTurn:
    """Ask the same question again, or give up if it has been asked enough."""
    pending.attempts += 1
    pending.asked_at = time.time() if now is None else now
    if pending.attempts >= MAX_ATTEMPTS:
        return SendDialogTurn(prompt=_say("gave_up", pending), pending=None, cancelled=True)
    key = f"{pending.stage}_retry"
    if pending.stage == "recipient" and not pending.spelling_requested:
        # Second miss on a name is not a volume problem. Switch tactics rather
        # than repeat the same question louder.
        pending.spelling_requested = True
        key = "spell"
    return SendDialogTurn(prompt=_say(key, pending), pending=pending)


def start_send_dialog(
    transcript: str, language: str = "english", now: Optional[float] = None
) -> Optional[PendingSend]:
    """Recognise a request to send something, with whatever slots were given.

    Returns `None` for anything that is not a send request -- including questions
    that mention messaging, which is most of the false-positive risk here.
    """
    text = (transcript or "").strip()
    if not text:
        return None
    # Cheap reject first: most utterances mention nothing about sending, and this
    # skips the two substitutions below for all of them.
    if not _SEND_VERB.search(text):
        return None
    if text.rstrip().endswith("?"):
        return None
    # Strip the politeness, then test the instruction underneath it. "Can you
    # send Ravi a message" is a request, not a question about her capabilities,
    # and the opener is the only thing that makes it look like one.
    instruction = _POLITE_OPENER.sub("", text, count=1).strip()
    if not instruction or _QUESTION_OPENER.match(instruction):
        return None
    if not _SEND_HEAD.match(instruction):
        return None

    platform = detect_platform(instruction)
    pending = PendingSend(
        platform=platform,
        recipient=_extract_recipient(instruction, platform),
        message=_extract_message_body(instruction),
        language=language if language in _PROMPTS else "english",
        asked_at=time.time() if now is None else now,
    )
    return pending


def advance_send_dialog(
    pending: PendingSend, transcript: str, now: Optional[float] = None
) -> SendDialogTurn:
    """Interpret one answer and either ask the next question or send.

    The answer is read according to the question she asked, not by guessing from
    content: "telegram" means the platform if she asked which app and a recipient
    if she asked who. Interpreting by content instead is how a slot-filler ends
    up putting an app name in the name field.
    """
    text = (transcript or "").strip()
    if not text:
        return SendDialogTurn(prompt=_say(pending.stage, pending), pending=pending)

    if _CANCEL.search(text) and pending.stage != "message":
        # Not during `message`: "stop sending me updates" is a legitimate thing to
        # want to say to someone, and the cancel words are ordinary words.
        return SendDialogTurn(prompt=_say("cancelled", pending), pending=None, cancelled=True)

    if pending.stage == "platform":
        platform = detect_platform(text)
        if not platform:
            return _retry(pending, now)
        pending.platform = platform

    elif pending.stage == "recipient":
        spelled = parse_spelled_letters(text)
        recipient = spelled or _clean_recipient(text)
        if not recipient or (spelled is None and looks_unintelligible(recipient)):
            return _retry(pending, now)
        pending.recipient = recipient
        # A spelled name is what the user said it is. Second-guessing it would
        # ask them to spell what they just spelled.
        if spelled:
            pending.spelling_requested = True

    elif pending.stage == "message":
        pending.message = text.strip(" .,")
        if not pending.message:
            return _retry(pending, now)

    elif pending.stage == "confirm":
        if _YES.search(text):
            return SendDialogTurn(
                prompt=_say("sending", pending),
                pending=None,
                ready=True,
                instruction=pending.resolved_instruction(),
            )
        if _NO.search(text):
            return SendDialogTurn(
                prompt=_say("cancelled", pending), pending=None, cancelled=True
            )
        return _retry(pending, now)

    return _ask_next(pending, now)


def wants_spelling(transcript: str) -> bool:
    """Did the user offer to spell something, unprompted?

    "let me spell it" mid-dialog is cooperation, and treating it as a recipient
    would fill the slot with the offer instead of the name.
    """
    return bool(_SPELL_REQUEST.search(transcript or ""))


def open_send_dialog(
    transcript: str,
    pending: Optional[PendingSend],
    language: str = "english",
    now: Optional[float] = None,
) -> Optional[SendDialogTurn]:
    """One call for the whole behaviour: continue a send, or start one.

    Returns `None` when this utterance has nothing to do with sending, which is
    the overwhelmingly common case — so the caller's fast path is a single `if`.

    Continuing takes precedence over starting. Mid-dialog, "on WhatsApp" is an
    answer to the question she just asked; treated as a fresh request it would
    restart the dialog and lose the slots already filled.
    """
    reference = time.time() if now is None else now

    if pending and not pending.is_stale(reference):
        return advance_send_dialog(pending, transcript, reference)

    started = start_send_dialog(transcript, language, reference)
    if not started:
        return None
    # An abandoned dialog is discarded rather than resumed: if the last thing was
    # three minutes ago, this is a new request that happens to look similar.
    return _ask_next(started, reference)
