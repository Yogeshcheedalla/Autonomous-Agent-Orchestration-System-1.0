"""
Tests for `backend.voice_runtime` — the composition root and the autonomous runner.
==================================================================================

These cover audit items 7 and 8, and they exist because both were failures of
*wiring*, not of logic: `ConversationalOrchestrator` and `VoiceFeedbackLoop` each
had passing unit tests while being unreachable from the running product, and the
task graph only advanced when a client happened to POST a step.

So every test here drives the real object graph. Nothing is mocked except the
clock's patience — step gaps are shortened so a twelve-step plan finishes in
milliseconds rather than seconds.

There is no pytest-asyncio in this environment, so async bodies are run through
`asyncio.run` inside ordinary sync tests. That is also closer to how the routes
call in: a fresh loop per request path.

One platform trap, since it invalidated a first draft of these tests: on Windows
the event loop's clock resolution is ~15.6 ms, and `_run_once` fires any timer
due before `now + resolution`. So `await asyncio.sleep(0.005)` does not sleep at
all — it yields. Anything here that waits does so against a wall-clock deadline
in units of `TICK`, and anything that needs the runner to have made progress
waits for the step counter rather than for a duration.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.agent_modules.conversational_orchestrator import (
    RoutingAction,
    task_status_answer,
)
from backend.agent_modules.voice_feedback_loop import AudioEvent
from backend.voice_runtime import EventBus, VoiceRuntime, get_runtime


# ── helpers ────────────────────────────────────────────────────────────────

#: One event-loop tick that actually sleeps on Windows (resolution ≈15.6 ms).
TICK = 0.02


def _steps(n, action="direct_qa"):
    return [
        {
            "description": f"Step {i}",
            "action": action,
            "params": {},
            "voice_prompt": f"Doing step {i}",
        }
        for i in range(n)
    ]


def _fast(runtime, gap=0.0):
    """Collapse the pacing so tests measure behaviour, not wall clock.

    `gap` is worth setting whenever a test needs the plan to still be running
    when the next line executes: with no router attached a step is pure
    simulation and ~0.7 steps/ms, so a 30-step plan finishes inside a single
    `sleep(0.05)` and a test about pausing ends up testing completion.
    """
    runtime.STEP_GAP_S = gap
    runtime.PAUSE_POLL_S = TICK
    return runtime


async def _until(predicate, *, timeout=5.0):
    """Poll `predicate` against a wall-clock deadline. Returns whether it held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(TICK)
    return predicate()


async def _drain_to(runtime, session_id, *, timeout=10.0):
    """Wait until the runner for `session_id` retires, or give up."""
    await _until(
        lambda: (runtime.runner_status(session_id) or {}).get("stopped_at"),
        timeout=timeout,
    )
    return runtime.runner_status(session_id)


async def _wait_steps(runtime, session_id, count, *, timeout=5.0):
    """Wait until the runner has executed at least `count` steps."""
    ok = await _until(
        lambda: (runtime.runner_status(session_id) or {}).get("steps_executed", 0) >= count,
        timeout=timeout,
    )
    assert ok, f"runner never reached {count} steps: {runtime.runner_status(session_id)}"


def _collect(runtime):
    """Subscribe and return (list-that-fills, the pump task to cancel)."""
    seen = []
    queue = runtime.bus.subscribe()

    async def pump():
        while True:
            seen.append(await queue.get())

    task = asyncio.ensure_future(pump())
    return seen, task


# ── the event bus ──────────────────────────────────────────────────────────

class TestEventBus:
    """The bus is published to from worker threads, so it gets its own tests."""

    def test_subscriber_receives_published_events(self):
        async def body():
            bus = EventBus()
            bus.attach_loop(asyncio.get_running_loop())
            queue = bus.subscribe()
            bus.publish({"event": "hello", "data": {}})
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert event["event"] == "hello"
            assert "timestamp" in event

        asyncio.run(body())

    def test_history_replays_for_a_late_subscriber(self):
        bus = EventBus()
        bus.publish({"event": "first", "data": {}})
        bus.publish({"event": "second", "data": {}})
        assert [e["event"] for e in bus.history()] == ["first", "second"]

    def test_a_full_subscriber_drops_frames_instead_of_blocking(self):
        async def body():
            bus = EventBus()
            bus.attach_loop(asyncio.get_running_loop())
            bus.MAX_QUEUE = 2
            queue = bus.subscribe()
            for i in range(6):
                bus.publish({"event": f"e{i}", "data": {}})
            assert queue.qsize() == 2
            # A live automation step must never be back-pressured by a slow UI.
            assert bus.dropped == 4

        asyncio.run(body())

    def test_publishing_from_a_worker_thread_reaches_the_loop(self):
        async def body():
            bus = EventBus()
            bus.attach_loop(asyncio.get_running_loop())
            queue = bus.subscribe()
            await asyncio.to_thread(bus.publish, {"event": "from_thread", "data": {}})
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert event["event"] == "from_thread"

        asyncio.run(body())

    def test_unsubscribed_queues_stop_receiving(self):
        async def body():
            bus = EventBus()
            bus.attach_loop(asyncio.get_running_loop())
            queue = bus.subscribe()
            bus.unsubscribe(queue)
            bus.publish({"event": "ignored", "data": {}})
            assert queue.empty()
            assert bus.subscriber_count == 0

        asyncio.run(body())


