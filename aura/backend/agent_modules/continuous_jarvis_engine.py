"""
Continuous Voice Jarvis Engine for Akansha AI OS
Manages continuous background execution of longer multi-turn task workflows
with live voice feedback streaming, subtask tracking, and voice interruption support.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ContinuousSubtaskNode:
    id: str
    goal_description: str
    action: str
    params: Dict[str, Any]
    status: str = "pending"  # pending, executing, completed, failed, paused
    voice_update_prompt: str = ""
    result: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass
class ContinuousJarvisSession:
    session_id: str
    main_goal: str
    target_tab_id: Optional[str] = None
    language_preference: str = "telugu_english"
    status: str = "active"  # active, paused, completed, cancelled
    current_step_index: int = 0
    subtasks: List[ContinuousSubtaskNode] = field(default_factory=list)
    voice_logs: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    lane_id: Optional[str] = None
    is_forked: bool = False


class ContinuousVoiceJarvisEngine:
    """
    Continuous Task Automation Loop (Jarvis Mode).
    Decomposes multi-turn user voice goals into a continuous subtask loop,
    executes each step through domain executors, streams verbal status prompts,
    and supports hands-free voice barge-in interrupts with single-tab isolation.
    """

    def __init__(self, domain_router: Optional[Any] = None) -> None:
        self.router = domain_router
        self.active_sessions: Dict[str, ContinuousJarvisSession] = {}
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []

    def register_event_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Register listener for real-time WebSocket / SSE updates."""
        self.listeners.append(listener)

    def _notify(self, event_type: str, payload: Dict[str, Any]) -> None:
        event = {"event": event_type, "timestamp": time.time(), "data": payload}
        for listener in self.listeners:
            try:
                listener(event)
            except Exception as err:
                logger.warning("Listener notification error: %s", err)

    def start_continuous_task(
        self,
        goal: str,
        decomposed_steps: Optional[List[Dict[str, Any]]] = None,
        language: str = "telugu_english",
        target_tab_id: Optional[str] = None,
    ) -> ContinuousJarvisSession:
        """Initialize a new continuous Jarvis task session isolated to a single tab."""
        session_id = f"jarvis_{uuid.uuid4().hex[:8]}"
        session = ContinuousJarvisSession(
            session_id=session_id,
            main_goal=goal,
            target_tab_id=target_tab_id or f"tab_{session_id}",
            language_preference=language,
            status="active",
        )

        steps = decomposed_steps or self._default_decomposition(goal, session.target_tab_id)
        for idx, step in enumerate(steps):
            # Ensure target_tab_id is explicitly set on all step params to enforce zero interference
            step_params = step.get("params", {})
            if "tab_id" not in step_params and session.target_tab_id:
                step_params["tab_id"] = session.target_tab_id

            subnode = ContinuousSubtaskNode(
                id=f"step_{idx+1}",
                goal_description=step.get("description", f"Step {idx+1} for {goal}"),
                action=step.get("action", "direct_qa"),
                params=step_params,
                voice_update_prompt=step.get("voice_prompt", f"Executing step {idx+1}..."),
            )
            session.subtasks.append(subnode)

        self.active_sessions[session_id] = session
        self._notify("task_started", {
            "session_id": session_id,
            "goal": goal,
            "target_tab_id": session.target_tab_id,
            "total_steps": len(session.subtasks)
        })
        logger.info("Started Continuous Jarvis Session: %s on target tab %s for goal '%s'", session_id, session.target_tab_id, goal)
        return session

    def _default_decomposition(self, goal: str, target_tab_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Default step breakdown when external planner is not active."""
        goal_lower = goal.lower()
        if "search" in goal_lower or "browser" in goal_lower or "google" in goal_lower:
            return [
                {
                    "description": f"Open browser and search for '{goal}'",
                    "action": "navigate",
                    "params": {"url": f"https://www.google.com/search?q={goal}"},
                    "voice_prompt": f"Opening browser to search for '{goal}'",
                },
                {
                    "description": "Extract top results and summarize content",
                    "action": "scrape_web",
                    "params": {"query": goal},
                    "voice_prompt": "Analyzing search results and extracting key information.",
                },
                {
                    "description": "Present summary audio report",
                    "action": "direct_qa",
                    "params": {"response": f"Completed research for '{goal}'."},
                    "voice_prompt": f"Done! Here is what I found for '{goal}'.",
                },
            ]
        elif "open" in goal_lower or "app" in goal_lower or "window" in goal_lower:
            return [
                {
                    "description": f"Focus or open requested application for '{goal}'",
                    "action": "switch_window",
                    "params": {"title": goal},
                    "voice_prompt": f"Launching desktop window for '{goal}'",
                },
                {
                    "description": "Confirm desktop control readiness",
                    "action": "accessibility_click",
                    "params": {"element_id": "main_view"},
                    "voice_prompt": f"Window '{goal}' is ready for interaction.",
                },
            ]
        else:
            return [
                {
                    "description": f"Analyze and execute '{goal}'",
                    "action": "direct_qa",
                    "params": {"response": f"Executing continuous task for: {goal}"},
                    "voice_prompt": f"Processing continuous task for: {goal}",
                }
            ]

    def execute_next_subtask(
        self,
        session_id: str,
        bubble: Optional[Any] = None,
        memory_module: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Execute the next pending subtask in the continuous loop.

        Args:
            session_id: ID of the active session.
            bubble: Optional SessionContextBubble. When provided, its context is
                merged into step.params before execution and MemoryModule state
                is updated after each step completes.
            memory_module: Optional MemoryModule. When provided,
                update_task_state is called after each subtask completes.
        """
        session = self.active_sessions.get(session_id)
        if not session or session.status != "active":
            return None

        if session.current_step_index >= len(session.subtasks):
            session.status = "completed"
            self._notify("task_completed", {"session_id": session_id, "main_goal": session.main_goal})
            return {"status": "finished", "message": "All continuous subtasks completed."}

        subtask = session.subtasks[session.current_step_index]
        subtask.status = "executing"
        session.voice_logs.append(subtask.voice_update_prompt)

        # ── Bubble context merge ──────────────────────────────────────────────
        # Enrich step params with all current bubble fields before execution.
        if bubble is not None:
            try:
                subtask.params = bubble.merge_into_params(subtask.params)
            except Exception as err:
                logger.warning("execute_next_subtask: bubble.merge_into_params failed: %s", err)

        # ── Checkpoint gate check ─────────────────────────────────────────────
        # The orchestrator sets _requires_checkpoint via CheckpointGate.annotate_plan_with_gates.
        # Here we log a warning; the actual async gate is called by the orchestrator
        # (which has an async context). Sync callers will see this warning and can
        # choose to await CheckpointGate.await_confirmation before calling this method.
        if subtask.params.get("_requires_checkpoint") is True:
            logger.warning(
                "execute_next_subtask: step '%s' requires checkpoint confirmation "
                "(session=%s). Orchestrator should await CheckpointGate.await_confirmation "
                "before proceeding.",
                subtask.id,
                session_id,
            )

        # ── Emit subtask_started ──────────────────────────────────────────────
        self._notify("subtask_started", {
            "session_id": session_id,
            "step_index": session.current_step_index,
            "voice_prompt": subtask.voice_update_prompt,
            "description": subtask.goal_description,
        })

        res = {}
        if self.router:
            res = self.router.route_and_execute(
                user_input=subtask.goal_description,
                action=subtask.action,
                params=subtask.params,
            )
        else:
            res = {"status": "success", "executed_action": subtask.action, "details": "Execution simulation"}

        subtask.status = "completed"
        subtask.completed_at = time.time()
        subtask.result = res
        session.current_step_index += 1
        session.last_active_at = time.time()

        # ── MemoryModule state update ─────────────────────────────────────────
        if memory_module is not None:
            try:
                bubble_fields: Dict[str, Any] = {}
                if bubble is not None:
                    bubble_fields = {
                        "language_preference": getattr(bubble, "language_preference", None),
                        "strategy_preference": getattr(bubble, "strategy_preference", None),
                    }
                memory_module.update_task_state(
                    task_id=session_id,
                    goal=session.main_goal,
                    step=session.current_step_index,
                    total_steps=len(session.subtasks),
                    variables=bubble_fields or None,
                )
            except Exception as err:
                logger.warning("execute_next_subtask: memory_module.update_task_state failed: %s", err)

        # ── Emit subtask_completed ────────────────────────────────────────────
        self._notify("subtask_completed", {
            "session_id": session_id,
            "step_index": session.current_step_index - 1,
            "result": res,
            "next_pending": session.current_step_index < len(session.subtasks),
        })

        return {
            "status": "in_progress",
            "executed_step": subtask.goal_description,
            "voice_prompt": subtask.voice_update_prompt,
            "result": res,
            "step_index": session.current_step_index - 1,
            "remaining_steps": len(session.subtasks) - session.current_step_index,
        }

    def handle_voice_barge_in(self, session_id: str, interrupt_command: str) -> Dict[str, Any]:
        """Handle dynamic voice barge-in (e.g. 'Akansha pause', 'Akansha cancel')."""
        session = self.active_sessions.get(session_id)
        if not session:
            return {"status": "error", "message": "Session not found"}

        cmd_lower = interrupt_command.lower()
        if any(kw in cmd_lower for kw in ["stop", "pause", "hold", "wait", "aagu", "ruko"]):
            session.status = "paused"
            self._notify("task_paused", {"session_id": session_id, "reason": "Voice interrupt pause"})
            return {"status": "paused", "message": "Continuous execution paused by voice command."}

        if any(kw in cmd_lower for kw in ["cancel", "abort", "aapu", "bas"]):
            session.status = "cancelled"
            self._notify("task_cancelled", {"session_id": session_id, "reason": "Voice interrupt cancel"})
            return {"status": "cancelled", "message": "Continuous execution cancelled by user voice prompt."}

        if any(kw in cmd_lower for kw in ["resume", "continue", "start", "chalo"]):
            session.status = "active"
            self._notify("task_resumed", {"session_id": session_id})
            return {"status": "resumed", "message": "Resuming continuous execution."}

        return {"status": "active", "message": f"Processed voice feedback: {interrupt_command}"}

    def system_prompt_section(self) -> str:
        return """
# MODULE: CONTINUOUS VOICE JARVIS ENGINE
- MULTI-TURN CONTINUOUS AUTOMATION: Maintains active multi-step goals across voice streams without breaking turn context.
- VOICE STATUS STREAMING: Emits live spoken progress updates before/after subtask actions for hands-free operation.
- VOICE BARGE-IN INTERRUPTS: Supports dynamic verbal pauses, resumes, and cancellations mid-task.
"""

    # ── Task 8.3 — VoiceFeedbackLoop subscription pattern ────────────────────
    # VoiceFeedbackLoop registers as an event listener via register_event_listener.
    # The orchestrator wires this up at instantiation time, e.g.:
    #
    #   vfl = VoiceFeedbackLoop()
    #   def on_event(event_dict): ...
    #   jarvis_engine.register_event_listener(on_event)
    #
    # Event → VoiceFeedbackLoop handler mapping:
    #   "subtask_started"       → vfl.generate_progress_narration  → vfl.enqueue
    #   "subtask_completed"     → vfl.generate_completion_narration → vfl.enqueue
    #                             (suppressed via vfl.should_suppress_narration)
    #   "task_completed"        → vfl.generate_final_narration      → vfl.enqueue
    #   "plan_mutation_warning" → vfl.generate_warning_narration    → vfl.enqueue
    #   "awaiting_checkpoint"   → vfl.enter_listening_state
    #   "gate_cleared"          → vfl.exit_listening_state
    #
    # _notify already dispatches "subtask_started" and "subtask_completed" to all
    # registered listeners; VoiceFeedbackLoop subscribes to both.


# ── Task 8.4 — Cross-session asyncio parallel execution runner ───────────────


async def run_session_loop(
    jarvis_engine: ContinuousVoiceJarvisEngine,
    session_id: str,
) -> None:
    """Run one session's subtasks to completion, yielding to other sessions between steps."""
    while True:
        result = jarvis_engine.execute_next_subtask(session_id)
        if result is None or result.get("status") == "finished":
            break
        await asyncio.sleep(0)  # yield to other sessions


async def run_all_active_sessions(jarvis_engine: ContinuousVoiceJarvisEngine) -> None:
    """Run all active sessions in parallel. Each session loop yields between steps."""
    tasks = [
        asyncio.create_task(run_session_loop(jarvis_engine, sid))
        for sid in list(jarvis_engine.active_sessions.keys())
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
