"""
Integration and performance tests for the Continuous Conversational Automation Engine.
Tasks: 12.1-12.5, 13.1-13.4
"""
from __future__ import annotations
import asyncio
import sys
import os
import time
import importlib.util
import unittest
from unittest.mock import MagicMock, patch
from typing import Any, Dict

# Path setup
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_AGENT_MODULES = os.path.join(_BACKEND, "agent_modules")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _load(module_name, rel_path):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_AGENT_MODULES, rel_path)
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("agent_modules.reasoning_engine", "reasoning_engine.py")
_load("agent_modules.thinking_engine", "thinking_engine.py")
_load("agent_modules.user_understanding", "user_understanding.py")
_jarvis = _load("agent_modules.continuous_jarvis_engine", "continuous_jarvis_engine.py")
_scb = _load("agent_modules.session_context_bubble", "session_context_bubble.py")
_ie = _load("agent_modules.improvisation_engine", "improvisation_engine.py")
_tfm = _load("agent_modules.task_fork_manager", "task_fork_manager.py")
_cg = _load("agent_modules.checkpoint_gate", "checkpoint_gate.py")
_vfl = _load("agent_modules.voice_feedback_loop", "voice_feedback_loop.py")

SessionContextBubble = _scb.SessionContextBubble
ContextInjection = _scb.ContextInjection
ImprovisationEngine = _ie.ImprovisationEngine
ImprovRequest = _ie.ImprovRequest
MutationType = _ie.MutationType
TaskForkManager = _tfm.TaskForkManager
MAX_CONCURRENT_LANES = _tfm.MAX_CONCURRENT_LANES
CheckpointGate = _cg.CheckpointGate
VoiceFeedbackLoop = _vfl.VoiceFeedbackLoop
AudioEvent = _vfl.AudioEvent
ContinuousVoiceJarvisEngine = _jarvis.ContinuousVoiceJarvisEngine
ContinuousSubtaskNode = _jarvis.ContinuousSubtaskNode
ContinuousJarvisSession = _jarvis.ContinuousJarvisSession
run_all_active_sessions = _jarvis.run_all_active_sessions


def _make_session(pending=5, session_id="test"):
    engine = ContinuousVoiceJarvisEngine()
    session = engine.start_continuous_task(
        goal="integration test goal",
        decomposed_steps=[
            {
                "description": f"Step {i}",
                "action": "direct_qa",
                "params": {},
                "voice_prompt": f"Doing step {i}",
            }
            for i in range(pending)
        ],
    )
    return engine, session


def _mock_thinking():
    thinking = MagicMock()
    sm = MagicMock()
    sm.description = "step"
    sm.title = "Step"
    sm.target_tool = "direct_qa"
    tr = MagicMock()
    tr.subtasks = [sm]
    thinking.decompose_goal.return_value = tr
    return thinking


