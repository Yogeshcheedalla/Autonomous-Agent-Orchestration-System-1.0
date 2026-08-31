"""
VoiceFeedbackLoop — Voice-First Feedback Loop for Akansha AI OS.
- Generates StatusNarration for each subtask lifecycle event
- Filters AmbientNoise via WakeConfidence threshold (0.70)
- Forwards confirmed BargeIn to ConversationalOrchestrator within 150ms
- Manages narration queue with barge-in interruption
- Supports Telugu-English code-switching
"""
from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .continuous_jarvis_engine import ContinuousSubtaskNode
from .user_understanding import UserUnderstandingModule

logger = logging.getLogger(__name__)

WAKE_CONFIDENCE_THRESHOLD = 0.70
BARGE_IN_FORWARD_TIMEOUT_MS = 150
LOW_LATENCY_DURATION_THRESHOLD_S = 2.0
BACKPRESSURE_QUEUE_LIMIT = 50


class VFLState(str, Enum):
    IDLE = "idle"
    NARRATING = "narrating"
    LISTENING = "listening"   # awaiting checkpoint response


@dataclass
class StatusNarration:
    text: str
    session_id: str
    narration_type: str      # "progress", "completion", "warning", "acknowledgment", "final"
    priority: int = 0        # higher priority interrupts lower
    created_at: float = field(default_factory=time.time)


@dataclass
class AudioEvent:
    wake_confidence: float
    utterance: str
    session_id: str
    captured_at: float = field(default_factory=time.time)


