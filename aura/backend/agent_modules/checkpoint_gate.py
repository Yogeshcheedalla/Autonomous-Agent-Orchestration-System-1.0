"""
CheckpointGate — Pre-execution pause system for high-stakes steps.
Inserts CheckpointGate entries during plan construction and manages the
confirmation/timeout lifecycle at execution time.

Integrates with ReasoningEngine for risk scoring and SessionContextBubble
for context-aware question generation and skip logic.
"""
from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from .continuous_jarvis_engine import ContinuousJarvisSession, ContinuousSubtaskNode
from .reasoning_engine import ReasoningEngine
from .session_context_bubble import SessionContextBubble, CheckpointRecord

logger = logging.getLogger(__name__)

HIGH_STAKE_THRESHOLD = 0.7   # irreversibility score
LOW_STAKE_THRESHOLD  = 0.3   # routine step threshold
CHECKPOINT_TIMEOUT_SECONDS = 60


@dataclass
class CheckpointQuestion:
    step_id: str
    session_id: str
    question_text: str
    action_type: str
    target_url: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class CheckpointGate:
    """
    Pre-execution pause system for high-stakes steps.
    Inserts CheckpointGate entries during plan construction and manages the
    confirmation/timeout lifecycle at execution time.

    Integrates with ReasoningEngine for risk scoring and SessionContextBubble
    for context-aware question generation and skip logic.
    """

    def __init__(
        self,
        reasoning_engine: Optional[ReasoningEngine] = None,
        event_emitter: Optional[Callable[[str, Dict], None]] = None,
    ) -> None:
        self.reasoning_engine = reasoning_engine or ReasoningEngine()
        self._emit = event_emitter or (lambda e, d: None)
        # Pending confirmations: step_id -> asyncio.Event
        self._pending: Dict[str, asyncio.Event] = {}

    def is_high_stake(self, step: ContinuousSubtaskNode) -> bool:
        """Return True if the step's risk score exceeds the high-stake threshold."""
        try:
            result = self.reasoning_engine.analyze_decision(
                goal=step.goal_description,
                available_info=step.params,
                critical_parameters=[],
                is_destructive_action=True,
            )
            # Map confidence inversely: low confidence in safety = high risk
            risk_score = 1.0 - result.confidence
        except Exception as exc:
            logger.warning("CheckpointGate: ReasoningEngine failed, defaulting risk=0.5: %s", exc)
            risk_score = 0.5
        step.params["_estimated_risk"] = risk_score
        return risk_score > HIGH_STAKE_THRESHOLD

    def annotate_plan_with_gates(
        self,
        session: ContinuousJarvisSession,
        bubble: SessionContextBubble,
    ) -> None:
        """
        Scan all pending steps in a session and tag high-stake ones
        with '_requires_checkpoint': True. Called after plan construction
        and after every ImprovRequest injection.
        """
        for step in session.subtasks:
            if step.status != "pending":
                continue
            if step.params.get("_requires_checkpoint") is not None:
                continue  # already evaluated
            if step.params.get("_estimated_risk", 0.0) < LOW_STAKE_THRESHOLD:
                step.params["_requires_checkpoint"] = False
                continue
            if self.is_high_stake(step):
                if self._should_skip(step, bubble):
                    step.params["_requires_checkpoint"] = False
                else:
                    step.params["_requires_checkpoint"] = True
            else:
                step.params["_requires_checkpoint"] = False

    def _should_skip(self, step: ContinuousSubtaskNode, bubble: SessionContextBubble) -> bool:
        """Skip checkpoint if it was already answered or login is confirmed."""
        if bubble.has_answered_checkpoint(step.id):
            return True
        if "login" in step.action.lower() and bubble.is_login_confirmed():
            return True
        return False

    def generate_question(
        self,
        step: ContinuousSubtaskNode,
        bubble: SessionContextBubble,
    ) -> CheckpointQuestion:
        """Generate exactly one contextually specific CheckpointQuestion per step."""
        action = step.action
        target_url = step.params.get("url", "")
        account = bubble.account_context.get("account", "the target platform")
        browser = bubble.account_context.get("browser", "your browser")

        if "login" in action or "auth" in action:
            text = f"Are you already logged into {account} on {browser}?"
        elif "submit" in action or "form" in action:
            text = f"Ready to submit? This action on {target_url} cannot be undone."
        elif "delete" in action or "remove" in action:
            text = f"This will permanently delete data on {target_url}. Shall I proceed?"
        elif "payment" in action or "pay" in action:
            text = f"About to initiate a payment action on {account}. Confirm to proceed."
        else:
            text = f"Next step '{step.goal_description[:50]}' is irreversible. Shall I proceed?"

        return CheckpointQuestion(
            step_id=step.id,
            session_id=step.params.get("session_id", ""),
            question_text=text,
            action_type=action,
            target_url=target_url or None,
        )

    async def await_confirmation(
        self,
        step: ContinuousSubtaskNode,
        session: ContinuousJarvisSession,
        bubble: SessionContextBubble,
    ) -> bool:
        """
        Block execution until user confirms, re-asking once after timeout.
        Returns True if confirmed, False if still timed out after re-ask.
        """
        evt = asyncio.Event()
        self._pending[step.id] = evt
        session.status = "awaiting_checkpoint"

        try:
            await asyncio.wait_for(evt.wait(), timeout=CHECKPOINT_TIMEOUT_SECONDS)
            return True
        except asyncio.TimeoutError:
            # Re-ask once
            logger.info("CheckpointGate: timeout on step %s, re-asking", step.id)
            try:
                await asyncio.wait_for(evt.wait(), timeout=CHECKPOINT_TIMEOUT_SECONDS)
                return True
            except asyncio.TimeoutError:
                session.status = "paused"
                self._emit("checkpoint_timeout", {
                    "session_id": session.session_id,
                    "step_id": step.id,
                })
                return False
        finally:
            self._pending.pop(step.id, None)

    def confirm(self, step_id: str, response_text: str, bubble: SessionContextBubble) -> None:
        """Called by ConversationalOrchestrator when user confirmation is classified."""
        bubble.record_checkpoint(CheckpointRecord(
            step_id=step_id,
            question_text="",
            response=response_text,
            responded_at=time.time(),
        ))
        evt = self._pending.get(step_id)
        if evt:
            evt.set()
