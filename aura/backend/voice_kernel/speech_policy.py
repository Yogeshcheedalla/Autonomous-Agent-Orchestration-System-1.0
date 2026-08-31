"""
voice_kernel.speech_policy — "should I respond?" and speech priority (§9, §17, §18, §34).
========================================================================================

Two jobs, kept together because they are the same decision seen from two sides:

  `decide()`   Given a classified turn and the current situation, what should
               Akansha *do* — speak, listen, execute, ask, stay silent?
  `admit()`    Given something Akansha wants to say, should it actually reach
               the speaker right now, and in what order?

The second half is what stops the "narrates every tool call" failure mode. P6
events are dropped outright; ordinary progress is rate-limited; a P0 stop
pre-empts whatever is mid-sentence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional, Tuple

from .intent import Intent, IntentCategory
from .situation import ConversationMode, SituationModel
from .states import VoiceState


class Priority(IntEnum):
    """§18 — lower is more urgent."""

    P0_EMERGENCY = 0        # stop / abort acknowledgement
    P1_SAFETY = 1           # destructive-action confirmation
    P2_DIRECT_ANSWER = 2    # the user asked; answer
    P3_FAILURE = 3          # a task failed
    P4_KEY_PROGRESS = 4     # milestone the user would want to hear
    P5_PROGRESS = 5         # ordinary progress
    P6_INTERNAL = 6         # tool chatter — never spoken


class ResponseAction(str, Enum):
    """§9 — the response-decision layer's vocabulary."""

    SPEAK = "speak"
    LISTEN = "listen"
    EXECUTE = "execute"
    ASK = "ask"
    STAY_SILENT = "stay_silent"
    ACKNOWLEDGE = "acknowledge"
    INTERRUPT_SELF = "interrupt_self"
    WAIT = "wait"
    END_CONVERSATION = "end_conversation"


@dataclass(slots=True)
class Utterance:
    """Something Akansha may say."""

    text: str
    priority: Priority = Priority.P5_PROGRESS
    #: Utterances sharing a dedupe key collapse to the most recent one.
    dedupe_key: Optional[str] = None
    interruptible: bool = True
    created_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class PolicyDecision:
    action: ResponseAction
    reason: str
    utterance: Optional[Utterance] = None
    #: Set when the decision is ASK — the question to put to the user.
    question: Optional[str] = None
    #: Set when the decision is EXECUTE — how autonomous we may be.
    requires_confirmation: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "text": self.utterance.text if self.utterance else None,
            "priority": int(self.utterance.priority) if self.utterance else None,
            "question": self.question,
            "requires_confirmation": self.requires_confirmation,
        }


# §34 — phrases that *may* end a conversation. Deliberately not treated as
# terminal on their own: "thanks" while a deploy is running means "thanks",
# not "abandon the deploy".
_CLOSING = (
    "that's all", "thats all", "that is all", "we're done", "were done",
    "we are done", "i'm done", "im done", "nothing else", "no more",
    "goodbye", "bye", "see you", "good night", "talk later", "sarey ipudu",
)
_THANKS = ("thanks", "thank you", "thankyou", "thx", "dhanyavaad", "ధన్యవాదాలు")


