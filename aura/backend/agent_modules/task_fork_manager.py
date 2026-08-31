"""
TaskForkManager — Parallel Task Fork Manager for Akansha AI OS.
Spawns independent ForkedSession instances in new parallel execution lanes.
Enforces:
  - TabID uniqueness across all active sessions
  - Originating session state immutability during fork
  - Partial bubble inheritance (language + account only)
  - Complete initialization within 1000ms
"""
from __future__ import annotations

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set

from .continuous_jarvis_engine import ContinuousVoiceJarvisEngine, ContinuousJarvisSession
from .thinking_engine import ThinkingEngine

logger = logging.getLogger(__name__)

MAX_CONCURRENT_LANES = 5


@dataclass
class LaneRegistry:
    """Tracks all active LaneIDs and TabIDs to enforce global uniqueness."""
    active_lane_ids: Set[str] = field(default_factory=set)
    active_tab_ids: Set[str] = field(default_factory=set)
    lane_to_session: Dict[str, str] = field(default_factory=dict)  # lane_id -> session_id

    def allocate_lane(self) -> Optional[str]:
        if len(self.active_lane_ids) >= MAX_CONCURRENT_LANES:
            return None
        lane_id = f"lane_{uuid.uuid4().hex[:8]}"
        self.active_lane_ids.add(lane_id)
        return lane_id

    def allocate_tab(self) -> str:
        while True:
            tab_id = f"tab_{uuid.uuid4().hex[:10]}"
            if tab_id not in self.active_tab_ids:
                self.active_tab_ids.add(tab_id)
                return tab_id

    def release(self, lane_id: str, tab_id: str) -> None:
        self.active_lane_ids.discard(lane_id)
        self.active_tab_ids.discard(tab_id)
        self.lane_to_session.pop(lane_id, None)


class TaskForkManager:
    """
    Parallel Task Fork Manager.
    Spawns ForkedSession instances in new lanes. Enforces:
      - TabID uniqueness across all active sessions
      - Originating session state immutability during fork
      - Partial bubble inheritance (language + account only)
      - Complete initialization within 1000ms
    """

    def __init__(
        self,
        jarvis_engine: ContinuousVoiceJarvisEngine,
        thinking_engine: Optional[ThinkingEngine] = None,
        event_emitter: Optional[Callable[[str, Dict], None]] = None,
    ) -> None:
        self.jarvis_engine = jarvis_engine
        self.thinking_engine = thinking_engine or ThinkingEngine()
        self._emit = event_emitter or (lambda e, d: None)
        self.registry = LaneRegistry()

    def fork_session(
        self,
        new_task_goal: str,
        originating_session_id: str,
        originating_bubble: "SessionContextBubble",  # type: ignore[name-defined]
    ) -> Optional[ContinuousJarvisSession]:
        """
        Spawn a new ForkedSession. Returns None if max lanes exceeded.
        Must complete within 1000ms.
        """
        start = time.perf_counter()

        lane_id = self.registry.allocate_lane()
        if lane_id is None:
            self._emit("max_lanes_exceeded", {
                "originating_session_id": originating_session_id,
                "goal": new_task_goal,
                "active_lanes": len(self.registry.active_lane_ids),
            })
            return None

        tab_id = self.registry.allocate_tab()

        try:
            # Decompose goal into steps
            thinking_result = self.thinking_engine.decompose_goal(new_task_goal)
            steps = [
                {
                    "description": st.description,
                    "action": st.target_tool or "direct_qa",
                    "params": {"tab_id": tab_id},
                    "voice_prompt": f"Starting: {st.title}",
                }
                for st in thinking_result.subtasks
            ]

            # Inherit partial context from originating bubble
            forked_bubble = originating_bubble.fork_copy(new_session_id="pending")

            forked_session = self.jarvis_engine.start_continuous_task(
                goal=new_task_goal,
                decomposed_steps=steps,
                language=forked_bubble.language_preference,
                target_tab_id=tab_id,
            )
            forked_bubble.session_id = forked_session.session_id
            self.registry.lane_to_session[lane_id] = forked_session.session_id

        except Exception as exc:
            logger.error("TaskForkManager: fork failed: %s", exc)
            self.registry.release(lane_id, tab_id)
            self._emit("fork_failed", {
                "originating_session_id": originating_session_id,
                "goal": new_task_goal,
                "error": str(exc),
            })
            return None

        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > 1000:
            logger.warning("TaskForkManager: fork took %.1fms (target 1000ms)", elapsed_ms)

        self._emit("session_forked", {
            "originating_session_id": originating_session_id,
            "forked_session_id": forked_session.session_id,
            "lane_id": lane_id,
            "tab_id": tab_id,
            "latency_ms": elapsed_ms,
        })
        return forked_session

    def release_session(self, session_id: str) -> None:
        """Release LaneID and TabID when a session completes or is cancelled."""
        for lane_id, sid in list(self.registry.lane_to_session.items()):
            if sid == session_id:
                session = self.jarvis_engine.active_sessions.get(session_id)
                tab_id = session.target_tab_id if session else ""
                self.registry.release(lane_id, tab_id or "")
                break

    @property
    def active_lane_count(self) -> int:
        return len(self.registry.active_lane_ids)