# ── item 8: autonomous execution ───────────────────────────────────────────

class TestAutonomousRunner:
    """A plan must finish without the client asking for each step."""

    def test_a_plan_runs_to_completion_with_no_client_calls(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="research something", decomposed_steps=_steps(6)
            )
            assert runtime.start_autonomous(session.session_id)["started"] is True
            status = await _drain_to(runtime, session.session_id)
            assert status["stop_reason"] == "completed"
            # Exactly the real steps: the terminal "finished" call does no work.
            assert status["steps_executed"] == 6
            assert runtime.jarvis.active_sessions[session.session_id].status == "completed"
            await runtime.shutdown()

        asyncio.run(body())

    def test_an_unknown_session_is_refused(self):
        async def body():
            runtime = VoiceRuntime()
            await runtime.ensure_started()
            result = runtime.start_autonomous("jarvis_nope")
            assert result == {"started": False, "reason": "session_not_found"}
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_second_runner_for_one_session_is_refused(self):
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="long", decomposed_steps=_steps(200)
            )
            runtime.start_autonomous(session.session_id)
            second = runtime.start_autonomous(session.session_id)
            assert second["started"] is False
            assert second["reason"] == "already_running"
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_pause_suspends_the_runner_without_killing_it(self):
        """The regression that motivated this module.

        `run_session_loop` treated `execute_next_subtask` returning None as "we
        are done", but it returns None for a paused session too — so the first
        "Akansha pause" ended autonomous execution for good and "resume" had
        nothing left to resume.
        """
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="long job", decomposed_steps=_steps(20)
            )
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 2)
            runtime.jarvis.handle_voice_barge_in(session.session_id, "akansha pause")

            paused = runtime.runner_status(session.session_id)
            assert paused["running"] is True
            await asyncio.sleep(6 * TICK)
            held = runtime.runner_status(session.session_id)
            assert held["running"] is True
            assert session.status == "paused"
            # A step in flight when the pause landed may still have finished; what
            # must not happen is the plan continuing to advance while paused.
            assert held["steps_executed"] <= paused["steps_executed"] + 1
            stalled = held["steps_executed"]
            await asyncio.sleep(6 * TICK)
            assert runtime.runner_status(session.session_id)["steps_executed"] == stalled

            runtime.jarvis.handle_voice_barge_in(session.session_id, "resume")
            status = await _drain_to(runtime, session.session_id)
            assert status["stop_reason"] == "completed"
            assert status["steps_executed"] == 20
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_cancel_retires_the_runner(self):
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="cancel me", decomposed_steps=_steps(200)
            )
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 1)
            runtime.jarvis.handle_voice_barge_in(session.session_id, "cancel")
            status = await _drain_to(runtime, session.session_id)
            assert status["running"] is False
            assert runtime.jarvis.active_sessions[session.session_id].status == "cancelled"
            await runtime.shutdown()

        asyncio.run(body())

    def test_stop_autonomous_leaves_the_session_alone(self):
        """Retiring the runner is not cancelling the task — it stays resumable."""
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="keep me", decomposed_steps=_steps(200)
            )
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 1)
            assert runtime.stop_autonomous(session.session_id) is True
            await _drain_to(runtime, session.session_id)
            assert runtime.jarvis.active_sessions[session.session_id].status == "active"
            assert runtime.runner_status(session.session_id)["stop_reason"] == "client_request"
            # And it can be handed back to a fresh runner.
            assert runtime.start_autonomous(session.session_id)["started"] is True
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_step_limit_stops_a_runaway_plan(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            runtime.MAX_STEPS = 4
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="too long", decomposed_steps=_steps(50)
            )
            runtime.start_autonomous(session.session_id)
            status = await _drain_to(runtime, session.session_id)
            assert status["stop_reason"] == "step_limit"
            assert status["steps_executed"] == 4
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_session_paused_forever_retires_on_the_idle_guard(self):
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            runtime.MAX_IDLE_S = 3 * TICK
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="abandoned", decomposed_steps=_steps(200)
            )
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 1)
            runtime.jarvis.handle_voice_barge_in(session.session_id, "pause")
            status = await _drain_to(runtime, session.session_id)
            assert status["stop_reason"] == "idle_timeout"
            # The task itself is untouched — only the runner gave up.
            assert runtime.jarvis.active_sessions[session.session_id].status == "paused"
            await runtime.shutdown()

        asyncio.run(body())

    def test_two_sessions_run_concurrently(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            a = runtime.jarvis.start_continuous_task(goal="a", decomposed_steps=_steps(8))
            b = runtime.jarvis.start_continuous_task(goal="b", decomposed_steps=_steps(8))
            runtime.start_autonomous(a.session_id)
            runtime.start_autonomous(b.session_id)
            for session in (a, b):
                status = await _drain_to(runtime, session.session_id)
                assert status["stop_reason"] == "completed"
                assert status["steps_executed"] == 8
            await runtime.shutdown()

        asyncio.run(body())

    def test_memory_tracks_progress_while_the_runner_works(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="remember this", decomposed_steps=_steps(5)
            )
            runtime.start_autonomous(session.session_id)
            await _drain_to(runtime, session.session_id)
            state = runtime.memory.active_tasks[session.session_id]
            assert state.goal == "remember this"
            assert (state.current_step, state.total_steps) == (5, 5)
            assert state.is_completed is True
            await runtime.shutdown()

        asyncio.run(body())


# ── item 7: narration and routing are reachable at all ─────────────────────

class TestLiveCommentary:
    """§17 — subtask events become spoken lines without the client asking."""

    def test_every_step_produces_progress_and_completion_narration(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            seen, pump = _collect(runtime)
            session = runtime.jarvis.start_continuous_task(
                goal="narrate me", decomposed_steps=_steps(3)
            )
            runtime.start_autonomous(session.session_id)
            await _drain_to(runtime, session.session_id)
            # 3 starts + 3 completions + 1 final
            await _until(
                lambda: len([e for e in seen if e["event"] == "narration"]) >= 7
            )
            pump.cancel()

            narrations = [e["data"] for e in seen if e["event"] == "narration"]
            assert len(narrations) == 7
            assert narrations[-1]["narration_type"] == "final"
            # Every line is addressed to the session, which VFL alone could not do:
            # it reads the id from step.params, and the engine never puts it there.
            assert {n["session_id"] for n in narrations} == {session.session_id}
            await runtime.shutdown()

        asyncio.run(body())

    def test_lifecycle_events_reach_subscribers(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            seen, pump = _collect(runtime)
            session = runtime.jarvis.start_continuous_task(
                goal="watch me", decomposed_steps=_steps(2)
            )
            runtime.start_autonomous(session.session_id)
            await _drain_to(runtime, session.session_id)
            wanted = {"autonomous_started", "subtask_started", "subtask_completed",
                      "task_completed", "autonomous_stopped"}
            await _until(lambda: wanted <= {e["event"] for e in seen})
            pump.cancel()
            assert wanted <= {e["event"] for e in seen}
            await runtime.shutdown()

        asyncio.run(body())


class TestRouting:
    """§7, §14, §35, §4 — through the orchestrator that nothing used to build."""

    async def _running(self, steps=200):
        """A session that is genuinely mid-flight, and stays that way.

        `gap=TICK` matters: with no router attached a step is a simulation, so
        without pacing the plan would be over before the first question is asked
        and every §14 assertion here would be testing a finished task.
        """
        runtime = _fast(VoiceRuntime(), gap=TICK)
        await runtime.ensure_started()
        session = runtime.jarvis.start_continuous_task(
            goal="deploy the app", decomposed_steps=_steps(steps)
        )
        runtime.register_bubble(session.session_id)
        runtime.start_autonomous(session.session_id)
        await _wait_steps(runtime, session.session_id, 1)
        return runtime, session

    def test_a_status_question_is_answered_and_the_plan_is_untouched(self):
        """§14. This used to route to the ImprovisationEngine: asking a question
        about the task rewrote the task."""
        async def body():
            runtime, session = await self._running()
            before = list(runtime.jarvis.active_sessions[session.session_id].subtasks)
            decision = await runtime.route_async("what are you doing", session.session_id)
            assert decision.action is RoutingAction.ANSWER_CONVERSATIONAL_QUERY
            assert decision.spoken_answer
            assert "step" in decision.spoken_answer
            after = runtime.jarvis.active_sessions[session.session_id].subtasks
            assert [n.id for n in after] == [n.id for n in before]
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    def test_execution_keeps_moving_while_the_question_is_answered(self):
        async def body():
            runtime, session = await self._running()
            first = await runtime.route_async("what are you doing", session.session_id)
            steps_then = runtime.runner_status(session.session_id)["steps_executed"]
            await _wait_steps(runtime, session.session_id, steps_then + 2)
            second = await runtime.route_async("how far along are you", session.session_id)
            assert first.spoken_answer != second.spoken_answer
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    @pytest.mark.parametrize("utterance", ["yeah", "mm-hmm", "okay", "right"])
    def test_a_backchannel_is_suppressed(self, utterance):
        """§7. Worse than becoming a task: it became a *confirmation* of one, so a
        grunt could approve a gated step."""
        async def body():
            runtime, session = await self._running()
            decision = await runtime.route_async(utterance, session.session_id)
            assert decision.suppressed is True
            assert "backchannel" in decision.reason
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    def test_a_control_phrase_pauses_the_task(self):
        async def body():
            runtime, session = await self._running()
            decision = await runtime.route_async("stop", session.session_id)
            assert decision.action is RoutingAction.EXECUTE_BARGE_IN_CONTROL
            assert runtime.jarvis.active_sessions[session.session_id].status == "paused"
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())

    def test_routing_stays_inside_the_latency_budget(self):
        async def body():
            runtime, session = await self._running()
            decision = await runtime.route_async("what are you doing", session.session_id)
            assert decision.latency_ms < 800
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())


class TestBargeInForwarding:
    """VFL filters ambient noise before anything downstream sees it."""

    def test_low_confidence_audio_is_discarded(self):
        async def body():
            runtime = _fast(VoiceRuntime())
            await runtime.ensure_started()
            seen, pump = _collect(runtime)
            await runtime.vfl.handle_audio_event(
                AudioEvent(wake_confidence=0.42, utterance="mumble", session_id="s")
            )
            await asyncio.sleep(3 * TICK)
            pump.cancel()
            assert not [e for e in seen if e["event"] == "barge_in_confirmed"]
            await runtime.shutdown()

        asyncio.run(body())

    def test_confident_audio_is_announced(self):
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="busy", decomposed_steps=_steps(200)
            )
            runtime.register_bubble(session.session_id)
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 1)
            seen, pump = _collect(runtime)
            await runtime.vfl.handle_audio_event(
                AudioEvent(wake_confidence=0.91, utterance="what are you doing",
                           session_id=session.session_id)
            )
            await _until(lambda: [e for e in seen if e["event"] == "barge_in_confirmed"])
            pump.cancel()
            assert [e for e in seen if e["event"] == "barge_in_confirmed"]
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())