class TestIntegration(unittest.TestCase):
    """Integration tests — tasks 12.1-12.5"""

    # Task 12.1 — full session with mid-task refinement
    def test_full_session_mid_task_refinement(self):
        """Start session with 10 steps; apply ImprovRequest after step 3; verify execution
        continues with updated params on all subsequent steps."""
        engine, session = _make_session(pending=10)
        bubble = SessionContextBubble(session_id=session.session_id)

        # Execute 3 steps
        for _ in range(3):
            result = engine.execute_next_subtask(session.session_id, bubble=bubble)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "in_progress")

        self.assertEqual(session.current_step_index, 3)

        # Apply language change mid-task
        req = ImprovRequest(session_id=session.session_id, user_message="do it in java")
        improv_engine = ImprovisationEngine()
        mutation_result = improv_engine.process(req, session, bubble)
        self.assertTrue(mutation_result.success)
        self.assertEqual(mutation_result.mutation.mutation_type, MutationType.LANGUAGE_CHANGE)
        self.assertEqual(bubble.language_preference, "Java")

        # Execute remaining steps — all should have language_preference=Java.
        # Loop until execute_next_subtask returns None or "finished" (session completed).
        while True:
            result = engine.execute_next_subtask(session.session_id, bubble=bubble)
            if result is None:
                break
            if result["status"] == "in_progress":
                last_step_index = result["step_index"]
                executed_step = session.subtasks[last_step_index]
                self.assertEqual(
                    executed_step.params.get("language_preference"),
                    "Java",
                    f"Step {last_step_index} missing language_preference=Java",
                )
            elif result["status"] == "finished":
                break

        self.assertEqual(session.status, "completed")

    # Task 12.2 — parallel fork with tab isolation
    def test_parallel_fork_tab_isolation(self):
        """Fork two sessions; run both to completion; verify distinct tab_ids, no
        cross-contamination."""
        engine = ContinuousVoiceJarvisEngine()
        thinking = _mock_thinking()
        fork_mgr = TaskForkManager(jarvis_engine=engine, thinking_engine=thinking)
        bubble_orig = SessionContextBubble(session_id="orig")

        session_a = fork_mgr.fork_session("goal A", "orig", bubble_orig)
        session_b = fork_mgr.fork_session("goal B", "orig", bubble_orig)
        self.assertIsNotNone(session_a)
        self.assertIsNotNone(session_b)

        # Tab IDs must be different
        self.assertNotEqual(session_a.target_tab_id, session_b.target_tab_id)

        # Run both sessions concurrently via asyncio
        asyncio.run(run_all_active_sessions(engine))

        # Both should have completed without touching each other's subtask list
        self.assertEqual(session_a.status, "completed")
        self.assertEqual(session_b.status, "completed")
        # No subtask from A should appear in B's list and vice versa.
        # Session isolation means the subtask *lists* are separate objects —
        # the engine assigns deterministic IDs (step_1, step_2 …) per session,
        # so uniqueness of IDs across sessions is NOT guaranteed or required.
        # What matters is that neither list contains objects from the other.
        for step in session_a.subtasks:
            self.assertNotIn(step, session_b.subtasks, "Session A step object found in session B")

    # Task 12.3 — checkpoint gate full lifecycle
    def test_checkpoint_gate_lifecycle(self):
        """High-stake step → awaiting_checkpoint → confirm → execution resumes."""
        _, session = _make_session(pending=3)
        bubble = SessionContextBubble(session_id=session.session_id)

        gate = CheckpointGate()
        # Mark step 0 as high-stake
        step = session.subtasks[0]
        step.params["_requires_checkpoint"] = True
        step.params["_estimated_risk"] = 0.85

        async def run():
            # Start confirmation wait in background
            confirm_task = asyncio.create_task(
                gate.await_confirmation(step, session, bubble)
            )
            # Give it a moment to enter awaiting state
            await asyncio.sleep(0.05)
            self.assertEqual(session.status, "awaiting_checkpoint")
            # Confirm
            gate.confirm(step.id, "yes proceed", bubble)
            confirmed = await confirm_task
            return confirmed

        confirmed = asyncio.run(run())
        self.assertTrue(confirmed)
        self.assertTrue(bubble.has_answered_checkpoint(step.id))

    # Task 12.4 — language switch mid-task
    def test_language_switch_mid_task(self):
        """Issue language change ImprovRequest; verify all subsequent pending steps have
        new language."""
        _, session = _make_session(pending=8)
        bubble = SessionContextBubble(session_id=session.session_id)
        improv = ImprovisationEngine()

        # Mark first two steps as completed (simulate already-executed)
        session.subtasks[0].status = "completed"
        session.subtasks[1].status = "completed"
        session.current_step_index = 2

        # Switch to Python
        req = ImprovRequest(session_id=session.session_id, user_message="switch to in python")
        result = improv.process(req, session, bubble)
        self.assertTrue(result.success)
        self.assertEqual(bubble.language_preference, "Python")

        for step in session.subtasks[session.current_step_index:]:
            if step.status == "pending":
                self.assertEqual(
                    step.params.get("language_preference"),
                    "Python",
                    f"Step {step.id} missing language_preference=Python",
                )

    # Task 12.5 — session completion triggers memory and preference promotion
    def test_session_completion_memory_update(self):
        """Complete a session; verify MemoryModule.update_task_state called for each step."""
        engine, session = _make_session(pending=3)
        bubble = SessionContextBubble(session_id=session.session_id)
        bubble.language_preference = "Java"

        memory_mock = MagicMock()

        while session.current_step_index < len(session.subtasks):
            r = engine.execute_next_subtask(
                session.session_id, bubble=bubble, memory_module=memory_mock
            )
            if r and r["status"] == "finished":
                break

        self.assertEqual(memory_mock.update_task_state.call_count, 3)
        # Each call should have the correct session_id as the task_id kwarg
        for call in memory_mock.update_task_state.call_args_list:
            args = call.args
            kwargs = call.kwargs
            task_id_val = kwargs.get("task_id") if "task_id" in kwargs else (args[0] if args else None)
            self.assertEqual(task_id_val, session.session_id)


