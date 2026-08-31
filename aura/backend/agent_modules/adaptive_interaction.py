from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PaceProfile:
    cadence: str  # "fast_concise" or "detailed_thorough"
    max_output_words: int
    include_bullet_points: bool
    speak_speed: float  # 1.0 = normal, 1.25 = fast


class AdaptiveInteractionEngine:
    """
    Module: Adaptive Interaction Pace Engine
    Objectives:
    - Analyze user input length, speed, and cadence
    - Adapt response style dynamically:
      - Short/Fast user input -> ultra-concise, direct response style
      - Long/Detailed user input -> thorough, explanatory response style
    - Provide immediate interruption handling to halt active responses when new input arrives
    """

    def __init__(self) -> None:
        self.last_input_time: float = time.time()
        self.is_interrupted: bool = False
        self.interruption_callbacks: List[Callable[[], None]] = []

    def evaluate_input_pace(self, user_input: str) -> PaceProfile:
        now = time.time()
        time_since_last = now - self.last_input_time
        self.last_input_time = now

        words = user_input.strip().split()
        word_count = len(words)

        # Reset interruption flag on new input
        self.is_interrupted = False

        if word_count > 6:
            return PaceProfile(
                cadence="detailed_thorough",
                max_output_words=250,
                include_bullet_points=True,
                speak_speed=1.0,
            )
        else:
            return PaceProfile(
                cadence="fast_concise",
                max_output_words=40,
                include_bullet_points=False,
                speak_speed=1.2,
            )

    def trigger_interruption(self, reason: str = "New user instruction received") -> Dict[str, Any]:
        """
        Immediately halt active response generation or running subtask upon receiving interruption.
        """
        self.is_interrupted = True
        for cb in self.interruption_callbacks:
            try:
                cb()
            except Exception:
                pass

        return {
            "interrupted": True,
            "reason": reason,
            "timestamp": time.time(),
            "status": "stopped",
        }

    def register_interruption_callback(self, callback: Callable[[], None]) -> None:
        self.interruption_callbacks.append(callback)
