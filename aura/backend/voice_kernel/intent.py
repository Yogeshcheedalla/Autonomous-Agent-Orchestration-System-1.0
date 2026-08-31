"""
voice_kernel.intent — 14-way turn classification (§10, §7, §30, §36).
=====================================================================

Replaces the 4-category keyword scorer in `voice_engine.py`. Two concrete
defects in that implementation are fixed here:

  1.  **Substring matching.** The old classifier did
      `any(cmd in text for cmd in STOP_COMMANDS)`, so "I am *wait*ing for the
      build", "that was *quit*e good" and "find the *pause*d video" were all
      classified CONTROL/stop. Every lexicon here is compiled into a
      word-boundary alternation instead.

  2.  **No backchannel category.** "Yeah" and "mm-hmm" were routed as fresh
      QUESTIONs, so Akansha answered her own progress updates. §7 requires
      these to be recognised and, usually, not answered at all.

The classifier is deliberately lexical + contextual, with no network call, so
one turn costs microseconds. `voice_engine`'s LLM resolver stays available for
entity disambiguation but is no longer on the critical path of *every* turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class IntentCategory(str, Enum):
    INFORMATION = "INFORMATION"
    QUESTION = "QUESTION"
    COMMAND = "COMMAND"
    AUTOMATION = "AUTOMATION"
    FOLLOW_UP = "FOLLOW_UP"
    CORRECTION = "CORRECTION"
    INTERRUPTION = "INTERRUPTION"
    CANCELLATION = "CANCELLATION"
    CONFIRMATION = "CONFIRMATION"
    REJECTION = "REJECTION"
    CLARIFICATION = "CLARIFICATION"
    SOCIAL_CONVERSATION = "SOCIAL_CONVERSATION"
    SILENCE = "SILENCE"
    BACKCHANNEL = "BACKCHANNEL"


def _lex(*phrases: str) -> re.Pattern[str]:
    """Compile phrases into a word-boundary alternation.

    Longest-first so "never mind" wins over "mind", and non-ASCII phrases get
    lookaround boundaries since `\\b` does not behave usefully next to Telugu
    or Devanagari codepoints.
    """
    parts: List[str] = []
    for phrase in sorted(set(phrases), key=len, reverse=True):
        escaped = re.escape(phrase.strip())
        if phrase.isascii():
            parts.append(rf"\b{escaped}\b")
        else:
            parts.append(rf"(?<![\w]){escaped}(?![\w])")
    return re.compile("|".join(parts), re.IGNORECASE)


# ── lexicons ────────────────────────────────────────────────────────────────

# §4/§20: hard interruption. These take priority over everything.
INTERRUPTION = _lex(
    "stop", "stop it", "stop that", "stop now", "shut up", "be quiet", "quiet",
    "enough", "hold on", "hold up", "wait", "wait wait", "hang on",
    "aagu", "aapu", "ఆపు", "ఆగు", "ruko", "ruk jao", "रुको", "bas", "बस",
)

CANCELLATION = _lex(
    "cancel", "cancel that", "abort", "never mind", "nevermind", "forget it",
    "drop it", "don't do it", "do not do it", "call it off", "scrap that",
    "cancel cheyyi", "వద్దు", "cancel karo", "रद्द करो",
)

CONFIRMATION = _lex(
    "yes", "yeah", "yep", "yup", "sure", "go ahead", "do it", "please do",
    "confirm", "confirmed", "correct", "that's right", "thats right",
    "sounds good", "ok do it", "okay do it", "affirmative", "proceed",
    "sare", "సరే", "అవును", "avunu", "haan", "हाँ", "theek hai", "ठीक है",
)

REJECTION = _lex(
    "no", "nope", "nah", "don't", "do not", "negative", "not that",
    "that's wrong", "thats wrong", "incorrect", "wrong one",
    "kaadu", "కాదు", "వద్దు", "nahi", "नहीं", "mat karo",
)

# §7: conversational glue. Recognising these is what stops Akansha from
# treating an encouraging "yeah" as a brand-new task.
BACKCHANNEL = _lex(
    "mm", "mhm", "mm-hmm", "mmhmm", "uh huh", "uh-huh", "hmm", "hm",
    "right", "i see", "got it", "gotcha", "makes sense", "of course",
    "exactly", "true", "indeed", "cool", "nice", "okay", "ok", "alright",
    "fine", "that's fine", "thats fine", "sounds fine", "carry on", "continue",
    "keep going", "go on",
    "ha", "haan ji", "acha", "achha", "अच्छा", "ఆహా", "సరేసరే",
)

CORRECTION = _lex(
    "actually", "i meant", "i mean", "instead", "rather", "not that one",
    "change it to", "make it", "no wait", "scratch that", "correction",
    "kaadu kaadu", "nahi nahi",
)

CLARIFICATION = _lex(
    "what do you mean", "which one", "i don't understand", "i dont understand",
    "come again", "say that again", "repeat that", "pardon", "sorry what",
    "can you clarify", "explain that", "artham kaledu", "samajh nahi aaya",
)

SOCIAL = _lex(
    "hello", "hi", "hey", "good morning", "good evening", "good night",
    "thanks", "thank you", "thankyou", "thx", "bye", "goodbye", "see you",
    "how are you", "what's up", "whats up", "nice work", "well done",
    "namaste", "నమస్కారం", "नमस्ते", "dhanyavaad", "ధన్యవాదాలు",
)

QUESTION_WORDS = _lex(
    "what", "who", "whom", "whose", "when", "where", "why", "how",
    "which", "can you", "could you", "do you", "did you", "is it", "are they",
    "should i", "tell me", "explain", "describe", "define", "show me",
    "enti", "ela", "ekkada", "eppudu", "enduku", "ఏమిటి", "ఎలా", "ఎక్కడ",
    "kya", "kaise", "kahan", "kab", "kyun", "क्या", "कैसे",
)

# §10 COMMAND: single-shot imperative on the local machine or browser chrome.
COMMAND_VERBS = _lex(
    "scroll", "click", "type", "press", "mute", "unmute", "maximize",
    "minimize", "close", "switch", "refresh", "reload", "zoom", "back",
    "forward", "next", "previous", "volume", "brightness", "screenshot",
    "copy", "paste", "select all", "undo", "redo", "pause", "resume", "play",
)

# §10 AUTOMATION: multi-step work, or anything scheduled/recurring.
AUTOMATION_VERBS = _lex(
    "open", "launch", "start", "navigate", "go to", "search", "find", "look up",
    "download", "upload", "install", "update", "deploy", "build", "compile",
    "run", "test", "fix", "debug", "refactor", "commit", "push", "pull",
    "book", "buy", "order", "send", "email", "message", "post", "submit",
    "enroll", "register", "sign in", "log in", "solve", "complete", "create",
    "generate", "write", "delete", "remove", "move", "rename", "organize",
    "teruvu", "తెరువు", "vetuku", "వెతుకు", "cheyyi", "చేయి",
    "kholo", "खोलो", "dhundo", "ढूंढो", "karo", "करो",
)

RECURRENCE = _lex(
    "every day", "everyday", "every morning", "every evening", "every hour",
    "every week", "every monday", "daily", "weekly", "hourly", "nightly",
    "each morning", "each day", "from now on", "whenever", "always",
    "schedule", "remind me", "recurring", "roju", "రోజూ", "har roz",
)

FOLLOW_UP = _lex(
    "also", "and then", "after that", "next", "same for", "do the same",
    "the other one", "that one too", "as well", "too", "again",
    "one more", "another one", "what about", "how about",
)

PAUSE_WORDS = _lex("pause", "pause it", "hold", "freeze", "suspend", "pause cheyyi")
RESUME_WORDS = _lex(
    "resume", "continue", "carry on", "keep going", "go on", "proceed",
    "unpause", "start again", "pick up where", "kondaka", "aage badho",
)

# §36: consequences that must never run on a low-confidence transcript.
DESTRUCTIVE = _lex(
    "delete", "drop", "remove", "wipe", "erase", "purge", "truncate",
    "format", "uninstall", "kill", "terminate", "force push", "reset hard",
    "revoke", "deploy to production", "production database", "rm -rf",
    "shut down", "shutdown", "restart", "reboot", "overwrite", "unsubscribe",
    "transfer", "pay", "send money", "cancel subscription",
)

# Things that only make sense relative to earlier turns (§13).
ANAPHORA = _lex(
    "it", "that", "this", "them", "those", "these", "the same", "again",
    "the other", "the previous", "the last one", "there", "here",
)

# Sites that need real browser automation rather than a chat answer.
AUTOMATION_SITES: Set[str] = {
    "youtube", "codechef", "leetcode", "github", "gitlab", "coursera",
    "linkedin", "hackerrank", "codeforces", "geeksforgeeks", "udemy", "nptel",
    "google", "gmail", "instagram", "twitter", "x.com", "facebook",
    "whatsapp", "telegram", "reddit", "stackoverflow", "notion", "jira",
    "azure", "aws", "vercel", "netlify", "amazon", "flipkart",
}

_WORD_RE = re.compile(r"[\w'ऀ-ॿఀ-౿]+")
_FILLER = _lex(
    "um", "uh", "er", "ah", "like", "you know", "basically", "literally",
    "i guess", "sort of", "kind of",
)


@dataclass(slots=True)
class Intent:
    """One classified user turn."""

    category: IntentCategory
    text: str
    cleaned_text: str

    # §30 — three independent confidences, not one blended number.
    intent_confidence: float = 0.0
    entity_confidence: float = 0.0
    action_confidence: float = 0.0

    entities: Dict[str, Any] = field(default_factory=dict)
    target_site: Optional[str] = None
    action: Optional[str] = None

    is_destructive: bool = False
    is_recurring: bool = False
    references_prior_turn: bool = False
    needs_clarification: bool = False
    needs_confirmation: bool = False

    #: Which lexicons fired, for the debug panel and for test assertions.
    matched: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        return self.category in (
            IntentCategory.INTERRUPTION,
            IntentCategory.CANCELLATION,
        )

    @property
    def is_actionable(self) -> bool:
        return self.category in (
            IntentCategory.COMMAND,
            IntentCategory.AUTOMATION,
            IntentCategory.FOLLOW_UP,
            IntentCategory.CORRECTION,
        )

    @property
    def min_confidence(self) -> float:
        return min(self.intent_confidence, self.entity_confidence, self.action_confidence)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "text": self.text,
            "cleaned_text": self.cleaned_text,
            "intent_confidence": round(self.intent_confidence, 3),
            "entity_confidence": round(self.entity_confidence, 3),
            "action_confidence": round(self.action_confidence, 3),
            "entities": self.entities,
            "target_site": self.target_site,
            "action": self.action,
            "is_destructive": self.is_destructive,
            "is_recurring": self.is_recurring,
            "references_prior_turn": self.references_prior_turn,
            "needs_clarification": self.needs_clarification,
            "needs_confirmation": self.needs_confirmation,
            "matched": self.matched,
        }


# Categories whose misreading changes the world rather than just the reply.
# Only these are worth a read-back when confidence is middling (§30/§36).
_CONSEQUENTIAL = (
    IntentCategory.COMMAND,
    IntentCategory.AUTOMATION,
    IntentCategory.CORRECTION,
    IntentCategory.CONFIRMATION,
)


def _first_hit_index(cleaned: str, pattern: re.Pattern[str]) -> Optional[int]:
    """Token index of the first lexicon match, or None.

    Needed for §4: a control word is only a control word when it is *addressed*
    to the assistant. "stop" and "okay stop" are interruptions; "let's compare
    stop words" merely contains the token. Word-boundary matching alone cannot
    tell those apart — position can.
    """
    m = pattern.search(cleaned)
    if m is None:
        return None
    return len(_WORD_RE.findall(cleaned[: m.start()]))


def _is_addressed_control(index: Optional[int], total_words: int) -> bool:
    """A control word counts when it heads the utterance, or the utterance is
    short enough that nothing else can be the point of it."""
    if index is None:
        return False
    return index <= 1 or total_words <= 3


def detect_control_phrase(text: str) -> Optional[str]:
    """Public form of the §4 control test, for callers outside the kernel.

    Returns "stop", "cancel", "pause", "resume" or None. The legacy voice engine
    matched these by plain substring, which fired on "I am waiting for the build
    to finish", "that was quite good" and "find the paused video". Sharing this
    function rather than copying the rule keeps one tested definition of what
    counts as a control phrase.

    Stop and cancel are allowed head position in a longer sentence ("okay stop
    talking, I get it") because failing to stop is the worse error. Pause and
    resume are held to the stricter short-utterance test: they take objects far
    more often than not, and "continue my Python course" / "pause after 30
    seconds" are task parameters, not transport controls.
    """
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return None
    total = len(_WORD_RE.findall(cleaned))
    for name, pattern, strict in (
        ("stop", INTERRUPTION, False),
        ("cancel", CANCELLATION, False),
        ("pause", PAUSE_WORDS, True),
        ("resume", RESUME_WORDS, True),
    ):
        index = _first_hit_index(cleaned, pattern)
        if index is None:
            continue
        if total <= 3 if strict else _is_addressed_control(index, total):
            return name
    return None


class IntentClassifier:
    """Lexical + contextual classifier. Pure function of (text, context)."""

    #: Below this, ask instead of acting (§30).
    CLARIFY_BELOW = 0.45
    #: Between CLARIFY_BELOW and this, confirm the interpretation (§30).
    CONFIRM_BELOW = 0.72
    #: Destructive actions need at least this much STT confidence (§36).
    DESTRUCTIVE_STT_FLOOR = 0.80

    def classify(
        self,
        text: str,
        *,
        stt_confidence: float = 1.0,
        pending_question: bool = False,
        pending_confirmation: bool = False,
        task_active: bool = False,
        assistant_holds_floor: bool = False,
        known_entities: Optional[Sequence[str]] = None,
    ) -> Intent:
        raw = (text or "").strip()
        if not raw:
            return Intent(
                category=IntentCategory.SILENCE,
                text="",
                cleaned_text="",
                intent_confidence=1.0,
                entity_confidence=1.0,
                action_confidence=1.0,
            )

        cleaned = _FILLER.sub("", raw)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.")
        if not cleaned:
            # Pure filler ("um... uh...") is a backchannel, not a command.
            return Intent(
                category=IntentCategory.BACKCHANNEL,
                text=raw,
                cleaned_text="",
                intent_confidence=0.9,
                entity_confidence=1.0,
                action_confidence=1.0,
            )

        words = _WORD_RE.findall(cleaned.lower())
        matched: Dict[str, List[str]] = {}

        def hits(name: str, pattern: re.Pattern[str]) -> List[str]:
            found = [m.group(0) for m in pattern.finditer(cleaned)]
            if found:
                matched[name] = found
            return found

        h_interrupt = hits("interruption", INTERRUPTION)
        h_cancel = hits("cancellation", CANCELLATION)
        h_confirm = hits("confirmation", CONFIRMATION)
        h_reject = hits("rejection", REJECTION)
        h_back = hits("backchannel", BACKCHANNEL)
        h_correct = hits("correction", CORRECTION)
        h_clarify = hits("clarification", CLARIFICATION)
        h_social = hits("social", SOCIAL)
        h_question = hits("question", QUESTION_WORDS)
        h_command = hits("command", COMMAND_VERBS)
        h_automation = hits("automation", AUTOMATION_VERBS)
        h_recurrence = hits("recurrence", RECURRENCE)
        h_followup = hits("follow_up", FOLLOW_UP)
        h_pause = hits("pause", PAUSE_WORDS)
        h_resume = hits("resume", RESUME_WORDS)
        h_destructive = hits("destructive", DESTRUCTIVE)
        h_anaphora = hits("anaphora", ANAPHORA)

        entities, entity_confidence = self._entities(cleaned, known_entities)
        site = entities.get("site")
        is_short = len(words) <= 3

        category, confidence, action = self._decide(
            words=words,
            is_short=is_short,
            site=site,
            # §4 positional guard — see _is_addressed_control.
            interrupt_addressed=_is_addressed_control(
                _first_hit_index(cleaned, INTERRUPTION) if h_interrupt else None,
                len(words),
            ),
            cancel_addressed=_is_addressed_control(
                _first_hit_index(cleaned, CANCELLATION) if h_cancel else None,
                len(words),
            ),
            task_active=task_active,
            assistant_holds_floor=assistant_holds_floor,
            pending_question=pending_question,
            pending_confirmation=pending_confirmation,
            h_interrupt=h_interrupt,
            h_cancel=h_cancel,
            h_confirm=h_confirm,
            h_reject=h_reject,
            h_back=h_back,
            h_correct=h_correct,
            h_clarify=h_clarify,
            h_social=h_social,
            h_question=h_question,
            h_command=h_command,
            h_automation=h_automation,
            h_recurrence=h_recurrence,
            h_followup=h_followup,
            h_pause=h_pause,
            h_resume=h_resume,
        )

        destructive = bool(h_destructive)
        action_confidence = self._action_confidence(
            category=category,
            action=action,
            site=site,
            stt_confidence=stt_confidence,
        )

        intent = Intent(
            category=category,
            text=raw,
            cleaned_text=cleaned,
            intent_confidence=round(min(1.0, confidence * stt_confidence ** 0.5), 4),
            entity_confidence=round(entity_confidence, 4),
            action_confidence=round(action_confidence, 4),
            entities=entities,
            target_site=site,
            action=action,
            is_destructive=destructive,
            is_recurring=bool(h_recurrence),
            references_prior_turn=bool(h_anaphora) or category is IntentCategory.FOLLOW_UP,
            matched=matched,
        )
        self._apply_confidence_gates(intent, stt_confidence)
        return intent

    # ── category decision ─────────────────────────────────────────────────
    def _decide(self, **k: Any) -> Tuple[IntentCategory, float, Optional[str]]:
        words: List[str] = k["words"]
        is_short: bool = k["is_short"]
        site: Optional[str] = k["site"]

        # 1. Hard interruption always wins, in any state (§4, §18 P0) — but only
        #    when the stop word is actually addressed to us. This is the guard
        #    the legacy substring matcher lacked: "let's compare stop words"
        #    contains "stop" and is not an interruption.
        if k["h_interrupt"] and k["interrupt_addressed"]:
            # "wait" / "hold on" mid-execution is a pause, not a full stop.
            if k["task_active"] and not is_short:
                return IntentCategory.INTERRUPTION, 0.95, "pause"
            return IntentCategory.INTERRUPTION, 0.97, "stop"

        # 2. Explicit cancellation.
        if k["h_cancel"] and k["cancel_addressed"]:
            return IntentCategory.CANCELLATION, 0.95, "cancel"

        # 3. Correction beats confirmation: "actually, staging" contains no
        #    yes/no but must re-plan rather than re-answer (§35).
        if k["h_correct"]:
            return IntentCategory.CORRECTION, 0.88, "amend_plan"

        # 4. Answers to a question we actually asked.
        if k["pending_confirmation"] or k["pending_question"]:
            if k["h_reject"]:
                return IntentCategory.REJECTION, 0.94, "reject"
            if k["h_confirm"]:
                return IntentCategory.CONFIRMATION, 0.94, "confirm"

        # 5. Pause / resume during a running task (§14).
        if k["task_active"]:
            if k["h_pause"] and is_short:
                return IntentCategory.INTERRUPTION, 0.9, "pause"
            if k["h_resume"] and is_short:
                return IntentCategory.CONFIRMATION, 0.9, "resume"

        # 6. Clarification requests from the user.
        if k["h_clarify"]:
            return IntentCategory.CLARIFICATION, 0.85, "clarify"

        # 7. Backchannel — only when short and carrying no verb or target.
        #    "okay" is BACKCHANNEL; "okay open youtube" is AUTOMATION.
        if k["h_back"] and is_short and not k["h_automation"] and not k["h_command"] and not site:
            # A bare "no" still vetoes, even unprompted — it almost always means
            # "don't". A bare "yes" with nothing pending agrees with nothing, so
            # it stays conversational glue (§7).
            if k["h_reject"] and len(words) == 1:
                return IntentCategory.REJECTION, 0.8, "reject"
            return IntentCategory.BACKCHANNEL, 0.9, None

        # 8. Bare yes / no with nothing pending. Rule 4 already handled the case
        #    where we actually asked something, so there is no proposal for an
        #    affirmative to attach to: treat it as glue rather than inventing an
        #    action to confirm (§7 — "yeah" must not become a new task). A
        #    negative still carries intent, so it keeps its category.
        if is_short and k["h_confirm"] and not k["h_automation"]:
            return IntentCategory.BACKCHANNEL, 0.75, None
        if is_short and k["h_reject"] and not k["h_automation"]:
            return IntentCategory.REJECTION, 0.7, "reject"

        # 9. Scheduled / recurring work is automation regardless of phrasing.
        if k["h_recurrence"]:
            return IntentCategory.AUTOMATION, 0.9, "schedule"

        # 10. Follow-up on the previous operation.
        if k["h_followup"] and (k["task_active"] or is_short):
            return IntentCategory.FOLLOW_UP, 0.8, "repeat_last"

        # 11. Real work: site targets and multi-step verbs.
        automation_score = len(k["h_automation"]) + (2 if site else 0)
        command_score = len(k["h_command"])
        question_score = len(k["h_question"])

        if automation_score and automation_score >= command_score:
            confidence = min(0.95, 0.62 + 0.1 * automation_score)
            return IntentCategory.AUTOMATION, confidence, self._automation_action(site)
        if command_score:
            confidence = min(0.92, 0.6 + 0.12 * command_score)
            return IntentCategory.COMMAND, confidence, "desktop_command"

        # 12. Social pleasantries — but only if that is all the turn contains.
        if k["h_social"] and not question_score:
            return IntentCategory.SOCIAL_CONVERSATION, 0.85, None

        # 13. Questions and statements.
        if question_score:
            confidence = min(0.9, 0.6 + 0.1 * question_score)
            return IntentCategory.QUESTION, confidence, "answer"
        if k["h_social"]:
            return IntentCategory.SOCIAL_CONVERSATION, 0.7, None
        return IntentCategory.INFORMATION, 0.55, "acknowledge"

    @staticmethod
    def _automation_action(site: Optional[str]) -> str:
        return f"{site}_flow" if site else "plan_and_execute"

    # ── entity extraction ─────────────────────────────────────────────────
    def _entities(
        self, text: str, known: Optional[Sequence[str]]
    ) -> Tuple[Dict[str, Any], float]:
        lowered = text.lower()
        entities: Dict[str, Any] = {}
        confidence = 1.0

        found_sites = [s for s in AUTOMATION_SITES if re.search(rf"\b{re.escape(s)}\b", lowered)]
        if len(found_sites) == 1:
            entities["site"] = found_sites[0]
        elif len(found_sites) > 1:
            # Ambiguous target — pick the first mentioned but flag the doubt so
            # the policy layer can confirm rather than guess (§30).
            entities["site"] = min(found_sites, key=lambda s: lowered.index(s))
            entities["site_alternatives"] = found_sites
            confidence = 0.55

        for label, pattern in (
            ("url", r"\bhttps?://\S+"),
            ("file_path", r"\b[\w./\\-]+\.(?:py|ts|tsx|js|jsx|java|json|md|yml|yaml|sql|txt|csv)\b"),
            ("count", r"\b(\d{1,4})\b"),
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                entities[label] = match.group(0)

        pause_after = re.search(r"\bpause\s+after\s+(\d+)\s*(second|sec|minute|min)", lowered)
        if pause_after:
            seconds = int(pause_after.group(1))
            if pause_after.group(2).startswith("min"):
                seconds *= 60
            entities["pause_after_seconds"] = seconds

        for language in ("telugu", "hindi", "english", "tamil", "kannada"):
            if re.search(rf"\bin\s+{language}\b", lowered):
                entities["language_filter"] = language.title()
                break

        query = re.search(
            r"\b(?:search(?:\s+for)?|look\s+up|find|play|open)\s+(.{2,80}?)"
            r"(?=\s+(?:and|then|in\s+telugu|in\s+hindi|on\s+\w+)\b|[.?!]|$)",
            text,
            re.IGNORECASE,
        )
        if query:
            candidate = query.group(1).strip(" ,.\"'")
            if candidate and candidate.lower() not in AUTOMATION_SITES:
                entities["query"] = candidate

        if known:
            resolved = [name for name in known if name.lower() in lowered]
            if resolved:
                entities["known_references"] = resolved

        # An anaphoric target with nothing concrete to bind to is low-confidence.
        if not entities and ANAPHORA.search(text):
            confidence = 0.5
        return entities, confidence

    def _action_confidence(
        self,
        *,
        category: IntentCategory,
        action: Optional[str],
        site: Optional[str],
        stt_confidence: float,
    ) -> float:
        if category in (
            IntentCategory.INTERRUPTION,
            IntentCategory.CANCELLATION,
            IntentCategory.BACKCHANNEL,
            IntentCategory.SILENCE,
        ):
            # Control paths must not be gated on a shaky transcript — refusing
            # to stop is worse than stopping spuriously.
            return 1.0
        if action is None:
            return 0.6
        # A resolved site raises confidence, but its absence is not suspicious:
        # "rename this file" has no site and is perfectly actionable. Basing the
        # no-site case too low made *every* local command demand a read-back.
        base = 0.92 if site else 0.82
        return max(0.1, min(1.0, base * (0.5 + 0.5 * stt_confidence)))

    def _apply_confidence_gates(self, intent: Intent, stt_confidence: float) -> None:
        """§30 + §36: decide whether to act, confirm, or ask."""
        if intent.category in (
            IntentCategory.INTERRUPTION,
            IntentCategory.CANCELLATION,
            IntentCategory.BACKCHANNEL,
            IntentCategory.SILENCE,
        ):
            return

        score = intent.min_confidence
        if score < self.CLARIFY_BELOW:
            intent.needs_clarification = True
        elif score < self.CONFIRM_BELOW and intent.category in _CONSEQUENTIAL:
            # §30 gates *operations*, not conversation. Reading back "Just to
            # confirm: what are you doing?" is worse than answering imperfectly:
            # a wrong answer costs a sentence, a wrong action costs a database.
            intent.needs_confirmation = True

        if intent.entities.get("site_alternatives"):
            intent.needs_clarification = True

        # §36: "delete the production database" heard at 0.62 must be read back.
        if intent.is_destructive and stt_confidence < self.DESTRUCTIVE_STT_FLOOR:
            intent.needs_confirmation = True
        elif intent.is_destructive and intent.is_actionable:
            intent.needs_confirmation = True
