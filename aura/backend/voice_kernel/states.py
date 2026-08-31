"""
voice_kernel.states — the voice state machine (§3).
==================================================

The 15 states from the spec, plus an explicit legality table. Illegal
transitions are *not* silently ignored: they raise, because a silent illegal
transition is exactly how "the assistant got stuck listening forever" bugs
happen. Callers that expect a transition may not be legal should use
`try_transition`.

The table below is written as `state -> {event: next_state}` so that reading
one row tells you everything that can happen from that state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .events import EventKind


class VoiceState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    SPEECH_DETECTED = "SPEECH_DETECTED"
    TRANSCRIBING = "TRANSCRIBING"
    UNDERSTANDING = "UNDERSTANDING"
    THINKING = "THINKING"
    RESPONDING = "RESPONDING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    EXECUTING = "EXECUTING"
    WAITING = "WAITING"
    CONFIRMING = "CONFIRMING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class IllegalTransition(RuntimeError):
    """Raised when an event cannot be applied to the current state."""


#: Sentinel returned by `peek` when no transition exists.
ILLEGAL = object()


# Events that are legal from *every* state. These are the safety valves:
# a stop must always land, an error must always be representable, and a
# session must always be closable.
_UNIVERSAL: Dict[EventKind, VoiceState] = {
    EventKind.USER_STOP: VoiceState.STOPPED,
    EventKind.SESSION_CLOSE: VoiceState.STOPPED,
    EventKind.ERROR: VoiceState.ERROR,
    EventKind.STT_ERROR: VoiceState.ERROR,
}

# Events that never change the state but are legal everywhere. Treating these
# as legal-but-inert keeps the transport from having to know which states care
# about, say, an environment refresh.
_INERT: Tuple[EventKind, ...] = (
    EventKind.ENVIRONMENT_UPDATE,
    EventKind.SPEECH_FRAME,
    EventKind.SILENCE_TICK,
    EventKind.RESPONSE_TOKEN,
)


_TABLE: Dict[VoiceState, Dict[EventKind, VoiceState]] = {
    VoiceState.IDLE: {
        EventKind.SESSION_OPEN: VoiceState.LISTENING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.USER_RESUME: VoiceState.LISTENING,
    },
    VoiceState.LISTENING: {
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.TASK_STARTED: VoiceState.EXECUTING,
        EventKind.STEP_STARTED: VoiceState.EXECUTING,
        EventKind.USER_PAUSE: VoiceState.PAUSED,
        EventKind.SESSION_OPEN: VoiceState.LISTENING,
    },
    VoiceState.SPEECH_DETECTED: {
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        # VAD fired but recognition produced nothing — go back to listening
        # rather than stranding the session in SPEECH_DETECTED.
        EventKind.SPEECH_END: VoiceState.LISTENING,
    },
    VoiceState.TRANSCRIBING: {
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.SPEECH_END: VoiceState.TRANSCRIBING,
        EventKind.SPEECH_START: VoiceState.TRANSCRIBING,
    },
    VoiceState.UNDERSTANDING: {
        # Endpointing said the turn is not actually over (§6).
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
        EventKind.SPEECH_START: VoiceState.TRANSCRIBING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.RESPONSE_TOKEN: VoiceState.RESPONDING,
        EventKind.TASK_STARTED: VoiceState.EXECUTING,
        EventKind.USER_CONFIRM: VoiceState.EXECUTING,
        EventKind.USER_REJECT: VoiceState.LISTENING,
    },
    VoiceState.THINKING: {
        EventKind.RESPONSE_TOKEN: VoiceState.RESPONDING,
        EventKind.RESPONSE_COMPLETE: VoiceState.RESPONDING,
        EventKind.TASK_STARTED: VoiceState.EXECUTING,
        EventKind.SPEECH_START: VoiceState.INTERRUPTED,
    },
    VoiceState.RESPONDING: {
        EventKind.TTS_STARTED: VoiceState.SPEAKING,
        EventKind.RESPONSE_COMPLETE: VoiceState.RESPONDING,
        EventKind.SPEECH_START: VoiceState.INTERRUPTED,
        EventKind.TASK_STARTED: VoiceState.EXECUTING,
        # Text-only reply with TTS muted (QUIET_MODE) never enters SPEAKING.
        EventKind.TTS_FINISHED: VoiceState.LISTENING,
    },
    VoiceState.SPEAKING: {
        # True barge-in (§4): user speech while we hold the floor.
        EventKind.SPEECH_START: VoiceState.INTERRUPTED,
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.INTERRUPTED,
        EventKind.TTS_FINISHED: VoiceState.LISTENING,
        EventKind.RESPONSE_TOKEN: VoiceState.SPEAKING,
        EventKind.RESPONSE_COMPLETE: VoiceState.SPEAKING,
        EventKind.TASK_STARTED: VoiceState.EXECUTING,
    },
    VoiceState.INTERRUPTED: {
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.TTS_FINISHED: VoiceState.LISTENING,
        # Barge-in turned out to be an echo or a cough.
        EventKind.SPEECH_END: VoiceState.LISTENING,
        EventKind.USER_PAUSE: VoiceState.PAUSED,
    },
    VoiceState.EXECUTING: {
        # The conversation channel stays open during execution (§14).
        EventKind.SPEECH_START: VoiceState.EXECUTING,
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.EXECUTING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.STEP_STARTED: VoiceState.EXECUTING,
        EventKind.STEP_COMPLETED: VoiceState.EXECUTING,
        EventKind.STEP_FAILED: VoiceState.EXECUTING,
        EventKind.TASK_COMPLETED: VoiceState.LISTENING,
        EventKind.TASK_FAILED: VoiceState.LISTENING,
        EventKind.USER_PAUSE: VoiceState.PAUSED,
        EventKind.RESPONSE_TOKEN: VoiceState.EXECUTING,
        EventKind.TTS_STARTED: VoiceState.EXECUTING,
        EventKind.TTS_FINISHED: VoiceState.EXECUTING,
    },
    VoiceState.WAITING: {
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.STEP_COMPLETED: VoiceState.EXECUTING,
        EventKind.TASK_COMPLETED: VoiceState.LISTENING,
        EventKind.USER_RESUME: VoiceState.EXECUTING,
    },
    VoiceState.CONFIRMING: {
        EventKind.USER_CONFIRM: VoiceState.EXECUTING,
        EventKind.USER_REJECT: VoiceState.LISTENING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.TTS_FINISHED: VoiceState.CONFIRMING,
        EventKind.TTS_STARTED: VoiceState.CONFIRMING,
    },
    VoiceState.PAUSED: {
        EventKind.USER_RESUME: VoiceState.EXECUTING,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.PARTIAL_TRANSCRIPT: VoiceState.TRANSCRIBING,
    },
    VoiceState.STOPPED: {
        EventKind.SESSION_OPEN: VoiceState.LISTENING,
        EventKind.USER_RESUME: VoiceState.LISTENING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
    },
    VoiceState.ERROR: {
        EventKind.SESSION_OPEN: VoiceState.LISTENING,
        EventKind.USER_RESUME: VoiceState.LISTENING,
        EventKind.SPEECH_START: VoiceState.SPEECH_DETECTED,
        EventKind.FINAL_TRANSCRIPT: VoiceState.UNDERSTANDING,
    },
}


@dataclass(slots=True)
class TransitionRecord:
    frm: VoiceState
    to: VoiceState
    event: EventKind
    at: float = field(default_factory=time.monotonic)


class VoiceStateMachine:
    """Event-driven FSM with a bounded audit trail."""

    #: How many transitions to keep for debugging / the UI timeline.
    HISTORY_LIMIT = 200

    def __init__(self, initial: VoiceState = VoiceState.IDLE) -> None:
        self._state = initial
        self.history: List[TransitionRecord] = []
        self.entered_at: float = time.monotonic()

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def time_in_state(self) -> float:
        return time.monotonic() - self.entered_at

    def peek(self, event: EventKind):
        """Return the state `event` would move us to, or `ILLEGAL`.

        `None` means "legal but no state change" (an inert event).

        Precedence matters, and getting it wrong is not a subtle bug. The
        universal safety valves win first — a stop must land from anywhere.
        An explicit `_TABLE` row wins next, *before* `_INERT`: RESPONSE_TOKEN is
        inert in most states but promotes UNDERSTANDING/THINKING to RESPONDING,
        and checking `_INERT` first silently shadowed those rows, so the session
        never took the floor and barge-in (§4) could never fire.
        """
        if event in _UNIVERSAL:
            return _UNIVERSAL[event]
        row = _TABLE.get(self._state, {})
        if event in row:
            return row[event]
        if event in _INERT:
            return None
        return ILLEGAL

    def can(self, event: EventKind) -> bool:
        return self.peek(event) is not ILLEGAL

    def transition(self, event: EventKind) -> Optional[VoiceState]:
        """Apply `event`. Returns the new state, or None if inert.

        Raises IllegalTransition if the event has no meaning here.
        """
        target = self.peek(event)
        if target is ILLEGAL:
            raise IllegalTransition(
                f"event {event.value!r} is not legal in state {self._state.value}"
            )
        if target is None or target == self._state:
            return None
        self.history.append(TransitionRecord(self._state, target, event))
        if len(self.history) > self.HISTORY_LIMIT:
            del self.history[: len(self.history) - self.HISTORY_LIMIT]
        self._state = target  # type: ignore[assignment]
        self.entered_at = time.monotonic()
        return self._state

    def try_transition(self, event: EventKind) -> Optional[VoiceState]:
        """Like `transition` but returns None instead of raising on illegal input."""
        try:
            return self.transition(event)
        except IllegalTransition:
            return None

    def force(self, state: VoiceState, event: EventKind = EventKind.ERROR) -> None:
        """Escape hatch for recovery paths. Records the jump."""
        if state == self._state:
            return
        self.history.append(TransitionRecord(self._state, state, event))
        self._state = state
        self.entered_at = time.monotonic()

    # ── convenience predicates used by the policy layer ────────────────────
    @property
    def holds_floor(self) -> bool:
        """True when Akansha is the one making sound."""
        return self._state in (VoiceState.RESPONDING, VoiceState.SPEAKING)

    @property
    def is_busy_executing(self) -> bool:
        return self._state in (VoiceState.EXECUTING, VoiceState.WAITING)

    @property
    def is_user_turn(self) -> bool:
        return self._state in (
            VoiceState.LISTENING,
            VoiceState.SPEECH_DETECTED,
            VoiceState.TRANSCRIBING,
        )
