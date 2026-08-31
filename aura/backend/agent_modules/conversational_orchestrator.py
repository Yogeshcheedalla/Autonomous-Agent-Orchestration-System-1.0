"""
ConversationalOrchestrator — Unified routing brain for all user messages.
Routes to one of five mutually exclusive actions within 800ms.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .intent_continuation import TaskContinuationClassifier, ContinuationClassification
from .reasoning_engine import ReasoningEngine
from .three_lanes import ThreeLaneExecutionEngine
from .continuous_jarvis_engine import ContinuousVoiceJarvisEngine
from .improvisation_engine import ImprovisationEngine, ImprovRequest
from .task_fork_manager import TaskForkManager
from .checkpoint_gate import CheckpointGate
from .voice_feedback_loop import VoiceFeedbackLoop
from .session_context_bubble import SessionContextBubble
from .observability import ObservabilityTracer
from ..voice_kernel.intent import IntentCategory, IntentClassifier

logger = logging.getLogger(__name__)

ORCHESTRATION_TIMEOUT_MS = 800
MIN_RESOLVED_CONFIDENCE = 0.80
LOW_CONFIDENCE_THRESHOLD = 0.75
NEW_TASK_CONFIDENCE_THRESHOLD = 0.85
CONTINUATION_CONFIDENCE_THRESHOLD = 0.80

#: Questions *about the running task* (§14). These are answered from session
#: state — no model call, no plan mutation. `TaskContinuationClassifier` funnels
#: everything it does not recognise into "continuation" at 0.75, which routed
#: "what are you doing?" to the ImprovisationEngine: asking a question rewrote
#: the plan. That is the precise failure §14 exists to forbid.
_TASK_STATUS_Q = re.compile(
    r"\b(what(?:'s| is| are)?\s+(?:you|u|ur|your)?\s*(?:doing|up to|working on|status|progress)"
    r"|what\s+step|which\s+step|how\s+(?:far|much longer|long)"
    r"|are\s+you\s+(?:done|finished|nearly done|almost done)"
    r"|status|progress\s+update|where\s+are\s+(?:you|we))\b",
    re.IGNORECASE,
)



class RoutingAction(str, Enum):
    IMPROVISE_CURRENT_TASK      = "improvise_current_task"
    FORK_NEW_TASK               = "fork_new_task"
    ANSWER_CONVERSATIONAL_QUERY = "answer_conversational_query"
    EXECUTE_BARGE_IN_CONTROL    = "execute_barge_in_control"
    ASK_CHECKPOINT_CONFIRMATION = "ask_checkpoint_confirmation"


@dataclass
class RoutingDecision:
    action: RoutingAction
    confidence: float
    target_session_id: Optional[str]
    latency_ms: float
    user_message: str
    #: True when the action was deliberately not carried out. §7: a backchannel
    #: ("yeah", "mm-hmm") is conversational glue — it must not become a task, and
    #: it must not become a *confirmation* of one either.
    suppressed: bool = False
    #: Why the routing came out the way it did, for the events panel.
    reason: str = ""
    #: A ready-to-speak answer produced without touching the plan or a model
    #: (§14: talk while executing).
    spoken_answer: Optional[str] = None


def task_status_answer(session: "ContinuousJarvisSession") -> str:  # type: ignore[name-defined]
    """One line describing where a running task actually is.

    Cheap on purpose: §14 says a status question must be answerable *while*
    executing, and routing a model call through the middle of live automation
    is both slow and a chance to get the plan wrong.
    """
    total = len(session.subtasks)
    if session.status in ("completed",):
        return f"Finished: {session.main_goal}."
    if session.status == "cancelled":
        return f"Cancelled: {session.main_goal}."
    index = min(session.current_step_index, max(total - 1, 0))
    current = session.subtasks[index] if total else None
    where = f"step {min(session.current_step_index + 1, total)} of {total}"
    if session.status == "paused":
        return f"Paused at {where}: {current.goal_description if current else session.main_goal}."
    if current is None:
        return f"Working on {session.main_goal}."
    return f"{current.goal_description} — {where}."


@dataclass
class OrchestrationState:
    active_lane_ids: List[str] = field(default_factory=list)
    primary_session_id: Optional[str] = None
    awaiting_checkpoint: Dict[str, bool] = field(default_factory=dict)
    last_routing_timestamp: float = field(default_factory=time.time)


class IntentResolver:
    def __init__(self, reasoning_engine: ReasoningEngine) -> None:
        self.reasoning_engine = reasoning_engine

    def resolve(
        self,
        classification: ContinuationClassification,
        state: OrchestrationState,
        bubble: Optional[SessionContextBubble],
    ) -> tuple:
        confidence = classification.confidence
        if confidence < LOW_CONFIDENCE_THRESHOLD and state.primary_session_id:
            available_info = {}
            if bubble:
                available_info = {
                    "language_preference": bubble.language_preference,
                    "strategy_preference": bubble.strategy_preference,
                    "account_context": bubble.account_context,
                }
            try:
                result = self.reasoning_engine.analyze_decision(
                    goal=classification.reason,
                    available_info=available_info,
                    critical_parameters=[],
                )
                confidence = max(result.confidence, MIN_RESOLVED_CONFIDENCE)
            except Exception as exc:
                logger.warning("IntentResolver: ReasoningEngine escalation failed: %s", exc)

        target_action = classification.target_action
        if target_action == "barge_in_control":
            return RoutingAction.EXECUTE_BARGE_IN_CONTROL, confidence
        if target_action == "confirmation":
            return RoutingAction.ASK_CHECKPOINT_CONFIRMATION, confidence
        if target_action == "continuation" and confidence >= CONTINUATION_CONFIDENCE_THRESHOLD:
            return RoutingAction.IMPROVISE_CURRENT_TASK, confidence
        if target_action == "new_task" and confidence >= NEW_TASK_CONFIDENCE_THRESHOLD:
            return RoutingAction.FORK_NEW_TASK, confidence
        if state.primary_session_id:
            return RoutingAction.IMPROVISE_CURRENT_TASK, confidence
        return RoutingAction.ANSWER_CONVERSATIONAL_QUERY, confidence


class ConversationalOrchestrator:
    """
    Single entry point for all user messages.
    Routes to one of five actions within 800ms.
    """

    def __init__(
        self,
        jarvis_engine: ContinuousVoiceJarvisEngine,
        improvisation_engine: ImprovisationEngine,
        fork_manager: TaskForkManager,
        checkpoint_gate: CheckpointGate,
        voice_feedback_loop: VoiceFeedbackLoop,
        three_lane_engine: ThreeLaneExecutionEngine,
        reasoning_engine: Optional[ReasoningEngine] = None,
        tracer: Optional[ObservabilityTracer] = None,
        event_emitter: Optional[Callable[[str, Dict], None]] = None,
    ) -> None:
        self.jarvis_engine = jarvis_engine
        self.improvisation_engine = improvisation_engine
        self.fork_manager = fork_manager
        self.checkpoint_gate = checkpoint_gate
        self.vfl = voice_feedback_loop
        self.three_lane = three_lane_engine
        self.reasoning_engine = reasoning_engine or ReasoningEngine()
        self.tracer = tracer or ObservabilityTracer()
        self._emit = event_emitter or (lambda e, d: None)

        self._classifier = TaskContinuationClassifier()
        #: The voice kernel's classifier, used here as a veto rather than a
        #: replacement. It is pure and sub-millisecond, and it is the one tested
        #: definition in the codebase of what counts as a backchannel or a
        #: question — duplicating those rules is how they drift apart.
        self._kernel = IntentClassifier()
        self._resolver = IntentResolver(self.reasoning_engine)
        self.state = OrchestrationState()
        self._bubbles: Dict[str, SessionContextBubble] = {}

    def route(self, message: str, source_session_id: Optional[str] = None) -> RoutingDecision:
        """Route a user message. Must return within 800ms at p95."""
        start = time.perf_counter()
        span = self.tracer.start_span("ConversationalOrchestrator", "route")

        is_active = bool(self.jarvis_engine.active_sessions)
        try:
            classification = self._classifier.classify_intent(
                message, is_active_task_running=is_active
            )
        except Exception as exc:
            logger.error("ConversationalOrchestrator: classifier failed: %s", exc)
            elapsed_ms = (time.perf_counter() - start) * 1000
            decision = RoutingDecision(
                action=RoutingAction.ANSWER_CONVERSATIONAL_QUERY,
                confidence=0.5,
                target_session_id=None,
                latency_ms=elapsed_ms,
                user_message=message,
            )
            self._emit("orchestration_decision", {
                "message": message,
                "action": decision.action,
                "confidence": decision.confidence,
                "session_id": None,
                "latency_ms": elapsed_ms,
            })
            return decision

        primary_sid = source_session_id or self.state.primary_session_id
        bubble = self._bubbles.get(primary_sid) if primary_sid else None
        action, confidence = self._resolver.resolve(classification, self.state, bubble)

        # Hard-override: no active session → always answer conversationally
        if not is_active and action in (
            RoutingAction.IMPROVISE_CURRENT_TASK, RoutingAction.FORK_NEW_TASK
        ):
            action = RoutingAction.ANSWER_CONVERSATIONAL_QUERY

        action, confidence, suppressed, reason, spoken = self._veto(
            action, confidence, message, primary_sid, is_active
        )

        if not suppressed and spoken is None:
            self._delegate(action, message, primary_sid, bubble)

        elapsed_ms = (time.perf_counter() - start) * 1000
        decision = RoutingDecision(
            action=action,
            confidence=confidence,
            target_session_id=primary_sid,
            latency_ms=elapsed_ms,
            user_message=message,
            suppressed=suppressed,
            reason=reason,
            spoken_answer=spoken,
        )
        self.state.last_routing_timestamp = time.time()
        self.tracer.end_span(span, cost_justified=True)

        self._emit("orchestration_decision", {
            "message": message,
            "action": action,
            "confidence": confidence,
            "session_id": primary_sid,
            "latency_ms": elapsed_ms,
            "suppressed": suppressed,
            "reason": reason,
            "spoken_answer": spoken,
        })
        if elapsed_ms > ORCHESTRATION_TIMEOUT_MS:
            logger.warning(
                "ConversationalOrchestrator: routing took %.1fms (target 800ms)", elapsed_ms
            )
        return decision

    def _veto(
        self,
        action: RoutingAction,
        confidence: float,
        message: str,
        session_id: Optional[str],
        is_active: bool,
    ) -> tuple:
        """Second opinion from the voice kernel, applied before anything runs.

        `TaskContinuationClassifier` has no "neither" bucket: anything it does not
        recognise becomes `continuation` at 0.75, and with a task running that
        resolves to IMPROVISE_CURRENT_TASK. So two whole classes of utterance
        were being treated as plan edits or confirmations:

        * §14 — "what are you doing?" mutated the plan instead of answering.
        * §7  — a bare "yeah" during a gated step became ASK_CHECKPOINT_CONFIRMATION
          and confirmed it. A grunt could approve a destructive action.

        Returns `(action, confidence, suppressed, reason, spoken_answer)`.
        """
        try:
            intent = self._kernel.classify(
                message,
                task_active=is_active,
                pending_confirmation=any(self.state.awaiting_checkpoint.values()),
            )
        except Exception as exc:
            logger.warning("Kernel veto unavailable: %s", exc)
            return action, confidence, False, "", None

        if intent.category in (IntentCategory.BACKCHANNEL, IntentCategory.SILENCE):
            return (
                RoutingAction.ANSWER_CONVERSATIONAL_QUERY,
                intent.intent_confidence,
                True,
                f"suppressed: {intent.category.value.lower()} is not an instruction",
                None,
            )

        if is_active and _TASK_STATUS_Q.search(message or ""):
            session = self.jarvis_engine.active_sessions.get(session_id or "")
            if session is not None:
                return (
                    RoutingAction.ANSWER_CONVERSATIONAL_QUERY,
                    0.95,
                    False,
                    "status question answered from session state (§14)",
                    task_status_answer(session),
                )

        if intent.category is IntentCategory.QUESTION and action in (
            RoutingAction.IMPROVISE_CURRENT_TASK, RoutingAction.FORK_NEW_TASK
        ):
            return (
                RoutingAction.ANSWER_CONVERSATIONAL_QUERY,
                intent.intent_confidence,
                False,
                "question answered rather than executed",
                None,
            )

        return action, confidence, False, "", None

    def register_session(self, session_id: str, bubble: SessionContextBubble) -> None:
        self._bubbles[session_id] = bubble
        self.state.primary_session_id = session_id
        if session_id not in self.state.active_lane_ids:
            self.state.active_lane_ids.append(session_id)

    def deregister_session(self, session_id: str) -> None:
        self._bubbles.pop(session_id, None)
        if session_id in self.state.active_lane_ids:
            self.state.active_lane_ids.remove(session_id)
        if self.state.primary_session_id == session_id:
            self.state.primary_session_id = (
                self.state.active_lane_ids[-1] if self.state.active_lane_ids else None
            )

    def _delegate(
        self,
        action: RoutingAction,
        message: str,
        session_id: Optional[str],
        bubble: Optional[SessionContextBubble],
    ) -> None:
        session = (
            self.jarvis_engine.active_sessions.get(session_id)
            if session_id else None
        )

        if action == RoutingAction.IMPROVISE_CURRENT_TASK and session and bubble:
            req = ImprovRequest(session_id=session_id, user_message=message)
            result = self.improvisation_engine.process(req, session, bubble)
            if not result.success:
                self._emit("improvisation_failed", {
                    "session_id": session_id,
                    "error": result.error,
                })

        elif action == RoutingAction.FORK_NEW_TASK and bubble:
            forked = self.fork_manager.fork_session(message, session_id or "", bubble)
            if forked:
                forked_bubble = SessionContextBubble(
                    session_id=forked.session_id,
                    language_preference=bubble.language_preference,
                    account_context=dict(bubble.account_context),
                )
                self.register_session(forked.session_id, forked_bubble)
            else:
                logger.info("Fork failed or at capacity; answering conversationally.")

        elif action == RoutingAction.ANSWER_CONVERSATIONAL_QUERY:
            try:
                self.three_lane.process_request(message)
            except Exception as exc:
                logger.error("ThreeLane query failed: %s", exc)

        elif action == RoutingAction.EXECUTE_BARGE_IN_CONTROL and session_id:
            self.jarvis_engine.handle_voice_barge_in(session_id, message)

        elif action == RoutingAction.ASK_CHECKPOINT_CONFIRMATION and session_id and bubble:
            session_obj = self.jarvis_engine.active_sessions.get(session_id)
            if session_obj:
                for step in session_obj.subtasks:
                    if step.id in self.checkpoint_gate._pending:
                        self.checkpoint_gate.confirm(step.id, message, bubble)
                        break
