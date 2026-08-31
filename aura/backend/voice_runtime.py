"""
Composition root for the conversational runtime, plus the autonomous step runner.

Two registered audit items live here.

**Item 7 — dead orchestration wiring.** `ConversationalOrchestrator` and
`VoiceFeedbackLoop` were never constructed outside tests. Every collaborator they
need already existed and passed its own unit tests; nothing ever built the graph,
so the routing brain and the narration loop were unreachable code. Nothing in the
running product could route a message to one of the five actions, and no subtask
event ever became spoken commentary.

**Item 8 — no autonomous runner.** `/api/jarvis/execute-step` advanced the task
graph exactly one step per client HTTP call. With nothing driving it, §14 ("talk
while executing") and §17 (live commentary) describe states the system could
never reach — there was no *while executing*. This module supplies the missing
driver: one asyncio task per session that keeps calling `execute_next_subtask`
until the goal is done, the user cancels, or a guard trips.

Design notes worth keeping:

* `execute_next_subtask` is **blocking** — the domain router drives real browser
  and desktop automation. It is therefore run through `asyncio.to_thread`, never
  inline, or a single automation step would freeze every other request the
  server is handling.
* Because of that, `ContinuousVoiceJarvisEngine._notify` fires **from a worker
  thread**. Everything downstream of it (the event bus, narration enqueue,
  barge-in forwarding) has to be safe to call off the event loop.
* A pause must not kill the runner. `execute_next_subtask` returns `None` both
  for "no such session" and for "session is not active", and the shipped
  `run_session_loop` helper treated both as "stop for good" — so the first
  "Akansha pause" ended autonomous execution permanently. The loop here
  distinguishes the two and keeps polling through a pause.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set

from .agent_modules.checkpoint_gate import CheckpointGate
from .agent_modules.continuous_jarvis_engine import ContinuousVoiceJarvisEngine
from .agent_modules.conversational_orchestrator import (
    ConversationalOrchestrator,
    RoutingDecision,
)
from .agent_modules.improvisation_engine import ImprovisationEngine
from .agent_modules.memory_module import MemoryModule
from .agent_modules.observability import ObservabilityTracer
from .agent_modules.reasoning_engine import ReasoningEngine
from .agent_modules.session_context_bubble import SessionContextBubble
from .agent_modules.task_fork_manager import TaskForkManager
from .agent_modules.three_lanes import ThreeLaneExecutionEngine
from .agent_modules.voice_feedback_loop import StatusNarration, VoiceFeedbackLoop

logger = logging.getLogger(__name__)


# ── event fan-out ───────────────────────────────────────────────────────────

class EventBus:
    """Fan-out of runtime events to any number of SSE / WebSocket subscribers.

    Publishers are not always on the event loop — the autonomous runner executes
    blocking steps in a worker thread and the engine notifies listeners from
    inside that thread. So `publish` must be callable from anywhere, and it must
    never block the caller: a slow subscriber drops frames rather than stalling
    live automation.
    """

    #: Per-subscriber backlog. Beyond this the subscriber loses frames; the
    #: alternative is back-pressuring a browser automation step on a dead tab.
    MAX_QUEUE = 256
    #: Replay buffer so a client that connects mid-task still sees recent work.
    MAX_HISTORY = 200

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=self.MAX_HISTORY)
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.dropped = 0

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def history(self, since: float = 0.0) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._history if e.get("timestamp", 0.0) > since]

    def publish(self, event: Dict[str, Any]) -> None:
        event.setdefault("timestamp", time.time())
        with self._lock:
            self._history.append(event)
            targets = list(self._subscribers)
        if not targets:
            return
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._deliver(targets, event)
        elif self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._deliver, targets, event)
        else:
            # Nobody can receive it; the history buffer above is the only record.
            self.dropped += 1

    def _deliver(self, targets: List[asyncio.Queue], event: Dict[str, Any]) -> None:
        for queue in targets:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped += 1


# ── runner bookkeeping ──────────────────────────────────────────────────────

@dataclass
class RunnerStats:
    session_id: str
    started_at: float = field(default_factory=time.time)
    steps_executed: int = 0
    last_step_at: Optional[float] = None
    stopped_at: Optional[float] = None
    stop_reason: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.stopped_at is None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "running": self.running,
            "started_at": self.started_at,
            "steps_executed": self.steps_executed,
            "last_step_at": self.last_step_at,
            "stopped_at": self.stopped_at,
            "stop_reason": self.stop_reason,
            "elapsed_s": round((self.stopped_at or time.time()) - self.started_at, 3),
        }


class VoiceRuntime:
    """The one live object graph: engine + orchestrator + narration + runners."""

    #: Breathing room between steps so a runaway plan cannot monopolise a core.
    STEP_GAP_S = 0.05
    #: How often a paused runner re-checks whether it may proceed.
    PAUSE_POLL_S = 0.25
    #: Give up on a session that has made no progress for this long. A paused
    #: task stays paused — only the runner retires, and it can be restarted.
    MAX_IDLE_S = 900.0
    #: Backstops against a plan that generates work faster than it completes.
    MAX_STEPS = 500
    MAX_WALL_S = 3600.0

    def __init__(self, jarvis_engine: Optional[ContinuousVoiceJarvisEngine] = None) -> None:
        self.bus = EventBus()
        self.memory = MemoryModule()
        self.jarvis = jarvis_engine or ContinuousVoiceJarvisEngine()

        self.reasoning = ReasoningEngine()
        self.tracer = ObservabilityTracer()
        self.vfl = VoiceFeedbackLoop(
            event_emitter=self._emit,
            orchestrator_callback=self._on_barge_in_utterance,
        )
        self.orchestrator = ConversationalOrchestrator(
            jarvis_engine=self.jarvis,
            improvisation_engine=ImprovisationEngine(
                reasoning_engine=self.reasoning, event_emitter=self._emit
            ),
            fork_manager=TaskForkManager(
                jarvis_engine=self.jarvis, event_emitter=self._emit
            ),
            checkpoint_gate=CheckpointGate(
                reasoning_engine=self.reasoning, event_emitter=self._emit
            ),
            voice_feedback_loop=self.vfl,
            three_lane_engine=ThreeLaneExecutionEngine(),
            reasoning_engine=self.reasoning,
            tracer=self.tracer,
            event_emitter=self._emit,
        )

        # Engine lifecycle events become both bus frames and spoken commentary.
        self.jarvis.register_event_listener(self._on_engine_event)

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runners: Dict[str, asyncio.Task] = {}
        self._stats: Dict[str, RunnerStats] = {}
        self._narration_task: Optional[asyncio.Task] = None
        self._lock = threading.Lock()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def attach_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the serving loop so worker threads can hand work back to it."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop
        self.bus.attach_loop(loop)

    async def ensure_started(self) -> None:
        """Idempotent: bind the loop and bring up the narration consumer."""
        self.attach_loop()
        if self._narration_task is None or self._narration_task.done():
            self._narration_task = asyncio.ensure_future(self._narration_consumer())

    async def shutdown(self) -> None:
        for session_id in list(self._runners):
            self.stop_autonomous(session_id, reason="shutdown")
        pending = [t for t in self._runners.values() if not t.done()]
        if self._narration_task is not None and not self._narration_task.done():
            self._narration_task.cancel()
            pending.append(self._narration_task)
        if pending:
            # `wait` rather than `wait_for(shield(...))`: every one of these tasks
            # is being cancelled and will raise CancelledError on the way out, so
            # awaiting them individually made a clean shutdown look like a crash.
            await asyncio.wait(pending, timeout=2.0)
        self._runners.clear()
        self._narration_task = None

    # ── autonomous execution (item 8) ───────────────────────────────────────

    def start_autonomous(
        self,
        session_id: str,
        bubble: Optional[SessionContextBubble] = None,
    ) -> Dict[str, Any]:
        """Drive `session_id` to completion without further client calls."""
        session = self.jarvis.active_sessions.get(session_id)
        if session is None:
            return {"started": False, "reason": "session_not_found"}

        existing = self._runners.get(session_id)
        if existing is not None and not existing.done():
            return {"started": False, "reason": "already_running",
                    "runner": self._stats[session_id].as_dict()}

        if bubble is None:
            bubble = self.orchestrator._bubbles.get(session_id) or SessionContextBubble(
                session_id=session_id,
                language_preference=session.language_preference,
            )
        self.orchestrator.register_session(session_id, bubble)

        stats = RunnerStats(session_id=session_id)
        self._stats[session_id] = stats

        if self._loop is None:
            try:
                self.attach_loop(asyncio.get_running_loop())
            except RuntimeError:
                stats.stopped_at = time.time()
                stats.stop_reason = "no_event_loop"
                return {"started": False, "reason": "no_event_loop"}

        task = asyncio.ensure_future(self._run_session(session_id, bubble, stats))
        self._runners[session_id] = task
        task.add_done_callback(lambda _t, sid=session_id: self._runners.pop(sid, None))

        self.bus.publish({"event": "autonomous_started",
                          "data": {"session_id": session_id, "goal": session.main_goal}})
        logger.info("Autonomous runner started for %s", session_id)
        return {"started": True, "runner": stats.as_dict()}

    def stop_autonomous(self, session_id: str, reason: str = "client_request") -> bool:
        """Retire the runner. The session itself is left exactly as it is."""
        task = self._runners.get(session_id)
        if task is None or task.done():
            return False
        stats = self._stats.get(session_id)
        if stats is not None and stats.stop_reason is None:
            stats.stop_reason = reason
        task.cancel()
        return True

    def runner_status(self, session_id: str) -> Optional[Dict[str, Any]]:
        stats = self._stats.get(session_id)
        return stats.as_dict() if stats else None

    async def _run_session(
        self,
        session_id: str,
        bubble: SessionContextBubble,
        stats: RunnerStats,
    ) -> None:
        idle_since: Optional[float] = None
        try:
            while True:
                session = self.jarvis.active_sessions.get(session_id)
                if session is None:
                    return self._retire(stats, "session_gone")
                if session.status in ("completed", "cancelled"):
                    return self._retire(stats, session.status)
                if stats.steps_executed >= self.MAX_STEPS:
                    return self._retire(stats, "step_limit")
                if time.time() - stats.started_at > self.MAX_WALL_S:
                    return self._retire(stats, "time_limit")

                if session.status != "active":
                    # Paused, or awaiting something. Wait it out — a pause is a
                    # pause, not a cancellation, and this is exactly where the
                    # shipped helper used to exit and never come back.
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > self.MAX_IDLE_S:
                        return self._retire(stats, "idle_timeout")
                    await asyncio.sleep(self.PAUSE_POLL_S)
                    continue

                result = await asyncio.to_thread(
                    self.jarvis.execute_next_subtask, session_id, bubble, self.memory
                )

                if result is None:
                    # Status flipped between the check above and the call — most
                    # often a barge-in landing mid-step. Same treatment as above.
                    idle_since = idle_since or time.time()
                    if time.time() - idle_since > self.MAX_IDLE_S:
                        return self._retire(stats, "idle_timeout")
                    await asyncio.sleep(self.PAUSE_POLL_S)
                    continue

                idle_since = None
                if result.get("status") == "finished":
                    # The terminal call does no work — it only reports that the
                    # graph is drained — so it must not inflate the step count.
                    return self._retire(stats, "completed")
                stats.steps_executed += 1
                stats.last_step_at = time.time()
                await asyncio.sleep(self.STEP_GAP_S)
        except asyncio.CancelledError:
            self._retire(stats, stats.stop_reason or "cancelled")
            raise
        except Exception as exc:  # a broken step must not take the server with it
            logger.exception("Autonomous runner for %s failed", session_id)
            self._retire(stats, f"error: {exc}")

    def _retire(self, stats: RunnerStats, reason: str) -> None:
        if stats.stopped_at is None:
            stats.stopped_at = time.time()
        stats.stop_reason = reason
        self.bus.publish({"event": "autonomous_stopped",
                          "data": {**stats.as_dict(), "reason": reason}})
        logger.info("Autonomous runner for %s retired: %s", stats.session_id, reason)

    # ── routing (item 7) ────────────────────────────────────────────────────

    def route(self, message: str, session_id: Optional[str] = None) -> RoutingDecision:
        """Synchronous routing. Blocking — call `route_async` from the loop."""
        decision = self.orchestrator.route(message, session_id)
        if decision.spoken_answer:
            # §14: the answer is already computed from session state, so speak it
            # at a priority that jumps the progress queue. The user asked; leaving
            # them behind four queued "starting step N" lines is not an answer.
            narration = StatusNarration(
                text=decision.spoken_answer,
                session_id=decision.target_session_id or "",
                narration_type="acknowledgment",
                priority=20,
            )
            self.bus.publish({
                "event": "narration",
                "data": {
                    "session_id": narration.session_id,
                    "text": narration.text,
                    "narration_type": narration.narration_type,
                    "priority": narration.priority,
                },
            })
            self._enqueue_narration(narration)
        return decision

    async def route_async(
        self, message: str, session_id: Optional[str] = None
    ) -> RoutingDecision:
        """Route off the event loop.

        `_delegate` can reach `ThreeLaneExecutionEngine.process_request`, which
        does LLM I/O, so routing inline would block every other request for the
        length of a model call.
        """
        await self.ensure_started()
        return await asyncio.to_thread(self.route, message, session_id)

    def register_bubble(
        self, session_id: str, language_preference: str = "telugu_english"
    ) -> SessionContextBubble:
        bubble = self.orchestrator._bubbles.get(session_id)
        if bubble is None:
            bubble = SessionContextBubble(
                session_id=session_id, language_preference=language_preference
            )
        self.orchestrator.register_session(session_id, bubble)
        return bubble

    # ── narration (§17) ─────────────────────────────────────────────────────

    def _on_engine_event(self, event: Dict[str, Any]) -> None:
        """Engine lifecycle → bus frame, and where apt → spoken commentary.

        Called from the runner's worker thread. Must not raise: the engine logs
        listener failures and carries on, which would silently lose commentary.
        """
        self.bus.publish(dict(event))
        try:
            narration = self._narration_for(event)
        except Exception as exc:
            logger.warning("Narration generation failed: %s", exc)
            return
        if narration is None:
            return
        self.bus.publish({
            "event": "narration",
            "data": {
                "session_id": narration.session_id,
                "text": narration.text,
                "narration_type": narration.narration_type,
                "priority": narration.priority,
            },
        })
        self._enqueue_narration(narration)

    def _narration_for(self, event: Dict[str, Any]) -> Optional[StatusNarration]:
        kind = event.get("event")
        data = event.get("data") or {}
        session_id = data.get("session_id", "")

        if kind == "awaiting_checkpoint":
            self.vfl.enter_listening_state()
            return None
        if kind == "gate_cleared":
            self.vfl.exit_listening_state()
            return None

        if kind == "task_completed":
            session = self.jarvis.active_sessions.get(session_id)
            total = len(session.subtasks) if session else 0
            return self.vfl.generate_final_narration(
                session_id, data.get("main_goal", ""), total
            )
        if kind == "plan_mutation_warning":
            return self.vfl.generate_warning_narration(session_id)
        if kind in ("task_paused", "task_cancelled", "task_resumed"):
            return self.vfl.generate_acknowledgment_narration(
                kind.replace("task_", ""), session_id
            )

        if kind not in ("subtask_started", "subtask_completed"):
            return None

        node = self._node_for(session_id, data.get("step_index"))
        if node is None:
            return None
        bubble = self.orchestrator._bubbles.get(session_id) or SessionContextBubble(
            session_id=session_id
        )
        if kind == "subtask_started":
            narration = self.vfl.generate_progress_narration(node, bubble)
        else:
            if self.vfl.should_suppress_narration(node):
                return None
            narration = self.vfl.generate_completion_narration(node, bubble)
        # VFL reads the session id off `step.params["session_id"]`, which the
        # engine never sets, so every narration would otherwise be addressed to
        # the empty string and no subscriber could filter by session.
        narration.session_id = session_id or narration.session_id
        return narration

    def _node_for(self, session_id: str, step_index: Any) -> Optional[Any]:
        session = self.jarvis.active_sessions.get(session_id)
        if session is None or not isinstance(step_index, int):
            return None
        if 0 <= step_index < len(session.subtasks):
            return session.subtasks[step_index]
        return None

    def _enqueue_narration(self, narration: StatusNarration) -> None:
        """Hand a narration to the VFL queue from whichever thread we are on."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                asyncio.ensure_future(self.vfl.enqueue(narration))
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(self.vfl.enqueue(narration), loop)

    async def _narration_consumer(self) -> None:
        """Drain the VFL queue forever, so it cannot grow without bound."""
        try:
            await self.vfl.run_narration_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Narration loop died")

    # ── barge-in forwarding ─────────────────────────────────────────────────

    def _on_barge_in_utterance(self, session_id: str, utterance: str) -> None:
        """VFL hands us a confirmed barge-in and times this hop against 150 ms.

        Routing can reach an LLM, so it is scheduled rather than run inline —
        blowing the budget here would be measured and logged as a latency
        failure while the real cost sat in the model call.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            self.route(utterance, session_id)
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.route_async(utterance, session_id))
        )

    # ── introspection ───────────────────────────────────────────────────────

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        self.bus.publish({"event": name, "data": payload})

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "main_goal": s.main_goal,
                    "status": s.status,
                    "current_step_index": s.current_step_index,
                    "total_steps": len(s.subtasks),
                    "autonomous": sid in self._runners,
                }
                for sid, s in self.jarvis.active_sessions.items()
            ],
            "runners": [st.as_dict() for st in self._stats.values()],
            "vfl_state": self.vfl.state.value,
            "orchestrator": {
                "primary_session_id": self.orchestrator.state.primary_session_id,
                "active_lane_ids": list(self.orchestrator.state.active_lane_ids),
            },
            "memory": {
                "active_tasks": len(self.memory.active_tasks),
                "facts": len(self.memory.memories),
            },
            "bus": {
                "subscribers": self.bus.subscriber_count,
                "dropped": self.bus.dropped,
            },
        }


_RUNTIME: Optional[VoiceRuntime] = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime(jarvis_engine: Optional[ContinuousVoiceJarvisEngine] = None) -> VoiceRuntime:
    """The one runtime for this process.

    `jarvis_engine` is honoured only on first call — it exists so the runtime
    adopts the engine `main` already exposes through `/api/jarvis/*` instead of
    quietly starting a second one whose sessions nothing else can see.
    """
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = VoiceRuntime(jarvis_engine=jarvis_engine)
    return _RUNTIME
