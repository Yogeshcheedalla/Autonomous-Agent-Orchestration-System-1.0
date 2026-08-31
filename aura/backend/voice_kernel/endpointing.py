"""
voice_kernel.endpointing — end-of-turn detection (§6).
=====================================================

The failure mode this module exists to prevent: the user says

    "Open my project and..."          <- 300ms pause
    "...find the failing tests."

and a naive 200ms-silence endpointer submits the first fragment, so Akansha
opens the project and stops. The old path in `VoiceAssistantClient` used a
flat 1200ms timer for every utterance, which fails the other way — it makes
"stop" feel sluggish while still cutting off slow speakers.

Instead we compute an explicit `turn_complete_probability` from independent
signals and only finalize above a threshold that itself adapts to the
conversational situation.

Signals, and why each one is here:

  silence        Longest single-signal predictor, but useless alone.
  duration       Very short utterances after a long pause are usually
                 complete ("yes", "stop"); very long ones need more slack.
  lexical        A trailing conjunction/preposition/determiner is near-proof
                 the speaker is not done. This is the signal that fixes the
                 "Open my project and..." case.
  punctuation    Good recognisers emit terminal punctuation on endpoint.
  semantic       Does the fragment contain a predicate at all?
  prosody        Falling pitch / falling energy at the tail. Optional: only
                 contributes when the transport supplies it.
  situation      A pending yes/no question lowers the bar; an in-flight task
                 raises it slightly (we would rather listen than interrupt).

Nothing here does I/O, so the whole thing is unit-testable at the
millisecond level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── lexical evidence ────────────────────────────────────────────────────────

# Words that essentially cannot end an English sentence. If the tail of the
# transcript is one of these, the speaker has more to say.
_DANGLING = {
    # coordinating / subordinating conjunctions
    "and", "or", "but", "so", "because", "although", "though", "while",
    "if", "unless", "until", "when", "whenever", "since", "whereas", "plus",
    # prepositions
    "of", "to", "in", "on", "at", "for", "with", "from", "by", "about",
    "into", "onto", "over", "under", "through", "between", "against",
    "without", "within", "toward", "towards", "upon", "via",
    # determiners / quantifiers
    "the", "a", "an", "my", "your", "our", "their", "his", "her", "its",
    "this", "that", "these", "those", "some", "any", "every", "each",
    "another", "both", "either", "neither",
    # auxiliaries and infinitive markers
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "can", "could", "should", "shall", "may", "might",
    "must", "let", "gonna", "going",
    # comparatives that demand a complement
    "than", "as", "such",
}

# Telugu / Hindi tails that behave the same way. Romanised and script forms,
# because the recogniser may emit either.
_DANGLING_INDIC = {
    # Telugu romanised connectives
    "mariyu", "tarvata", "appudu", "kaani", "ala", "inka", "tho", "ki",
    "lo", "nundi", "gurinchi",
    # Hindi romanised connectives
    "aur", "phir", "lekin", "kyunki", "agar", "toh", "ke", "ka", "ki",
    "se", "mein", "par",
    # script forms
    "మరియు", "తర్వాత", "కాని", "అప్పుడు", "ఇంకా",
    "और", "फिर", "लेकिन", "क्योंकि", "अगर",
}

# Explicit "I am still talking" markers. Distinct from _DANGLING: these can
# appear mid-utterance and still mean more is coming.
_CONTINUATION = (
    "and then", "after that", "and also", "as well as", "followed by",
    "next up", "one more thing", "also", "additionally", "furthermore",
    "tarvata", "aur phir", "inka okati",
)

_TERMINAL_PUNCT = re.compile(r"[.!?。？！]\s*$")
_COMMA_TAIL = re.compile(r"[,;:\-–—]\s*$")

# A crude predicate detector: an imperative verb up front, or any finite verb.
_PREDICATE = re.compile(
    r"\b(open|close|find|search|play|stop|run|build|deploy|fix|check|show|"
    r"tell|make|create|delete|move|copy|send|write|read|list|start|install|"
    r"update|cancel|pause|resume|is|are|was|were|do|does|did|can|could|will|"
    r"would|should|have|has|had|want|need|like|know|think|go|get|put|take|"
    r"chey|cheyyi|chestha|teruvu|vetuku|karo|kholo|dhundo|banao)\b",
    re.IGNORECASE,
)

_WORD = re.compile(r"[\w'ऀ-ॿఀ-౿]+")

# Utterances that are grammatically finished the instant they are spoken. No
# amount of extra silence can make "stop" more complete, and §4 says a barge-in
# must land fast, so these skip the weighted evidence and finalize at the
# minimum-silence floor. Kept deliberately small: every entry must be a phrase
# that cannot be the *head* of a longer sentence the user is still building.
_STANDALONE = frozenset({
    # control (§4/§20)
    "stop", "stop it", "stop that", "stop now", "wait", "hold on", "hang on",
    "quiet", "be quiet", "enough", "cancel", "cancel that", "abort",
    "pause", "resume", "continue", "never mind", "nevermind", "forget it",
    # answers (§30 — a terse yes/no is a whole turn)
    "yes", "yeah", "yep", "yup", "no", "nope", "nah", "okay", "ok", "sure",
    "go ahead", "do it", "correct", "wrong",
    # Telugu / Hindi equivalents, romanised and script
    "aagu", "aapu", "ruko", "ruk jao", "bas", "sare", "avunu", "kaadu",
    "vaddu", "haan", "nahi", "theek hai",
    "ఆపు", "ఆగు", "సరే", "అవును", "కాదు", "వద్దు",
    "रुको", "बस", "हाँ", "नहीं", "ठीक है",
})


def _is_standalone(words: List[str]) -> bool:
    if not words or len(words) > 2:
        return False
    return " ".join(w.lower() for w in words) in _STANDALONE


@dataclass(slots=True)
class SpeechSegment:
    """One contiguous run of user speech as measured by the VAD."""

    started_at_ms: float
    ended_at_ms: Optional[float] = None
    peak_rms: float = 0.0
    frames: int = 0

    @property
    def duration_ms(self) -> float:
        if self.ended_at_ms is None:
            return 0.0
        return max(0.0, self.ended_at_ms - self.started_at_ms)


@dataclass(slots=True)
class EndpointDecision:
    """Result of one endpointing evaluation."""

    turn_complete_probability: float
    threshold: float
    should_finalize: bool
    #: Per-signal contributions, for the debug panel and for tests.
    signals: Dict[str, float] = field(default_factory=dict)
    #: How long to keep waiting before re-evaluating, in ms.
    recheck_in_ms: int = 200
    reason: str = ""


class EndpointDetector:
    """Computes `turn_complete_probability` for the current utterance.

    Tuning constants are instance attributes so the UI can expose them (§5
    "the VAD must be configurable") without reaching into module globals.
    """

    # Silence at which a plain, unambiguous sentence is considered over.
    base_silence_ms: float = 700.0
    # Never finalize before this much silence, no matter how complete the text
    # looks — protects against the recogniser emitting `isFinal` mid-thought.
    min_silence_ms: float = 240.0
    # Hard ceiling: finalize regardless of lexical evidence. Without this a
    # trailing "and" would keep the turn open forever.
    max_silence_ms: float = 2600.0
    # Default probability threshold for finalizing.
    base_threshold: float = 0.62

    def __init__(self, **overrides: float) -> None:
        for key, value in overrides.items():
            if not hasattr(self, key):
                raise AttributeError(f"unknown endpointing option {key!r}")
            setattr(self, key, float(value))

    # ── individual signals ────────────────────────────────────────────────
    def _silence_signal(self, silence_ms: float) -> float:
        """Saturating ramp from min_silence_ms to base_silence_ms."""
        if silence_ms <= self.min_silence_ms:
            return 0.0
        span = max(1.0, self.base_silence_ms - self.min_silence_ms)
        return min(1.0, (silence_ms - self.min_silence_ms) / span)

    def _duration_signal(self, word_count: int) -> float:
        """Short utterances are usually whole; very long ones need slack."""
        if word_count == 0:
            return 0.0
        if word_count <= 2:
            return 0.9      # "stop", "yes", "go ahead"
        if word_count <= 6:
            return 0.6
        if word_count <= 18:
            return 0.45
        return 0.3          # long dictation — be patient

    def _lexical_signal(self, text: str, words: List[str]) -> Tuple[float, str, bool]:
        """Negative evidence dominates here: a dangling tail vetoes the turn.

        The third element says whether this is a *structural* veto rather than a
        vote. "and" at the end of an utterance is not weak evidence that more is
        coming — it is a grammatical guarantee, and no amount of silence,
        confident punctuation or recogniser certainty makes "Open my project and"
        a finished sentence. Only `max_silence_ms` overrides it.
        """
        if not words:
            return 0.0, "empty", False
        tail = words[-1].lower().strip("'")
        if tail in _DANGLING or tail in _DANGLING_INDIC:
            return 0.02, f"dangling tail {tail!r}", True
        lowered = text.lower()
        # A continuation marker in the last few words means more is coming.
        tail_window = " ".join(words[-4:]).lower()
        for marker in _CONTINUATION:
            if tail_window.endswith(marker):
                return 0.08, f"continuation marker {marker!r}", True
        if any(marker in lowered for marker in _CONTINUATION):
            # Marker earlier in the utterance: weak evidence only, and genuinely
            # a vote — "open chrome and then tell me the time" is complete.
            return 0.5, "continuation marker mid-utterance", False
        return 0.85, "clean tail", False

    def _punctuation_signal(self, text: str) -> Tuple[float, str]:
        if _TERMINAL_PUNCT.search(text):
            return 1.0, "terminal punctuation"
        if _COMMA_TAIL.search(text):
            return 0.05, "comma tail"
        return 0.5, "no punctuation"

    def _semantic_signal(self, text: str, words: List[str]) -> float:
        """Does this look like a whole thought rather than a fragment?"""
        if not words:
            return 0.0
        if len(words) == 1:
            # A bare word is complete only if it is a known standalone reply,
            # which the intent layer handles; here treat it as near-complete
            # so short control words are not delayed.
            return 0.8
        return 0.85 if _PREDICATE.search(text) else 0.35

    def _prosody_signal(self, prosody: Optional[Dict[str, float]]) -> Optional[float]:
        """Falling pitch and decaying energy both indicate a finished clause.

        Returns None when the transport did not supply prosody, so the weight
        can be redistributed rather than defaulting to a misleading 0.5.
        """
        if not prosody:
            return None
        score = 0.5
        pitch_delta = prosody.get("pitch_delta_hz")
        if pitch_delta is not None:
            # Falling contour → complete. Rising → question or continuation.
            score += -0.30 if pitch_delta > 12 else 0.30 if pitch_delta < -8 else 0.0
        energy_delta = prosody.get("energy_delta")
        if energy_delta is not None:
            score += 0.20 if energy_delta < -0.02 else -0.10
        return max(0.0, min(1.0, score))

    # ── threshold adaptation (§6 "conversation state", "task state") ───────
    def _threshold_for(
        self,
        *,
        pending_question: bool,
        executing: bool,
        holds_floor: bool,
    ) -> Tuple[float, List[str]]:
        threshold = self.base_threshold
        notes: List[str] = []
        if pending_question:
            # We asked something; a terse answer is expected. Finalize eagerly.
            threshold -= 0.14
            notes.append("pending question")
        if executing:
            # A task is running: prefer listening a beat longer over cutting in.
            threshold += 0.06
            notes.append("task executing")
        if holds_floor:
            # This is a barge-in. Act fast — the user is overriding us.
            threshold -= 0.18
            notes.append("barge-in")
        return max(0.25, min(0.9, threshold)), notes

    # ── public API ────────────────────────────────────────────────────────
    def evaluate(
        self,
        *,
        transcript: str,
        silence_ms: float,
        speech_duration_ms: float = 0.0,
        prosody: Optional[Dict[str, float]] = None,
        pending_question: bool = False,
        executing: bool = False,
        holds_floor: bool = False,
        recognizer_final: bool = False,
    ) -> EndpointDecision:
        text = (transcript or "").strip()
        words = _WORD.findall(text)

        threshold, notes = self._threshold_for(
            pending_question=pending_question,
            executing=executing,
            holds_floor=holds_floor,
        )

        if not words:
            return EndpointDecision(
                turn_complete_probability=0.0,
                threshold=threshold,
                should_finalize=False,
                signals={},
                recheck_in_ms=300,
                reason="no speech content",
            )

        # Hard floor: never finalize on a hair-trigger pause.
        if silence_ms < self.min_silence_ms and not recognizer_final:
            return EndpointDecision(
                turn_complete_probability=0.0,
                threshold=threshold,
                should_finalize=False,
                signals={"silence": 0.0},
                recheck_in_ms=int(self.min_silence_ms - silence_ms) or 60,
                reason="below minimum silence",
            )

        lexical, lexical_note, holds_open = self._lexical_signal(text, words)
        punct, punct_note = self._punctuation_signal(text)
        signals: Dict[str, float] = {
            "silence": self._silence_signal(silence_ms),
            "duration": self._duration_signal(len(words)),
            "lexical": lexical,
            "punctuation": punct,
            "semantic": self._semantic_signal(text, words),
        }
        weights: Dict[str, float] = {
            "silence": 0.34,
            "duration": 0.10,
            "lexical": 0.26,
            "punctuation": 0.12,
            "semantic": 0.18,
        }
        prosody_score = self._prosody_signal(prosody)
        if prosody_score is not None:
            signals["prosody"] = prosody_score
            weights["prosody"] = 0.12

        total_weight = sum(weights.values())
        probability = sum(signals[k] * weights[k] for k in weights) / total_weight

        # A finished standalone reply cannot be continued, so once we are past
        # the hair-trigger floor there is nothing left to wait for. Without this
        # a barge-in "stop" needs ~700ms of silence to clear the threshold,
        # which is exactly the sluggishness §4 complains about.
        if _is_standalone(words):
            probability = max(probability, 0.96)
            notes.append("standalone reply")

        # The recogniser declaring a final result is strong but not absolute
        # evidence — Chrome emits finals mid-thought on pauses.
        if recognizer_final:
            probability = min(1.0, probability + 0.15)
            notes.append("recognizer final")

        # An open grammatical structure is a veto, not a vote, so it is applied
        # *after* the bonuses rather than mixed in as one weighted signal among
        # five. As a weight it lost: "Open my project and" scored 0.6182 against
        # a 0.62 threshold — held back by 0.0018, and flipped outright by any of
        # `recognizer_final` (+0.15, which Chrome sets on every pause),
        # `pending_question` (-0.14) or `holds_floor` (-0.18). So the spec's
        # canonical example only worked in the one case none of those applied.
        #
        # The cap sits below `_threshold_for`'s own 0.25 floor, which is what
        # makes this a guarantee across every threshold adaptation rather than
        # another number to retune.
        if holds_open and silence_ms < self.max_silence_ms:
            probability = min(probability, 0.24)
            notes.append("structure open")

        # Hard ceiling, and deliberately last: a dangling "and" cannot hold the
        # turn open forever, so nothing above may override it.
        if silence_ms >= self.max_silence_ms:
            probability = max(probability, 0.95)
            notes.append("max silence reached")

        should = probability >= threshold
        remaining = max(60.0, self.base_silence_ms - silence_ms)
        return EndpointDecision(
            turn_complete_probability=round(probability, 4),
            threshold=round(threshold, 4),
            should_finalize=should,
            signals={k: round(v, 4) for k, v in signals.items()},
            recheck_in_ms=int(remaining),
            reason="; ".join([lexical_note, punct_note, *notes]),
        )