# ── status text and the registry ───────────────────────────────────────────

class TestStatusAnswer:
    def test_it_reports_the_running_step(self):
        runtime = VoiceRuntime()
        session = runtime.jarvis.start_continuous_task(
            goal="g", decomposed_steps=_steps(4)
        )
        session.current_step_index = 2
        assert task_status_answer(session) == "Step 2 — step 3 of 4."

    def test_it_reports_a_pause(self):
        runtime = VoiceRuntime()
        session = runtime.jarvis.start_continuous_task(
            goal="g", decomposed_steps=_steps(4)
        )
        session.status = "paused"
        assert task_status_answer(session).startswith("Paused at step 1 of 4")

    def test_it_reports_completion_without_indexing_past_the_end(self):
        runtime = VoiceRuntime()
        session = runtime.jarvis.start_continuous_task(
            goal="g", decomposed_steps=_steps(2)
        )
        session.current_step_index = 2
        session.status = "completed"
        assert task_status_answer(session) == "Finished: g."


class TestRegistry:
    def test_get_runtime_is_a_singleton(self):
        assert get_runtime() is get_runtime()

    def test_it_adopts_the_engine_it_is_given(self):
        from backend.agent_modules.continuous_jarvis_engine import (
            ContinuousVoiceJarvisEngine,
        )

        engine = ContinuousVoiceJarvisEngine()
        runtime = VoiceRuntime(jarvis_engine=engine)
        # A runtime that quietly built its own engine would make every session
        # created through /api/jarvis/* invisible to the runner.
        assert runtime.jarvis is engine

    def test_the_snapshot_describes_live_work(self):
        async def body():
            runtime = _fast(VoiceRuntime(), gap=TICK)
            await runtime.ensure_started()
            session = runtime.jarvis.start_continuous_task(
                goal="snapshot me", decomposed_steps=_steps(200)
            )
            runtime.start_autonomous(session.session_id)
            await _wait_steps(runtime, session.session_id, 1)
            snap = runtime.snapshot()
            entry = next(s for s in snap["sessions"] if s["session_id"] == session.session_id)
            assert entry["autonomous"] is True
            assert entry["total_steps"] == 200
            assert snap["runners"]
            runtime.stop_autonomous(session.session_id)
            await runtime.shutdown()

        asyncio.run(body())