class VoiceFeedbackLoop:
    """
    Voice-First Feedback Loop.
    - Generates StatusNarration for each subtask lifecycle event
    - Filters AmbientNoise via WakeConfidence threshold (0.70)
    - Forwards confirmed BargeIn to ConversationalOrchestrator within 150ms
    - Manages narration queue with barge-in interruption
    - Supports Telugu-English code-switching
    """

    def __init__(
        self,
        user_understanding: Optional[UserUnderstandingModule] = None,
        event_emitter: Optional[Callable[[str, Dict], None]] = None,
        orchestrator_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.user_understanding = user_understanding or UserUnderstandingModule()
        self._emit = event_emitter or (lambda e, d: None)
        self._orchestrator_callback = orchestrator_callback  # (session_id, utterance)
        self._queue: asyncio.Queue = asyncio.Queue()
        self.state = VFLState.IDLE
        self._current_narration: Optional[StatusNarration] = None

    # ── Narration generation ──────────────────────────────────────────────────

    def generate_completion_narration(
        self, step: ContinuousSubtaskNode, bubble: "SessionContextBubble"  # type: ignore[name-defined]
    ) -> StatusNarration:
        """Generate ≤ 15 word completion narration from voice_update_prompt."""
        raw = step.voice_update_prompt or f"Completed: {step.goal_description}"
        try:
            adapted = self.user_understanding.adapt_response_style(raw, mode="voice")
        except Exception:
            adapted = raw
        text = self._enforce_word_limit(adapted, max_words=15)
        if bubble.language_preference == "telugu_english":
            text = self._apply_telugu_english(text, tense="completed")
        return StatusNarration(text=text, session_id=step.params.get("session_id", ""),
                               narration_type="completion")

    def generate_progress_narration(
        self, step: ContinuousSubtaskNode, bubble: "SessionContextBubble"  # type: ignore[name-defined]
    ) -> StatusNarration:
        """Generate ≤ 12 word in-progress narration from goal_description."""
        raw = f"Starting: {step.goal_description}"
        try:
            adapted = self.user_understanding.adapt_response_style(raw, mode="voice")
        except Exception:
            adapted = raw
        text = self._enforce_word_limit(adapted, max_words=12)
        if bubble.language_preference == "telugu_english":
            text = self._apply_telugu_english(text, tense="in_progress")
        return StatusNarration(text=text, session_id=step.params.get("session_id", ""),
                               narration_type="progress")

    def generate_warning_narration(self, session_id: str) -> StatusNarration:
        return StatusNarration(
            text="Adapting plan, one moment.",
            session_id=session_id,
            narration_type="warning",
            priority=10,
        )

    def generate_final_narration(
        self, session_id: str, main_goal: str, total_steps: int
    ) -> StatusNarration:
        text = f"All {total_steps} steps complete. {main_goal[:30]} finished."
        return StatusNarration(text=text, session_id=session_id, narration_type="final")

    def generate_acknowledgment_narration(
        self, action: str, session_id: str
    ) -> StatusNarration:
        if "pause" in action:
            text = "Pausing now."
        elif "resume" in action:
            text = "Resuming."
        elif "cancel" in action:
            text = "Cancelling task."
        else:
            text = "Got it."
        return StatusNarration(text=text, session_id=session_id, narration_type="acknowledgment")

    def should_suppress_narration(self, step: ContinuousSubtaskNode) -> bool:
        """Suppress narration for routine low-latency sub-steps."""
        has_prompt = bool(step.voice_update_prompt)
        risk = step.params.get("_estimated_risk", 0.5)
        return (not has_prompt) and (risk < 0.1)

    # ── Queue management ──────────────────────────────────────────────────────

    async def enqueue(self, narration: StatusNarration) -> None:
        # Backpressure: drop oldest non-warning/non-final when queue too deep
        if self._queue.qsize() >= BACKPRESSURE_QUEUE_LIMIT:
            # Drain into a list, filter, and re-fill
            items: List[StatusNarration] = []
            while not self._queue.empty():
                try:
                    items.append(self._queue.get_nowait())
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
            # Keep warning and final, drop others if still over limit
            kept = [i for i in items if i.narration_type in ("warning", "final")]
            dropped = [i for i in items if i.narration_type not in ("warning", "final")]
            # Keep as many non-critical as possible within limit
            remaining_capacity = BACKPRESSURE_QUEUE_LIMIT - len(kept) - 1  # -1 for the new one
            kept.extend(dropped[-remaining_capacity:] if remaining_capacity > 0 else [])
            for item in kept:
                await self._queue.put(item)
        await self._queue.put(narration)

    async def run_narration_loop(self) -> None:
        """Consume narration queue in order without overlapping playback."""
        while True:
            narration = await self._queue.get()
            if self.state == VFLState.LISTENING:
                # Suppress during checkpoint listening state
                self._queue.task_done()
                continue
            self.state = VFLState.NARRATING
            self._current_narration = narration
            await self._play_narration(narration)
            self._current_narration = None
            self.state = VFLState.IDLE
            self._queue.task_done()

    async def _play_narration(self, narration: StatusNarration) -> None:
        """Simulate TTS playback. Replace with actual TTS call in production."""
        logger.info("[VFL] %s: %s", narration.narration_type.upper(), narration.text)
        await asyncio.sleep(0.05)   # placeholder for TTS latency

    # ── Barge-in detection ────────────────────────────────────────────────────

    async def handle_audio_event(self, event: AudioEvent) -> None:
        """
        Entry point for all audio events from the voice pipeline.
        Filters by WakeConfidence. Confirmed BargeIn forwarded within 150ms.
        """
        if event.wake_confidence < WAKE_CONFIDENCE_THRESHOLD:
            return  # discard as AmbientNoise

        # BargeIn confirmed
        self._emit("barge_in_confirmed", {
            "session_id": event.session_id,
            "utterance": event.utterance,
            "wake_confidence": event.wake_confidence,
        })

        # Stop current narration
        if self.state == VFLState.NARRATING:
            self._current_narration = None
            self.state = VFLState.IDLE

        # Forward to orchestrator within 150ms
        forward_start = time.perf_counter()
        if self._orchestrator_callback:
            self._orchestrator_callback(event.session_id, event.utterance)
        elapsed_ms = (time.perf_counter() - forward_start) * 1000
        if elapsed_ms > BARGE_IN_FORWARD_TIMEOUT_MS:
            logger.warning("VoiceFeedbackLoop: BargeIn forward took %.1fms (target 150ms)", elapsed_ms)

    def enter_listening_state(self) -> None:
        """Suppress narrations; enter checkpoint listening mode."""
        self.state = VFLState.LISTENING

    def exit_listening_state(self) -> None:
        self.state = VFLState.IDLE

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _enforce_word_limit(text: str, max_words: int) -> str:
        words = text.split()
        return " ".join(words[:max_words])

    @staticmethod
    def _apply_telugu_english(text: str, tense: str) -> str:
        if tense == "completed":
            result = text.replace("Completed", "Complete ayindi").replace("Done", "Ayyindi")
            if not result.endswith("."):
                result += "."
            return result
        elif tense == "in_progress":
            result = text.replace("Starting", "Start avutundi")
            if not result.endswith("."):
                result += "."
            return result
        return text
