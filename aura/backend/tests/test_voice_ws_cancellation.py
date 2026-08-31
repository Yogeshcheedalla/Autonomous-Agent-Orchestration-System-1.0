"""
Tests for the `voice_ws` cancellation registry (§20, §34).
==========================================================

`test_voice_ws.py` covers the socket: a frame in, the right frames out. This
file covers the thing sitting behind the socket that no frame reveals — the map
of in-flight executor work, and whether a directive actually reaches it.

Two defects live in this seam, and neither is visible from the wire:

  * `cancel_execution` used to be a frame the client received and nothing else.
    The generation or automation it named carried on, kept producing `speak`
    directives, and the user's "stop" only stopped the audio already queued. A
    test that asserts a `stop_tts` frame arrived passes in both worlds.
  * a *pause* used to end autonomous execution for good, because pause and
    cancel went down the same path. Resuming then had nothing to resume.

So these tests reach into `_Connection` and assert on `_work` and `_stop_flags`
directly. Both are load-bearing: `_work` holds tasks that `Task.cancel()` can
reach, `_stop_flags` holds the cooperative flags for blocking automation running
in a worker thread, which `Task.cancel()` cannot reach at all.

The socket is a stub. Nothing here connects, and no executor does real work — the
question is only which units of work survive which directive.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Dict, List

import pytest

from backend import voice_ws
from backend.voice_kernel import Directive, DirectiveKind, Event, EventKind, VoiceSession


class _StubSocket:
    """Just enough WebSocket for `_Connection`: it only ever sends text."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def make_conn() -> voice_ws._Connection:
    return voice_ws._Connection(_StubSocket(), VoiceSession("cancel-test"))


def generate(node: str | None = None) -> Directive:
    return Directive(DirectiveKind.GENERATE_RESPONSE, {"context": {"messages": []}})


def plan(node: str | None = "n1") -> Directive:
    return Directive(DirectiveKind.EXECUTE_PLAN, {"node_id": node, "description": "open notepad"})


@pytest.fixture()
def parked(monkeypatch):
    """An executor whose work never finishes on its own.

    Every task therefore stays in `_work` until something cancels it, which is
    exactly the state these assertions need. It records the flags it was handed so
    a test can prove a *pause* set one without the task dying.
    """
    started = asyncio.Event()
    handed: List[threading.Event] = []

    async def executor(session, directive):
        flag = directive.payload.get("stop_flag")
        if isinstance(flag, threading.Event):
            handed.append(flag)
        started.set()
        await asyncio.sleep(3600)
        return []

    monkeypatch.setattr(voice_ws, "_executor", executor)
    executor.started = started  # type: ignore[attr-defined]
    executor.handed = handed  # type: ignore[attr-defined]
    return executor


class TestWorkKeys:
    """What a directive is *called* decides what a later cancel can reach."""

    def test_generation_is_a_singleton(self):
        # Not keyed by turn: a second generation means the user said something
        # new, so the first one is answering a superseded question. Two live
        # generations interleave two answers into one voice.
        assert voice_ws._Connection._work_key(generate()) == "response"

    def test_a_plan_step_is_keyed_by_node(self):
        assert voice_ws._Connection._work_key(plan("n7")) == "node:n7"

    def test_a_plan_with_no_node_falls_back_to_a_shared_key(self):
        assert voice_ws._Connection._work_key(plan(None)) == "plan"

    def test_compression_has_its_own_key(self):
        # Sharing "response" would make every barge-in also abort compression,
        # and §12 would never complete in a conversation that is actually used.
        assert (
            voice_ws._Connection._work_key(Directive(DirectiveKind.COMPRESS_CONTEXT))
            == "compress"
        )


