"""
voice_kernel.events — the wire vocabulary between transport and kernel.
=======================================================================

Two directions:

  Event      transport → kernel.  Something happened in the world.
  Directive  kernel → transport.  Do this now.

Keeping these as plain dataclasses (rather than, say, passing raw dicts)
means the state machine can never be driven by a typo, and the WebSocket
layer has exactly one place to serialise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventKind(str, Enum):
    """Everything the outside world can tell the kernel."""

    # ── audio / VAD (§5) ────────────────────────────────────────────────
    SESSION_OPEN = "session_open"
    SESSION_CLOSE = "session_close"
    SPEECH_START = "speech_start"          # VAD rising edge
    SPEECH_FRAME = "speech_frame"          # ongoing energy report
    SPEECH_END = "speech_end"              # VAD falling edge
    SILENCE_TICK = "silence_tick"          # periodic while no speech

    # ── recognition (§6) ────────────────────────────────────────────────
    PARTIAL_TRANSCRIPT = "partial_transcript"
    FINAL_TRANSCRIPT = "final_transcript"
    STT_ERROR = "stt_error"

    # ── generation / playback (§19) ─────────────────────────────────────
    RESPONSE_TOKEN = "response_token"
    RESPONSE_COMPLETE = "response_complete"
    TTS_STARTED = "tts_started"
    TTS_FINISHED = "tts_finished"

    # ── task execution (§14, §15) ───────────────────────────────────────
    TASK_STARTED = "task_started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    # ── explicit user control (§4, §20) ─────────────────────────────────
    USER_STOP = "user_stop"
    USER_PAUSE = "user_pause"
    USER_RESUME = "user_resume"
    USER_CONFIRM = "user_confirm"
    USER_REJECT = "user_reject"

    # ── environment (§21) ───────────────────────────────────────────────
    ENVIRONMENT_UPDATE = "environment_update"

    ERROR = "error"


class DirectiveKind(str, Enum):
    """Everything the kernel can ask the transport to do."""

    START_LISTENING = "start_listening"
    STOP_LISTENING = "stop_listening"
    START_TRANSCRIBING = "start_transcribing"
    FINALIZE_TURN = "finalize_turn"        # carries the settled transcript
    KEEP_LISTENING = "keep_listening"      # turn not complete, do not submit
    GENERATE_RESPONSE = "generate_response"
    SPEAK = "speak"                        # carries text + priority
    STOP_TTS = "stop_tts"                  # barge-in (§4)
    EXECUTE_PLAN = "execute_plan"
    CANCEL_EXECUTION = "cancel_execution"
    PAUSE_EXECUTION = "pause_execution"
    RESUME_EXECUTION = "resume_execution"
    ASK_CLARIFICATION = "ask_clarification"
    REQUEST_CONFIRMATION = "request_confirmation"
    STATE_CHANGED = "state_changed"        # for the 3D core (§24)
    COMPRESS_CONTEXT = "compress_context"  # (§12)
    SITUATION_CHANGED = "situation_changed"
    NOOP = "noop"


@dataclass(slots=True)
class Event:
    kind: EventKind
    payload: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


@dataclass(slots=True)
class Directive:
    kind: DirectiveKind
    payload: Dict[str, Any] = field(default_factory=dict)
    #: Only set on SPEAK. Lower number = more urgent. See speech_policy.Priority.
    priority: Optional[int] = None
    at: float = field(default_factory=time.monotonic)

    def as_dict(self) -> Dict[str, Any]:
        """Wire form. `at` is deliberately omitted — it is monotonic, not wall clock."""
        out: Dict[str, Any] = {"directive": self.kind.value, **self.payload}
        if self.priority is not None:
            out["priority"] = self.priority
        return out


def noop() -> Directive:
    return Directive(DirectiveKind.NOOP)
