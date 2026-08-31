"""
voice_kernel.situation — the Conversation Situation Model (§8, §13, §21, §32).
=============================================================================

What the old code lacked: any notion of "what is going on right now". Each
`/api/voice/chat` call re-derived everything from one transcript string, so
"Go to GitHub" after "Open Chrome" had no idea a browser was already open, and
"Actually don't do that" had nothing to point at.

This module holds that missing middle. It is deliberately a dumb container
with resolution helpers — the reasoning lives in the policy and planner layers,
so this can be snapshotted, serialised into a checkpoint (§38) and diffed for
the UI without dragging behaviour along with it.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ConversationMode(str, Enum):
    """§32 active conversation modes."""

    NORMAL = "NORMAL"
    VOICE = "VOICE"
    HANDS_FREE = "HANDS_FREE"
    TASK_MODE = "TASK_MODE"
    AUTONOMOUS_MODE = "AUTONOMOUS_MODE"
    QUIET_MODE = "QUIET_MODE"


class Speaker(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    NOBODY = "nobody"


@dataclass
class References:
    """§13 — the explicit antecedents that make "continue" and "the same" work.

    Every field is the *most recent* value of its kind. When the user says
    "do the same for the other file", the planner reads `last_action` +
    `last_file` from here instead of re-parsing conversation history.
    """

    current_task: Optional[str] = None
    current_step: Optional[str] = None
    last_action: Optional[str] = None
    last_object: Optional[str] = None
    last_file: Optional[str] = None
    last_application: Optional[str] = None
    last_url: Optional[str] = None
    last_command: Optional[str] = None
    #: The action we have described but not yet performed. "Actually, don't do
    #: that" cancels exactly this.
    pending_action: Optional[Dict[str, Any]] = None

    def note_action(
        self,
        action: str,
        *,
        obj: Optional[str] = None,
        file: Optional[str] = None,
        app: Optional[str] = None,
        url: Optional[str] = None,
        command: Optional[str] = None,
    ) -> None:
        self.last_action = action
        if obj:
            self.last_object = obj
        if file:
            self.last_file = file
        if app:
            self.last_application = app
        if url:
            self.last_url = url
        if command:
            self.last_command = command

    def resolve(self, phrase: str) -> Optional[str]:
        """Best-effort antecedent for a referring expression.

        Returns None when nothing plausible is bound, which the caller must
        treat as "ask, do not guess".
        """
        p = phrase.lower().strip()
        table: Dict[str, Optional[str]] = {
            "it": self.last_object or self.last_file or self.last_application,
            "that": self.last_action or self.last_object,
            "this": self.current_step or self.current_task,
            "the same": self.last_action,
            "again": self.last_command or self.last_action,
            "the file": self.last_file,
            "the other file": self.last_file,
            "the app": self.last_application,
            "the page": self.last_url,
            "the site": self.last_url,
            "continue": self.current_task,
            "the task": self.current_task,
            "the step": self.current_step,
        }
        for key, value in table.items():
            if p == key or p.endswith(key):
                return value
        return None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnvironmentState:
    """§21 — a cache of the world, refreshed on demand rather than polled.

    `stale_after_s` exists because scanning the whole desktop on every turn is
    exactly the behaviour the spec forbids.
    """

    active_window: Optional[str] = None
    open_applications: List[str] = field(default_factory=list)
    active_browser: Optional[str] = None
    browser_tabs: List[str] = field(default_factory=list)
    current_url: Optional[str] = None
    workspace: Optional[str] = None
    project: Optional[str] = None
    terminal_sessions: List[str] = field(default_factory=list)
    running_processes: List[str] = field(default_factory=list)
    selected_files: List[str] = field(default_factory=list)
    updated_at: float = 0.0
    stale_after_s: float = 20.0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.updated_at) > self.stale_after_s

    def apply(self, patch: Dict[str, Any]) -> List[str]:
        """Merge a partial update. Returns the names of fields that changed."""
        changed: List[str] = []
        for key, value in patch.items():
            if not hasattr(self, key) or key in ("updated_at", "stale_after_s"):
                continue
            if getattr(self, key) != value:
                setattr(self, key, value)
                changed.append(key)
        self.updated_at = time.time()
        return changed

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["is_stale"] = self.is_stale
        return data


@dataclass
class SituationModel:
    """§8 — everything needed to answer "should I respond, and to what?"."""

    session_id: str
    mode: ConversationMode = ConversationMode.VOICE
    current_speaker: Speaker = Speaker.NOBODY

    current_topic: Optional[str] = None
    user_intent: Optional[str] = None
    user_emotion_signal: Optional[str] = None
    urgency: float = 0.0

    #: A question Akansha asked and is waiting on.
    pending_question: Optional[str] = None
    #: A destructive/ambiguous action awaiting an explicit yes.
    pending_confirmation: Optional[Dict[str, Any]] = None

    interruption_count: int = 0
    turn_count: int = 0
    #: §33 — speech heard while the wake-word gate was armed. Counted rather than
    #: discarded so "it ignored me" is diagnosable instead of invisible, and kept
    #: out of the context turns on purpose: an ambient mic hears conversations
    #: that were never addressed to us, and folding those into memory is the very
    #: thing the gate exists to prevent.
    unaddressed_count: int = 0
    last_unaddressed: Optional[str] = None
    last_user_turn_at: float = 0.0
    last_assistant_turn_at: float = 0.0
    conversation_started_at: float = field(default_factory=time.time)

    references: References = field(default_factory=References)
    environment: EnvironmentState = field(default_factory=EnvironmentState)

    #: Per-site login confirmations, scoped to *this* session rather than the
    #: process-global dict the old VoiceEngine used.
    login_confirmed_sites: Dict[str, bool] = field(default_factory=dict)

    language: str = "english"

    # ── mutators ──────────────────────────────────────────────────────────
    def note_unaddressed(self, transcript: str) -> None:
        """Record speech the wake-word gate rejected. Deliberately not a turn."""
        self.unaddressed_count += 1
        self.last_unaddressed = (transcript or "").strip()[:200] or None

    def note_user_turn(self, *, intent: Optional[str] = None, topic: Optional[str] = None) -> None:
        self.turn_count += 1
        self.last_user_turn_at = time.time()
        self.current_speaker = Speaker.USER
        if intent:
            self.user_intent = intent
        if topic:
            self.current_topic = topic

    def note_assistant_turn(self) -> None:
        self.last_assistant_turn_at = time.time()
        self.current_speaker = Speaker.ASSISTANT

    def ask(self, question: str) -> None:
        self.pending_question = question

    def answered(self) -> None:
        self.pending_question = None

    def request_confirmation(self, action: Dict[str, Any]) -> None:
        self.pending_confirmation = action
        self.references.pending_action = action

    def clear_confirmation(self) -> None:
        self.pending_confirmation = None
        self.references.pending_action = None

    def note_interruption(self) -> None:
        self.interruption_count += 1

    def confirm_login(self, site: str) -> None:
        self.login_confirmed_sites[site] = True

    def is_login_confirmed(self, site: str) -> bool:
        return self.login_confirmed_sites.get(site, False)

    # ── queries ───────────────────────────────────────────────────────────
    @property
    def is_awaiting_user(self) -> bool:
        return bool(self.pending_question or self.pending_confirmation)

    @property
    def speaks_only_when_necessary(self) -> bool:
        return self.mode is ConversationMode.QUIET_MODE

    @property
    def may_act_without_asking(self) -> bool:
        return self.mode in (ConversationMode.AUTONOMOUS_MODE, ConversationMode.TASK_MODE)

    @property
    def needs_wake_word(self) -> bool:
        """§33 — a wake word is only required before a session is warm.

        `turn_count == 0` used to force it, which meant the first utterance of
        every session was discarded: the user pressed the mic, spoke, and heard
        nothing back. That is the opposite of what §33 calls an *optional* wake
        word, and it is indistinguishable from a broken assistant.

        A session the user deliberately opened is directed speech by
        construction — the mic press is the addressing. What the wake word
        genuinely protects against is an *ambient* mic picking up a conversation
        that was never meant for us, so NORMAL mode still requires it, and VOICE
        mode re-arms it once the conversation has gone cold enough that the next
        sound in the room is more likely to be someone else.
        """
        if self.mode in (ConversationMode.HANDS_FREE, ConversationMode.AUTONOMOUS_MODE):
            return False
        if self.mode is ConversationMode.NORMAL:
            return True
        # VOICE mode: warm from the moment the session opens, until it goes idle.
        if self.turn_count == 0:
            return False
        idle_s = time.time() - max(self.last_user_turn_at, self.last_assistant_turn_at)
        return idle_s > 120.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": self.mode.value,
            "current_speaker": self.current_speaker.value,
            "current_topic": self.current_topic,
            "user_intent": self.user_intent,
            "user_emotion_signal": self.user_emotion_signal,
            "urgency": self.urgency,
            "pending_question": self.pending_question,
            "pending_confirmation": self.pending_confirmation,
            "interruption_count": self.interruption_count,
            "turn_count": self.turn_count,
            "language": self.language,
            "needs_wake_word": self.needs_wake_word,
            "references": self.references.as_dict(),
            "environment": self.environment.as_dict(),
            "login_confirmed_sites": dict(self.login_confirmed_sites),
        }
