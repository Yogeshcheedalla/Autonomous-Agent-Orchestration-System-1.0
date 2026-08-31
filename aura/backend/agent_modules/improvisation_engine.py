"""
ImprovisationEngine — Mid-Task Plan Mutation Engine for Akansha AI OS.
Mutates a running ActiveTaskPlan in real time based on user refinements.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .continuous_jarvis_engine import ContinuousJarvisSession, ContinuousSubtaskNode
from .reasoning_engine import ReasoningEngine
from .session_context_bubble import SessionContextBubble, ContextInjection

logger = logging.getLogger(__name__)


class MutationType(str, Enum):
    INJECT = "inject"
    UPDATE_PARAMS = "update_params"
    REMOVE = "remove"
    REORDER = "reorder"
    LANGUAGE_CHANGE = "language_change"
    STRATEGY_OVERRIDE = "strategy_override"


@dataclass
class StepOperation:
    op_type: MutationType
    target_step_id: Optional[str] = None
    insert_at_index: Optional[int] = None
    new_step: Optional[ContinuousSubtaskNode] = None
    updated_params: Optional[Dict[str, Any]] = None
    new_order: Optional[List[str]] = None


@dataclass
class ImprovRequest:
    session_id: str
    user_message: str
    created_at: float = field(default_factory=time.time)


@dataclass
class PlanMutation:
    mutation_id: str
    session_id: str
    mutation_type: MutationType
    operations: List[StepOperation]
    steps_affected: int
    conflict_detected: bool = False
    safe_injection_index: Optional[int] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class PlanMutationResult:
    success: bool
    mutation: PlanMutation
    applied_at: float = field(default_factory=time.time)
    error: Optional[str] = None


class ImprovisationEngine:
    """
    Mid-Task Improvisation Engine.
    Constraints:
      - Only forward-compatible mutations (completed/executing steps untouched)
      - Mutation construction + apply must complete within 500ms
      - current_step_index never decremented
    """

    LANGUAGE_KEYWORDS = [
        "in java", "in python", "in c++", "in javascript",
        "java lo", "python lo", "in typescript", "in go", "in rust",
    ]
    STRATEGY_KEYWORDS = [
        "optimal", "brute force", "memory efficient", "fastest", "simplest",
    ]

    def __init__(
        self,
        reasoning_engine: Optional[ReasoningEngine] = None,
        event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        self.reasoning_engine = reasoning_engine or ReasoningEngine()
        self._emit = event_emitter or (lambda event, data: None)

    def process(
        self,
        improv_request: ImprovRequest,
        session: ContinuousJarvisSession,
        bubble: SessionContextBubble,
    ) -> PlanMutationResult:
        """Main entry point. Must complete within 500ms."""
        start = time.perf_counter()
        mutation = self._build_mutation(improv_request, session, bubble)
        result = self._apply_mutation(mutation, session, bubble)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > 500:
            logger.warning(
                "ImprovisationEngine: mutation took %.1fms (target 500ms)", elapsed_ms
            )
        if result.success:
            self._emit("plan_mutated", {
                "session_id": session.session_id,
                "mutation_type": mutation.mutation_type,
                "steps_affected": mutation.steps_affected,
                "latency_ms": elapsed_ms,
            })
        return result

    def _build_mutation(
        self,
        req: ImprovRequest,
        session: ContinuousJarvisSession,
        bubble: SessionContextBubble,
    ) -> PlanMutation:
        msg = req.user_message.lower()
        operations: List[StepOperation] = []
        mutation_type = MutationType.INJECT
        conflict = False
        safe_index: Optional[int] = None

        # Detect language change
        for kw in self.LANGUAGE_KEYWORDS:
            if kw in msg:
                lang = kw.split()[-1].capitalize()
                mutation_type = MutationType.LANGUAGE_CHANGE
                operations.append(StepOperation(
                    op_type=MutationType.LANGUAGE_CHANGE,
                    updated_params={"language_preference": lang},
                ))
                break

        # Detect strategy override
        if not operations:
            for kw in self.STRATEGY_KEYWORDS:
                if kw in msg:
                    mutation_type = MutationType.STRATEGY_OVERRIDE
                    operations.append(StepOperation(
                        op_type=MutationType.STRATEGY_OVERRIDE,
                        updated_params={"strategy_override": kw},
                    ))
                    break

        # Default: inject a refinement step
        if not operations:
            next_idx = self._find_safe_injection_point(session)
            conflict = next_idx > session.current_step_index
            if conflict:
                safe_index = next_idx
            new_step = ContinuousSubtaskNode(
                id=f"improv_{uuid.uuid4().hex[:6]}",
                goal_description=req.user_message,
                action="direct_qa",
                params={"refinement": req.user_message},
                voice_update_prompt=f"Adapting: {req.user_message[:40]}",
            )
            operations.append(StepOperation(
                op_type=MutationType.INJECT,
                insert_at_index=next_idx,
                new_step=new_step,
            ))

        return PlanMutation(
            mutation_id=f"mut_{uuid.uuid4().hex[:8]}",
            session_id=req.session_id,
            mutation_type=mutation_type,
            operations=operations,
            steps_affected=len(operations),
            conflict_detected=conflict,
            safe_injection_index=safe_index,
        )

    def _find_safe_injection_point(self, session: ContinuousJarvisSession) -> int:
        for idx, step in enumerate(session.subtasks):
            if step.status == "pending" and idx >= session.current_step_index:
                return idx
        return len(session.subtasks)

    def _apply_mutation(
        self,
        mutation: PlanMutation,
        session: ContinuousJarvisSession,
        bubble: SessionContextBubble,
    ) -> PlanMutationResult:
        """Apply the mutation atomically. Never decrement current_step_index."""
        # Snapshot for rollback
        subtasks_snapshot = list(session.subtasks)
        try:
            if mutation.conflict_detected:
                self._emit("plan_mutation_warning", {
                    "session_id": session.session_id,
                    "safe_injection_index": mutation.safe_injection_index,
                })

            for op in mutation.operations:
                if op.op_type == MutationType.INJECT and op.new_step is not None:
                    idx = op.insert_at_index if op.insert_at_index is not None else len(session.subtasks)
                    idx = max(idx, session.current_step_index)  # never go backwards
                    session.subtasks.insert(idx, op.new_step)

                elif op.op_type == MutationType.UPDATE_PARAMS and op.target_step_id:
                    for step in session.subtasks:
                        if step.id == op.target_step_id and step.status == "pending":
                            step.params.update(op.updated_params or {})

                elif op.op_type == MutationType.REMOVE and op.target_step_id:
                    session.subtasks = [
                        s for s in session.subtasks
                        if not (s.id == op.target_step_id and s.status == "pending")
                    ]

                elif op.op_type == MutationType.LANGUAGE_CHANGE and op.updated_params:
                    lang = op.updated_params.get("language_preference", "")
                    injection = ContextInjection(
                        injection_type="language",
                        key="language_preference",
                        value=lang,
                        utterance="",
                    )
                    bubble.add_injection(injection)
                    # Re-parameterize all pending steps
                    for step in session.subtasks[session.current_step_index:]:
                        if step.status == "pending":
                            step.params["language_preference"] = lang

                elif op.op_type == MutationType.STRATEGY_OVERRIDE and op.updated_params:
                    strategy = op.updated_params.get("strategy_override", "")
                    for step in session.subtasks[session.current_step_index:]:
                        if step.status == "pending":
                            step.params["strategy_override"] = strategy

            return PlanMutationResult(success=True, mutation=mutation)

        except Exception as exc:
            # Rollback on failure
            session.subtasks = subtasks_snapshot
            logger.error("ImprovisationEngine: mutation failed: %s", exc)
            self._emit("plan_mutation_failed", {
                "session_id": session.session_id,
                "error": str(exc),
            })
            return PlanMutationResult(success=False, mutation=mutation, error=str(exc))
