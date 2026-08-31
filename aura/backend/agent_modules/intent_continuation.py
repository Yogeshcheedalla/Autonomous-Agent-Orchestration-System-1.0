"""
Task Continuation Classifier Module for Akansha AI OS.
Analyzes incoming user messages during active multi-turn execution to determine
whether a message is a continuation of the current task or a new independent task.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logger = logging.getLogger(__name__)


@dataclass
class ContinuationClassification:
    is_continuation: bool
    confidence: float
    reason: str
    target_action: str  # continuation, new_task, barge_in_control, confirmation


class TaskContinuationClassifier:
    """
    Task Continuation Classifier.
    Evaluates input messages mid-workflow to seamlessly merge context updates,
    confirmations, and parameters into ongoing continuous sessions or spawn new isolated task tabs.
    """

    def classify_intent(self, prompt: str, is_active_task_running: bool = False) -> ContinuationClassification:
        """
        Classifies incoming prompt as continuation of active task or new independent task.
        """
        if not is_active_task_running:
            return ContinuationClassification(
                is_continuation=False,
                confidence=1.0,
                reason="No active task running",
                target_action="new_task",
            )

        lowered = prompt.strip().lower()

        # 1. Affirmations / Session confirmations
        if re.search(r"^(yes|yeah|yep|haan|sure|correct|it is logged in|logged in|already logged in|open it|go ahead|do it|proceed|continue)$", lowered):
            return ContinuationClassification(
                is_continuation=True,
                confidence=0.98,
                reason="User affirmative confirmation for active task session",
                target_action="confirmation",
            )

        # 2. Barge-in controls
        if any(kw in lowered for kw in ["pause", "resume", "stop", "cancel", "hold on", "wait", "aagu", "ruko"]):
            return ContinuationClassification(
                is_continuation=True,
                confidence=0.99,
                reason="Voice barge-in execution control signal",
                target_action="barge_in_control",
            )

        # 3. Follow-up parameter additions / references ("also do...", "use my email...", "next step...")
        if re.search(r"\b(also|then|after that|now|next|instead|use|with|and then|add|change|submit|fill)\b", lowered):
            return ContinuationClassification(
                is_continuation=True,
                confidence=0.90,
                reason="Contextual follow-up parameter or subtask modifier",
                target_action="continuation",
            )

        # 4. Detect distinct new task intent keywords ("schedule a reminder", "open notepad", "what is")
        if re.search(r"\b(schedule|remind|open app|calculator|notepad|what is|tell me|weather|who is)\b", lowered):
            return ContinuationClassification(
                is_continuation=False,
                confidence=0.88,
                reason="Independent new task intent detected",
                target_action="new_task",
            )

        # Default fallback during active task is continuation
        return ContinuationClassification(
            is_continuation=True,
            confidence=0.75,
            reason="Implicit context continuation during active session",
            target_action="continuation",
        )

    def system_prompt_section(self) -> str:
        return """
# MODULE: TASK CONTINUATION CLASSIFIER
- DYNAMIC DISAMBIGUATION: Distinguishes between ongoing task continuations/confirmations and new independent task requests.
- SEAMLESS CONTEXT MERGING: Merges user subtask updates ("also click submit", "use my email") into active continuous loops without resetting turn state.
- ISOLATED DISPATCH: Safely routes brand new task prompts to isolated session tabs without disrupting active work.
"""