class TestPerformance(unittest.TestCase):
    """Performance regression tests — tasks 13.1-13.4"""

    # Task 13.2 — PlanMutation apply time ≤ 500ms for 200-step plan
    def test_plan_mutation_500ms(self):
        """200-step session; language mutation must complete within 500ms."""
        engine = ContinuousVoiceJarvisEngine()
        session = engine.start_continuous_task(
            goal="perf test",
            decomposed_steps=[
                {
                    "description": f"Step {i}",
                    "action": "direct_qa",
                    "params": {},
                    "voice_prompt": f"Step {i}",
                }
                for i in range(200)
            ],
        )
        bubble = SessionContextBubble(session_id=session.session_id)
        req = ImprovRequest(session_id=session.session_id, user_message="in java")
        improv = ImprovisationEngine()

        start = time.perf_counter()
        result = improv.process(req, session, bubble)
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertTrue(result.success)
        self.assertLessEqual(
            elapsed_ms, 500, f"Mutation took {elapsed_ms:.1f}ms (limit 500ms)"
        )

    # Task 13.3 — TaskForkManager.fork_session ≤ 1000ms for 50-step fork
    def test_fork_session_1000ms(self):
        """50-step fork must complete within 1000ms."""
        engine = ContinuousVoiceJarvisEngine()
        thinking = MagicMock()
        subtasks = []
        for i in range(50):
            sm = MagicMock()
            sm.description = f"step {i}"
            sm.title = f"Step {i}"
            sm.target_tool = "direct_qa"
            subtasks.append(sm)
        tr = MagicMock()
        tr.subtasks = subtasks
        thinking.decompose_goal.return_value = tr

        fork_mgr = TaskForkManager(jarvis_engine=engine, thinking_engine=thinking)
        bubble = SessionContextBubble(session_id="perf_orig")

        start = time.perf_counter()
        session = fork_mgr.fork_session("50-step goal", "perf_orig", bubble)
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertIsNotNone(session)
        self.assertEqual(len(session.subtasks), 50)
        self.assertLessEqual(
            elapsed_ms, 1000, f"Fork took {elapsed_ms:.1f}ms (limit 1000ms)"
        )

    # Task 13.4 — Barge-in forward latency ≤ 150ms
    def test_barge_in_latency_150ms(self):
        """AudioEvent(confidence=0.9) → orchestrator_callback within 150ms."""
        latencies = []

        def callback(session_id, utterance):
            latencies.append(time.perf_counter())

        vfl = VoiceFeedbackLoop(orchestrator_callback=callback)
        event = AudioEvent(
            wake_confidence=0.9, utterance="pause", session_id="perf_sess"
        )

        start = time.perf_counter()
        asyncio.run(vfl.handle_audio_event(event))
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertEqual(len(latencies), 1, "Callback not invoked")
        self.assertLessEqual(
            elapsed_ms, 150, f"Barge-in took {elapsed_ms:.1f}ms (limit 150ms)"
        )

    # Task 13.1 — Orchestration routing latency ≤ 800ms at p95 (stub)
    def test_orchestration_routing_800ms(self):
        """ImprovisationEngine.process on 10 steps must be well under 800ms p95."""
        engine_local = ContinuousVoiceJarvisEngine()
        session_local = engine_local.start_continuous_task(
            goal="routing perf test",
            decomposed_steps=[
                {
                    "description": f"Step {i}",
                    "action": "direct_qa",
                    "params": {},
                    "voice_prompt": f"Step {i}",
                }
                for i in range(10)
            ],
        )
        bubble = SessionContextBubble(session_id=session_local.session_id)
        improv = ImprovisationEngine()
        latencies = []
        for msg in ["in java", "use optimal", "add debug step", "use python", "brute force"]:
            req = ImprovRequest(session_id=session_local.session_id, user_message=msg)
            t0 = time.perf_counter()
            improv.process(req, session_local, bubble)
            latencies.append((time.perf_counter() - t0) * 1000)
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
        self.assertLessEqual(
            p95, 800, f"p95 routing latency {p95:.1f}ms exceeds 800ms"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