class SpeechPolicy:
    """Decides whether to speak, and gate-keeps everything that gets spoken."""

    #: Minimum gap between two ordinary progress updates (§17).
    progress_min_gap_s: float = 6.0
    #: Utterances older than this are stale — drop rather than say them late.
    max_staleness_s: float = 12.0

    def __init__(self) -> None:
        self._last_progress_at: float = 0.0
        self._queue: List[Utterance] = []
        self._spoken_keys: Dict[str, float] = {}

    # ── §9: should I respond? ─────────────────────────────────────────────
    def decide(
        self,
        intent: Intent,
        situation: SituationModel,
        state: VoiceState,
        *,
        task_active: bool = False,
    ) -> PolicyDecision:
        category = intent.category

        # P0 — a stop lands in every state, including mid-tool-execution (§20).
        if category is IntentCategory.INTERRUPTION:
            if intent.action == "pause":
                return PolicyDecision(
                    ResponseAction.WAIT,
                    "user asked to hold",
                    Utterance("Paused.", Priority.P0_EMERGENCY, "pause-ack"),
                )
            return PolicyDecision(
                ResponseAction.INTERRUPT_SELF,
                "explicit stop",
                Utterance("Stopped.", Priority.P0_EMERGENCY, "stop-ack", interruptible=False),
            )

        if category is IntentCategory.CANCELLATION:
            return PolicyDecision(
                ResponseAction.INTERRUPT_SELF,
                "explicit cancellation",
                Utterance("Cancelled.", Priority.P0_EMERGENCY, "cancel-ack", interruptible=False),
            )

        # Silence: never fill it with noise.
        if category is IntentCategory.SILENCE:
            return PolicyDecision(ResponseAction.LISTEN, "no speech content")

        # §7 — a backchannel keeps the floor with the user. Do not answer it.
        if category is IntentCategory.BACKCHANNEL:
            if situation.pending_question:
                # "mm-hmm" in reply to a real question is not an answer.
                return PolicyDecision(
                    ResponseAction.WAIT,
                    "backchannel while a question is pending",
                )
            return PolicyDecision(ResponseAction.LISTEN, "backchannel — continue")

        # Answers to something we asked.
        if category is IntentCategory.CONFIRMATION:
            if situation.pending_confirmation:
                return PolicyDecision(
                    ResponseAction.EXECUTE, "user confirmed the pending action"
                )
            if intent.action == "resume":
                return PolicyDecision(ResponseAction.EXECUTE, "resume requested")
            if situation.pending_question:
                return PolicyDecision(ResponseAction.EXECUTE, "affirmative answer")
            return PolicyDecision(ResponseAction.LISTEN, "bare acknowledgement, nothing pending")

        if category is IntentCategory.REJECTION:
            if situation.pending_confirmation or situation.pending_question:
                return PolicyDecision(
                    ResponseAction.ACKNOWLEDGE,
                    "user declined the pending action",
                    Utterance("Understood, leaving it.", Priority.P2_DIRECT_ANSWER, "reject-ack"),
                )
            return PolicyDecision(ResponseAction.LISTEN, "negation with nothing pending")

        # §35 — corrections re-plan; they never restart the conversation.
        if category is IntentCategory.CORRECTION:
            if intent.needs_clarification:
                return PolicyDecision(
                    ResponseAction.ASK,
                    "correction with no resolvable target",
                    question=self._clarify_question(intent, situation),
                )
            return PolicyDecision(ResponseAction.EXECUTE, "correction — amend the plan")

        if category is IntentCategory.CLARIFICATION:
            return PolicyDecision(ResponseAction.SPEAK, "user asked us to clarify")

        # §34 — distinguish conversation end from task end.
        if category is IntentCategory.SOCIAL_CONVERSATION:
            lowered = intent.cleaned_text.lower()
            closing = any(p in lowered for p in _CLOSING)
            thanks = any(p in lowered for p in _THANKS)
            if closing and not task_active:
                return PolicyDecision(
                    ResponseAction.END_CONVERSATION,
                    "closing phrase with no work in flight",
                    Utterance("Talk soon.", Priority.P2_DIRECT_ANSWER, "farewell"),
                )
            if closing and task_active:
                return PolicyDecision(
                    ResponseAction.ACKNOWLEDGE,
                    "closing phrase but a task is still running",
                    Utterance(
                        "I'll keep working on the current task and stay quiet unless something needs you.",
                        Priority.P2_DIRECT_ANSWER,
                        "closing-with-task",
                    ),
                )
            if thanks and situation.speaks_only_when_necessary:
                return PolicyDecision(ResponseAction.STAY_SILENT, "quiet mode, social turn")
            return PolicyDecision(ResponseAction.SPEAK, "social turn")

        # Real work. Confidence gates first (§30, §36).
        if intent.needs_clarification:
            return PolicyDecision(
                ResponseAction.ASK,
                "confidence below the act threshold",
                question=self._clarify_question(intent, situation),
            )
        if intent.needs_confirmation:
            return PolicyDecision(
                ResponseAction.ASK,
                "consequential or moderately confident — read it back",
                question=self._confirm_question(intent),
                requires_confirmation=True,
            )

        if category in (IntentCategory.AUTOMATION, IntentCategory.COMMAND, IntentCategory.FOLLOW_UP):
            if category is IntentCategory.FOLLOW_UP and not self._has_antecedent(situation):
                return PolicyDecision(
                    ResponseAction.ASK,
                    "follow-up with no antecedent",
                    question="What should I apply that to?",
                )
            return PolicyDecision(ResponseAction.EXECUTE, f"{category.value.lower()} intent")

        if category is IntentCategory.QUESTION:
            return PolicyDecision(ResponseAction.SPEAK, "direct question")

        # INFORMATION — the user narrating their own actions ("okay, I'm
        # opening the browser..."). §9 says do not necessarily answer.
        if situation.speaks_only_when_necessary:
            return PolicyDecision(ResponseAction.STAY_SILENT, "quiet mode, statement")
        if intent.references_prior_turn or situation.pending_question:
            return PolicyDecision(ResponseAction.SPEAK, "statement bearing on the open thread")
        return PolicyDecision(ResponseAction.LISTEN, "user is narrating; stay out of the way")

    # ── §18: admission control for outbound speech ─────────────────────────
    def admit(
        self,
        utterance: Utterance,
        situation: SituationModel,
        state: VoiceState,
    ) -> Tuple[bool, str]:
        """Should this utterance be spoken now?"""
        if utterance.priority >= Priority.P6_INTERNAL:
            return False, "P6 internal events are never spoken"

        if situation.mode is ConversationMode.QUIET_MODE and utterance.priority > Priority.P3_FAILURE:
            return False, "quiet mode suppresses non-critical speech"

        if state is VoiceState.INTERRUPTED and utterance.priority > Priority.P1_SAFETY:
            return False, "user is interrupting; hold non-critical speech"

        if utterance.dedupe_key:
            last = self._spoken_keys.get(utterance.dedupe_key)
            if last is not None and (time.monotonic() - last) < 3.0:
                return False, f"duplicate of {utterance.dedupe_key} within 3s"

        if utterance.priority >= Priority.P5_PROGRESS:
            gap = time.monotonic() - self._last_progress_at
            if gap < self.progress_min_gap_s:
                return False, f"progress rate limit ({gap:.1f}s < {self.progress_min_gap_s}s)"

        age = time.monotonic() - utterance.created_at
        if age > self.max_staleness_s and utterance.priority >= Priority.P4_KEY_PROGRESS:
            return False, f"stale by {age:.1f}s"

        return True, "admitted"

    def mark_spoken(self, utterance: Utterance) -> None:
        if utterance.dedupe_key:
            self._spoken_keys[utterance.dedupe_key] = time.monotonic()
        if utterance.priority >= Priority.P5_PROGRESS:
            self._last_progress_at = time.monotonic()

    def enqueue(self, utterance: Utterance) -> None:
        """Queue an utterance, keeping the queue priority-ordered and small."""
        if utterance.priority >= Priority.P6_INTERNAL:
            return
        if utterance.dedupe_key:
            self._queue = [q for q in self._queue if q.dedupe_key != utterance.dedupe_key]
        self._queue.append(utterance)
        self._queue.sort(key=lambda u: (int(u.priority), u.created_at))
        del self._queue[16:]

    def drain(self, situation: SituationModel, state: VoiceState) -> List[Utterance]:
        """Pop everything currently admissible, most urgent first."""
        kept: List[Utterance] = []
        out: List[Utterance] = []
        for utterance in self._queue:
            ok, _ = self.admit(utterance, situation, state)
            if ok:
                out.append(utterance)
                self.mark_spoken(utterance)
            elif (time.monotonic() - utterance.created_at) < self.max_staleness_s:
                kept.append(utterance)
        self._queue = kept
        return out

    def flush_non_critical(self) -> int:
        """Barge-in: drop everything we were about to say. Returns count dropped."""
        before = len(self._queue)
        self._queue = [u for u in self._queue if u.priority <= Priority.P1_SAFETY]
        return before - len(self._queue)

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _has_antecedent(situation: SituationModel) -> bool:
        r = situation.references
        return any((r.last_action, r.last_object, r.last_file, r.current_task))

    @staticmethod
    def _clarify_question(intent: Intent, situation: SituationModel) -> str:
        alternatives = intent.entities.get("site_alternatives")
        if alternatives:
            joined = ", ".join(alternatives[:-1]) + f" or {alternatives[-1]}"
            return f"Did you mean {joined}?"
        if intent.references_prior_turn and not SpeechPolicy._has_antecedent(situation):
            return "Which one do you mean?"
        return f"I caught “{intent.cleaned_text}” but I'm not sure what to do with it. Can you say it another way?"

    @staticmethod
    def _confirm_question(intent: Intent) -> str:
        if intent.is_destructive:
            return f"I heard “{intent.cleaned_text}”. That's destructive — should I go ahead?"
        return f"Just to confirm: {intent.cleaned_text}?"