class TestRegistry:
    def test_starting_work_registers_a_task_and_a_flag(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.wait_for(parked.started.wait(), 2)
            assert set(conn._work) == {"node:n1"}
            assert set(conn._stop_flags) == {"node:n1"}
            assert not conn._work["node:n1"].done()
            await conn.close()

        asyncio.run(body())

    def test_a_second_generation_supersedes_the_first(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([generate()])
            await asyncio.wait_for(parked.started.wait(), 2)
            first = conn._work["response"]
            await conn.emit([generate()])
            await asyncio.sleep(0.05)
            assert first.cancelled() or first.done(), "the superseded answer kept generating"
            assert conn._work["response"] is not first
            assert not conn._work["response"].done()
            await conn.close()

        asyncio.run(body())

    def test_two_different_nodes_run_side_by_side(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1"), plan("n2")])
            await asyncio.sleep(0.05)
            assert set(conn._work) == {"node:n1", "node:n2"}
            await conn.close()

        asyncio.run(body())

    def test_finished_work_removes_itself_from_the_registry(self, monkeypatch):
        # Otherwise the maps grow for the life of the socket, and a later cancel
        # finds a completed task where it expected live work.
        async def executor(session, directive):
            return []

        monkeypatch.setattr(voice_ws, "_executor", executor)

        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.sleep(0.05)
            assert conn._work == {}
            assert conn._stop_flags == {}
            await conn.close()

        asyncio.run(body())

    def test_no_executor_means_the_directive_is_only_forwarded(self, monkeypatch):
        # The pre-existing UI drives `/api/voice/chat` and does the work itself,
        # so an unregistered executor must forward rather than swallow.
        monkeypatch.setattr(voice_ws, "_executor", None)

        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            assert conn._work == {}
            assert conn._out.qsize() == 1
            assert conn._out.get_nowait()["directive"] == "execute_plan"

        asyncio.run(body())


class TestCancellation:
    def test_cancel_execution_stops_the_named_node(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.wait_for(parked.started.wait(), 2)
            task = conn._work["node:n1"]
            flag = conn._stop_flags["node:n1"]
            await conn.emit([Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": ["n1"]})])
            await asyncio.sleep(0.05)
            assert task.cancelled(), "the automation task outlived the cancel"
            assert flag.is_set(), "blocking automation was never told to stop"
            assert "node:n1" not in conn._work
            await conn.close()

        asyncio.run(body())

    def test_cancel_execution_also_stops_the_answer(self, parked):
        # A cancellation is also "stop answering". Otherwise the model keeps
        # streaming sentences into a turn the user just abandoned.
        async def body():
            conn = make_conn()
            await conn.emit([generate(), plan("n1")])
            await asyncio.sleep(0.05)
            response = conn._work["response"]
            await conn.emit([Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": ["n1"]})])
            await asyncio.sleep(0.05)
            assert response.cancelled()
            await conn.close()

        asyncio.run(body())

    def test_cancel_execution_with_no_nodes_still_stops_the_unkeyed_plan(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([plan(None)])
            await asyncio.sleep(0.05)
            task = conn._work["plan"]
            await conn.emit([Directive(DirectiveKind.CANCEL_EXECUTION, {})])
            await asyncio.sleep(0.05)
            assert task.cancelled()
            await conn.close()

        asyncio.run(body())

    def test_stop_tts_stops_the_answer_but_not_the_task(self, parked):
        """§4. A barge-in cuts the audio, so the rest of the reply has nothing
        left to be spoken *as* — but the file it was in the middle of opening is
        still worth opening."""
        async def body():
            conn = make_conn()
            await conn.emit([generate(), plan("n1")])
            await asyncio.sleep(0.05)
            response = conn._work["response"]
            node = conn._work["node:n1"]
            await conn.emit([Directive(DirectiveKind.STOP_TTS, {"reason": "user cut in"})])
            await asyncio.sleep(0.05)
            assert response.cancelled(), "the superseded reply kept streaming"
            assert not node.done(), "a barge-in must not abandon the running task"
            assert not conn._stop_flags["node:n1"].is_set()
            await conn.close()

        asyncio.run(body())

    def test_cancelling_nothing_is_harmless(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": ["ghost"]})])
            assert conn._work == {}
            await conn.close()

        asyncio.run(body())


class TestPauseIsNotCancel:
    def test_pause_sets_the_flag_and_leaves_the_task_alive(self, parked):
        """The §34 regression.

        Pause and cancel used to share a path, so pausing retired the runner and
        there was nothing left to resume. The flag stops the step *loop* from
        taking another step; the task stays so a resume has a runner to resume.
        """
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.wait_for(parked.started.wait(), 2)
            task = conn._work["node:n1"]
            await conn.emit([Directive(DirectiveKind.PAUSE_EXECUTION, {"nodes": ["n1"]})])
            await asyncio.sleep(0.05)
            assert not task.done(), "a pause killed the task"
            assert conn._stop_flags["node:n1"].is_set(), "the step loop was never paused"
            assert "node:n1" in conn._work
            await conn.close()

        asyncio.run(body())

    def test_pause_does_not_touch_the_answer(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([generate(), plan("n1")])
            await asyncio.sleep(0.05)
            response = conn._work["response"]
            await conn.emit([Directive(DirectiveKind.PAUSE_EXECUTION, {"nodes": ["n1"]})])
            await asyncio.sleep(0.05)
            assert not response.done(), "pausing a task must not cut the sentence"
            await conn.close()

        asyncio.run(body())


class TestOrdering:
    def test_cancellation_lands_before_new_work_from_the_same_batch(self, parked):
        """One `handle()` can return a cancel *and* a fresh plan.

        Emitting in list order without applying the cancel first would let the
        new task be created and then immediately killed by the older directive.
        """
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.sleep(0.05)
            old = conn._work["node:n1"]
            await conn.emit([
                Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": ["n1"]}),
                plan("n1"),
            ])
            await asyncio.sleep(0.05)
            assert old.cancelled()
            assert "node:n1" in conn._work, "the replacement plan was cancelled too"
            assert not conn._work["node:n1"].done()
            await conn.close()

        asyncio.run(body())

    def test_every_directive_is_forwarded_even_when_it_also_cancels(self, parked):
        # The client still has to know: it owns the audio element that a
        # `stop_tts` refers to.
        async def body():
            conn = make_conn()
            await conn.emit([generate()])
            await asyncio.sleep(0.05)
            await conn.emit([Directive(DirectiveKind.STOP_TTS, {"reason": "barge-in"})])
            frames = [conn._out.get_nowait()["directive"] for _ in range(conn._out.qsize())]
            assert "stop_tts" in frames
            await conn.close()

        asyncio.run(body())


class TestClose:
    def test_closing_stops_every_blocking_worker(self, parked):
        """The socket is going away; the worker threads are not.

        `close()` cancels tasks, but a thread already inside pyautogui does not
        notice a cancelled task. If the flags are not set first, automation keeps
        driving the real mouse after the conversation has ended.
        """
        async def body():
            conn = make_conn()
            await conn.emit([generate(), plan("n1"), plan("n2")])
            await asyncio.sleep(0.05)
            flags = list(conn._stop_flags.values())
            assert len(flags) == 3
            await conn.close()
            assert all(f.is_set() for f in flags), "a worker thread was left running"
            assert conn._work == {}
            assert conn._stop_flags == {}

        asyncio.run(body())

    def test_closing_twice_is_safe(self, parked):
        async def body():
            conn = make_conn()
            await conn.emit([plan("n1")])
            await asyncio.sleep(0.05)
            await conn.close()
            await conn.close()

        asyncio.run(body())

    def test_a_closed_connection_sends_nothing_further(self, parked):
        async def body():
            conn = make_conn()
            await conn.close()
            await conn.send({"directive": "speak", "text": "still here"})
            # `close()` leaves its own `None` sentinel on the queue for the writer
            # to see; what must not be there is the refused frame.
            queued = [conn._out.get_nowait() for _ in range(conn._out.qsize())]
            assert queued == [None]

        asyncio.run(body())


class TestExecutorFailures:
    def test_an_executor_that_raises_reports_an_error_and_frees_the_slot(self, monkeypatch):
        async def executor(session, directive):
            raise RuntimeError("model gateway refused")

        monkeypatch.setattr(voice_ws, "_executor", executor)

        async def body():
            conn = make_conn()
            await conn.emit([generate()])
            await asyncio.sleep(0.05)
            assert conn._work == {}, "a crashed executor held its slot forever"
            frames = [conn._out.get_nowait() for _ in range(conn._out.qsize())]
            assert any(
                "model gateway refused" in json.dumps(f) for f in frames
            ), f"the failure never reached the client: {[f.get('directive') for f in frames]}"
            await conn.close()

        asyncio.run(body())

    def test_a_streaming_executor_dispatches_each_event_as_it_arrives(self, monkeypatch):
        """§19: the reason `Executor` accepts an async generator at all.

        The kernel has to see token one before token two exists, or its
        sentence-boundary draining fires all at once at the end and the whole
        point of streaming is lost.
        """
        seen: List[int] = []

        async def executor(session, directive):
            for i, word in enumerate(("Opening ", "the ", "file.")):
                seen.append(i)
                yield Event(EventKind.RESPONSE_TOKEN, {"text": word})
            yield Event(EventKind.RESPONSE_COMPLETE, {"text": "Opening the file."})

        monkeypatch.setattr(voice_ws, "_executor", executor)

        async def body():
            conn = make_conn()
            await conn.emit([generate()])
            await asyncio.sleep(0.05)
            assert seen == [0, 1, 2]
            frames = [conn._out.get_nowait() for _ in range(conn._out.qsize())]
            assert any(f.get("directive") == "speak" for f in frames), (
                "no speech came out of a completed generation: "
                f"{[f.get('directive') for f in frames]}"
            )
            await conn.close()

        asyncio.run(body())

    def test_an_awaitable_executor_still_works(self, monkeypatch):
        # The non-streaming shape stays supported for fire-and-report work.
        async def executor(session, directive):
            return [Event(EventKind.RESPONSE_COMPLETE, {"text": "Done."})]

        monkeypatch.setattr(voice_ws, "_executor", executor)

        async def body():
            conn = make_conn()
            await conn.emit([generate()])
            await asyncio.sleep(0.05)
            frames = [conn._out.get_nowait() for _ in range(conn._out.qsize())]
            assert any(f.get("directive") == "speak" for f in frames)
            await conn.close()

        asyncio.run(body())
