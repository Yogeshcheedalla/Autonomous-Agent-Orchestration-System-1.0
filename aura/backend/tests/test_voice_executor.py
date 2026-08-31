"""
Tests for `backend.voice_executor` — the half of the voice loop that does I/O.
=============================================================================

The bug this module was written to fix is a registration bug, not a logic bug:
`voice_ws.set_executor` existed and documented itself, and nothing in the product
ever called it, so `/ws/voice/{session_id}` forwarded `generate_response` and
`execute_plan` to a client that does not implement them. So the first test here
asserts the wiring, not behaviour — a green logic suite over an unregistered
executor is exactly the failure mode that shipped.

Everything else covers the three directives, and specifically the properties that
are invisible until something goes wrong:

  * §19 wants sentence one spoken while sentence three is still generating, so
    tokens must be *yielded as they arrive*, not accumulated and returned.
  * a barge-in must not let the superseded answer finish speaking;
  * a stalled model must end the turn rather than leave the user in silence;
  * §12's compression is a transaction, and a summariser that throws has to put
    the batch back — otherwise "compress the oldest turns" becomes "delete them".

No test here reaches the network or pyautogui. `_run_automation` is replaced
wholesale in the plan tests: the real one drives the actual mouse.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional

import pytest

from backend import voice_executor, voice_ws
from backend.voice_kernel import Directive, DirectiveKind, EventKind, VoiceSession
from backend.voice_kernel.context import Turn


# ── fakes ────────────────────────────────────────────────────────────────────
class _Delta:
    def __init__(self, content: Optional[str]) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: Optional[str]) -> None:
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content: Optional[str]) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    """Stands in for `client.chat.completions`.

    `scripts` maps a model name to what it does: a list of strings streams them
    as chunks, an Exception instance is raised at `create` time (a dead route).
    """

    def __init__(self, scripts: Dict[str, Any]) -> None:
        self.scripts = scripts
        self.calls: List[str] = []

    def create(self, *, model: str, messages: Any, stream: bool = False, **_: Any) -> Any:
        self.calls.append(model)
        script = self.scripts.get(model, RuntimeError(f"no route for {model}"))
        if isinstance(script, BaseException):
            raise script
        if not stream:
            class _Msg:
                def __init__(self, text: str) -> None:
                    self.message = type("M", (), {"content": text})()

            return type("R", (), {"choices": [_Msg("".join(script))]})()
        return [_Chunk(part) for part in script]


class _Client:
    def __init__(self, scripts: Dict[str, Any]) -> None:
        self.chat = type("C", (), {"completions": _Completions(scripts)})()

    @property
    def completions(self) -> _Completions:
        return self.chat.completions


@pytest.fixture()
def session() -> VoiceSession:
    return VoiceSession("exec-test")


def prompt_directive(session: VoiceSession, **extra: Any) -> Directive:
    """A `generate_response` carrying exactly what the kernel would put in it."""
    return Directive(DirectiveKind.GENERATE_RESPONSE, {**session.build_prompt(), **extra})


async def drain(agen) -> List[Any]:
    return [event async for event in agen]


# ── the registration this module exists to perform ───────────────────────────
class TestInstallation:
    def test_install_registers_the_executor_with_the_transport(self, monkeypatch):
        # The original defect: `set_executor` was never called, so the kernel's
        # actionable directives were forwarded to a client that ignores them.
        monkeypatch.setattr(voice_ws, "_executor", None)
        assert voice_ws._executor is None
        voice_executor.install()
        assert voice_ws._executor is voice_executor.execute

    def test_install_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(voice_ws, "_executor", None)
        voice_executor.install()
        voice_executor.install()
        assert voice_ws._executor is voice_executor.execute

    def test_the_app_installs_it_on_startup(self, monkeypatch):
        """The wiring is only real if something calls `install()` at boot.

        Asserting on the startup hook rather than on `install` itself, because a
        correct `install` that no one invokes is the exact bug that shipped.
        """
        monkeypatch.setattr(voice_ws, "_executor", None)
        from backend.main import _install_voice_executor

        asyncio.run(_install_voice_executor())
        assert voice_ws._executor is voice_executor.execute

    def test_unknown_directives_are_ignored_rather_than_raising(self, session):
        # `execute` is handed every ACTIONABLE directive; a kind it does not
        # implement must be a no-op, not an exception that kills the socket task.
        events = asyncio.run(
            drain(voice_executor.execute(session, Directive(DirectiveKind.SPEAK, {"text": "hi"})))
        )
        assert events == []


# ── §19: streaming generation ────────────────────────────────────────────────
class TestGenerateResponse:
    def test_tokens_are_yielded_as_they_arrive_not_batched(self, session, monkeypatch):
        """The property §19 depends on.

        A `response_token` must reach the caller before the next one is produced.
        The fake pushes one token, waits for this test to observe it, and only
        then pushes the second — so if the executor accumulated and returned at
        the end, this deadlocks and fails on the timeout rather than passing.
        """
        seen_first = threading.Event()

        def fake_stream(queue, loop, messages, model, stop):
            loop.call_soon_threadsafe(queue.put_nowait, ("token", "Opening it"))
            assert seen_first.wait(5), "first token never observed downstream"
            loop.call_soon_threadsafe(queue.put_nowait, ("token", " now."))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        monkeypatch.setattr(voice_executor, "_stream_into", fake_stream)

        async def body():
            out = []
            async for event in voice_executor.execute(session, prompt_directive(session)):
                out.append(event)
                if len(out) == 1:
                    seen_first.set()
            return out

        events = asyncio.run(asyncio.wait_for(body(), timeout=10))
        kinds = [e.kind for e in events]
        assert kinds == [
            EventKind.RESPONSE_TOKEN,
            EventKind.RESPONSE_TOKEN,
            EventKind.RESPONSE_COMPLETE,
        ]
        assert events[0].payload["text"] == "Opening it"
        assert events[-1].payload["text"] == "Opening it now."

    def test_an_empty_context_does_not_call_a_model(self, session, monkeypatch):
        called = []
        monkeypatch.setattr(
            voice_executor, "_stream_into", lambda *a, **k: called.append(1)
        )
        directive = Directive(DirectiveKind.GENERATE_RESPONSE, {"context": {"messages": []}})
        events = asyncio.run(drain(voice_executor.execute(session, directive)))
        assert [e.kind for e in events] == [EventKind.RESPONSE_COMPLETE]
        assert called == []

    def test_a_barge_in_does_not_speak_the_superseded_answer(self, session, monkeypatch):
        """A cancelled generation must complete nothing.

        The kernel has already reset its response buffer for the new turn, so a
        `response_complete` here would be spoken as the answer to a question the
        user has moved on from.
        """
        def fake_stream(queue, loop, messages, model, stop):
            loop.call_soon_threadsafe(queue.put_nowait, ("token", "The first thing"))
            loop.call_soon_threadsafe(queue.put_nowait, ("cancelled", None))

        monkeypatch.setattr(voice_executor, "_stream_into", fake_stream)
        events = asyncio.run(drain(voice_executor.execute(session, prompt_directive(session))))
        kinds = [e.kind for e in events]
        assert EventKind.RESPONSE_COMPLETE not in kinds
        assert kinds == [EventKind.RESPONSE_TOKEN]

    def test_the_stop_flag_is_set_when_the_generator_is_abandoned(self, session, monkeypatch):
        """Whoever stops reading must stop the worker thread too.

        `_stream_into` runs in an executor thread that no `Task.cancel()` reaches;
        the only thing that stops it is the flag, and it is the caller's exit from
        the `async for` that has to set it.
        """
        def fake_stream(queue, loop, messages, model, stop):
            for i in range(50):
                if stop.is_set():
                    return
                loop.call_soon_threadsafe(queue.put_nowait, ("token", f"word{i} "))

        monkeypatch.setattr(voice_executor, "_stream_into", fake_stream)
        flag = threading.Event()

        async def body():
            directive = prompt_directive(session, stop_flag=flag)
            agen = voice_executor.execute(session, directive)
            async for _ in agen:
                break
            await agen.aclose()

        asyncio.run(body())
        assert flag.is_set()

    def test_a_stalled_model_ends_the_turn_instead_of_going_silent(self, session, monkeypatch):
        monkeypatch.setattr(voice_executor, "FIRST_TOKEN_TIMEOUT_S", 0.05)
        monkeypatch.setattr(voice_executor, "_stream_into", lambda *a, **k: None)
        events = asyncio.run(drain(voice_executor.execute(session, prompt_directive(session))))
        assert [e.kind for e in events] == [EventKind.STT_ERROR]
        assert "did not respond" in events[0].payload["error"]

    def test_a_stall_after_some_speech_finishes_the_sentence(self, session, monkeypatch):
        """Half an answer already reached the user's ears.

        Reporting an error now would contradict what they just heard, so the turn
        is closed with what was actually said.
        """
        monkeypatch.setattr(voice_executor, "TOKEN_TIMEOUT_S", 0.05)

        def fake_stream(queue, loop, messages, model, stop):
            loop.call_soon_threadsafe(queue.put_nowait, ("token", "I found two files"))

        monkeypatch.setattr(voice_executor, "_stream_into", fake_stream)
        events = asyncio.run(drain(voice_executor.execute(session, prompt_directive(session))))
        assert [e.kind for e in events] == [
            EventKind.RESPONSE_TOKEN,
            EventKind.RESPONSE_COMPLETE,
        ]
        assert events[-1].payload["text"] == "I found two files"

    def test_a_failure_with_nothing_said_is_reported_as_an_error(self, session, monkeypatch):
        def fake_stream(queue, loop, messages, model, stop):
            loop.call_soon_threadsafe(queue.put_nowait, ("error", "all routes down"))

        monkeypatch.setattr(voice_executor, "_stream_into", fake_stream)
        events = asyncio.run(drain(voice_executor.execute(session, prompt_directive(session))))
        assert [e.kind for e in events] == [EventKind.ERROR]
        assert events[0].payload["source"] == "model"


class TestStreamInto:
    """The worker-thread half: model failover and speakability filtering."""

    def run(self, scripts, model=None, candidates=("primary", "backup")):
        client = _Client(scripts)
        stop = threading.Event()
        import backend.ai_engine as ai

        failures: List[str] = []

        async def body():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            await loop.run_in_executor(
                None, voice_executor._stream_into, queue, loop, [{"role": "user", "content": "hi"}], model, stop
            )
            await asyncio.sleep(0)  # let call_soon_threadsafe callbacks land
            out = []
            while not queue.empty():
                out.append(queue.get_nowait())
            return out

        original = (ai._openrouter_client, ai._voice_model_candidates, ai.note_model_failure)
        ai._openrouter_client = lambda: client
        ai._voice_model_candidates = lambda: list(candidates)
        ai.note_model_failure = lambda m, e: failures.append(m)
        try:
            return client, failures, asyncio.run(body())
        finally:
            ai._openrouter_client, ai._voice_model_candidates, ai.note_model_failure = original

    def test_a_dead_route_falls_through_to_the_next_candidate(self):
        client, failures, out = self.run(
            {"primary": RuntimeError("402 payment required"), "backup": ["All ", "set."]}
        )
        assert client.completions.calls == ["primary", "backup"]
        assert failures == ["primary"], "a structurally dead route must be demoted"
        assert [k for k, _ in out] == ["token", "token", "done"]

    def test_a_model_that_says_nothing_speakable_is_treated_as_a_failure(self):
        # A route that streams only reasoning and no prose has failed; the next
        # candidate should get a turn instead of the user hearing the reasoning.
        client, _, out = self.run(
            {
                "primary": ["<think>", "the user wants a file", "</think>"],
                "backup": ["Here it is."],
            }
        )
        assert client.completions.calls == ["primary", "backup"]
        assert [k for k, _ in out] == ["token", "done"]
        assert out[0][1].strip() == "Here it is."

    def test_an_explicit_model_is_tried_first(self):
        client, _, _ = self.run({"chosen": ["Yes."]}, model="chosen")
        assert client.completions.calls[0] == "chosen"

    def test_every_route_failing_reports_an_error_not_silence(self):
        _, _, out = self.run(
            {"primary": RuntimeError("boom"), "backup": RuntimeError("also boom")}
        )
        assert [k for k, _ in out] == ["error"]

    def test_the_stop_flag_is_honoured_before_a_request_is_made(self):
        client = _Client({"primary": ["never"]})
        stop = threading.Event()
        stop.set()
        import backend.ai_engine as ai

        original = (ai._openrouter_client, ai._voice_model_candidates)
        ai._openrouter_client = lambda: client
        ai._voice_model_candidates = lambda: ["primary"]
        try:
            async def body():
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue()
                await loop.run_in_executor(
                    None, voice_executor._stream_into, queue, loop, [{"role": "user", "content": "x"}], None, stop
                )
                await asyncio.sleep(0)
                return queue.get_nowait()

            kind, _ = asyncio.run(body())
        finally:
            ai._openrouter_client, ai._voice_model_candidates = original
        assert kind == "cancelled"
        assert client.completions.calls == [], "a cancelled turn must not cost a request"


# ── §16/§40: automation ──────────────────────────────────────────────────────
class TestExecutePlan:
    def plan(self, session, **payload):
        return Directive(
            DirectiveKind.EXECUTE_PLAN,
            {"node_id": "n1", "description": "open notepad", **payload},
        )

    def test_a_successful_step_reports_started_then_completed(self, session, monkeypatch):
        monkeypatch.setattr(
            voice_executor,
            "_run_automation",
            lambda prompt, stop: {"success": True, "summary": "Notepad is open.", "steps": []},
        )
        events = asyncio.run(drain(voice_executor.execute(session, self.plan(session))))
        kinds = [e.kind for e in events]
        assert kinds[0] is EventKind.STEP_STARTED
        assert EventKind.STEP_COMPLETED in kinds
        completed = next(e for e in events if e.kind is EventKind.STEP_COMPLETED)
        assert completed.payload["node_id"] == "n1"
        assert completed.payload["summary"] == "Notepad is open."

    def test_a_step_with_no_description_fails_without_touching_the_desktop(
        self, session, monkeypatch
    ):
        monkeypatch.setattr(
            voice_executor,
            "_run_automation",
            lambda *a: pytest.fail("automation must not run for an empty step"),
        )
        directive = Directive(DirectiveKind.EXECUTE_PLAN, {"node_id": "n1", "description": "  "})
        events = asyncio.run(drain(voice_executor.execute(session, directive)))
        assert [e.kind for e in events] == [EventKind.STEP_FAILED]

    def test_a_cancelled_step_is_not_reported_as_a_failure(self, session, monkeypatch):
        # The user asked for it, and the kernel has already spoken its own
        # "Stopped." acknowledgement at P0 — a step_failed here would make the
        # assistant apologise for doing what it was told.
        monkeypatch.setattr(
            voice_executor,
            "_run_automation",
            lambda prompt, stop: {"cancelled": True, "success": False, "summary": "Stopped."},
        )
        events = asyncio.run(drain(voice_executor.execute(session, self.plan(session))))
        assert [e.kind for e in events] == [EventKind.STEP_STARTED]

    def test_the_stop_flag_reaches_the_blocking_worker(self, session, monkeypatch):
        seen: List[threading.Event] = []

        def fake(prompt, stop):
            seen.append(stop)
            return {"success": True, "summary": "done", "steps": []}

        monkeypatch.setattr(voice_executor, "_run_automation", fake)
        flag = threading.Event()
        asyncio.run(drain(voice_executor.execute(session, self.plan(session, stop_flag=flag))))
        assert seen == [flag], "automation cannot be interrupted without the flag"

    def test_a_wedged_step_times_out_and_sets_the_flag(self, session, monkeypatch):
        monkeypatch.setattr(voice_executor, "STEP_TIMEOUT_S", 0.05)
        flag = threading.Event()
        started = threading.Event()

        def fake(prompt, stop):
            started.set()
            stop.wait(5)  # a modal dialog pyautogui will never dismiss
            return {"success": False, "summary": "gave up"}

        monkeypatch.setattr(voice_executor, "_run_automation", fake)
        events = asyncio.run(
            drain(voice_executor.execute(session, self.plan(session, stop_flag=flag)))
        )
        assert started.is_set()
        assert [e.kind for e in events] == [EventKind.STEP_STARTED, EventKind.STEP_FAILED]
        assert "timed out" in events[-1].payload["error"]
        assert flag.is_set(), "a timeout must tell the worker thread to stop"

    def test_a_clarification_is_surfaced_as_the_step_failure_reason(self, session, monkeypatch):
        monkeypatch.setattr(
            voice_executor,
            "_run_automation",
            lambda prompt, stop: {
                "success": False,
                "needs_clarification": True,
                "summary": "Which project did you mean?",
            },
        )
        events = asyncio.run(drain(voice_executor.execute(session, self.plan(session))))
        assert events[-1].kind is EventKind.STEP_FAILED
        assert events[-1].payload["error"] == "Which project did you mean?"

    def test_an_exception_in_automation_is_a_step_failure_not_a_dead_socket(
        self, session, monkeypatch
    ):
        def boom(prompt, stop):
            raise OSError("display not found")

        monkeypatch.setattr(voice_executor, "_run_automation", boom)
        events = asyncio.run(drain(voice_executor.execute(session, self.plan(session))))
        assert events[-1].kind is EventKind.STEP_FAILED
        assert "display not found" in events[-1].payload["error"]


# ── §12: context compression ─────────────────────────────────────────────────
def _fill_context(session: VoiceSession, count: int = 40) -> int:
    for i in range(count):
        session.context.add_turn(
            Turn(role="user" if i % 2 == 0 else "assistant", text=f"turn {i} with several words")
        )
    return len(session.context.turns)


class TestCompressContext:
    def test_a_batch_is_summarised_and_the_turns_are_replaced(self, session, monkeypatch):
        """The regression: this path raised `AttributeError` on every call.

        `_compress_context` read `Turn.content`; the field is `Turn.text`. Because
        the message list was built outside the try block, the exception escaped
        after `take_compression_batch()` had already removed the turns — so §12
        inverted itself and deleted a batch of history per attempt instead of
        compressing it.
        """
        before = _fill_context(session)
        monkeypatch.setattr(voice_executor, "_summarise", lambda messages: "user opened files")
        asyncio.run(drain(voice_executor.execute(session, Directive(DirectiveKind.COMPRESS_CONTEXT))))
        assert len(session.context.summaries) == 1
        assert session.context.summaries[0].text == "user opened files"
        assert len(session.context.turns) < before
        assert session.context.summaries[0].covers_turns == before - len(session.context.turns)

    def test_the_summariser_receives_the_turn_text(self, session, monkeypatch):
        _fill_context(session)
        captured: List[List[Dict[str, str]]] = []
        monkeypatch.setattr(
            voice_executor,
            "_summarise",
            lambda messages: captured.append(messages) or "notes",
        )
        asyncio.run(drain(voice_executor.execute(session, Directive(DirectiveKind.COMPRESS_CONTEXT))))
        assert captured, "the summariser was never called"
        assert all(m["content"] for m in captured[0]), "empty content means the wrong attribute"
        assert captured[0][0]["content"].startswith("turn 0")

    def test_a_failed_summary_puts_the_turns_back(self, session, monkeypatch):
        before = _fill_context(session)

        def boom(messages):
            raise RuntimeError("could not summarise the conversation")

        monkeypatch.setattr(voice_executor, "_summarise", boom)
        asyncio.run(drain(voice_executor.execute(session, Directive(DirectiveKind.COMPRESS_CONTEXT))))
        assert len(session.context.turns) == before, "history was lost by a failed compression"
        assert session.context.summaries == []

    def test_compression_yields_no_events(self, session, monkeypatch):
        # It mutates the context in place; the kernel reads that on the next
        # build(). Emitting an event here would push a phantom turn into the FSM.
        _fill_context(session)
        monkeypatch.setattr(voice_executor, "_summarise", lambda messages: "notes")
        events = asyncio.run(
            drain(voice_executor.execute(session, Directive(DirectiveKind.COMPRESS_CONTEXT)))
        )
        assert events == []

    def test_nothing_to_compress_is_a_no_op(self, session, monkeypatch):
        monkeypatch.setattr(
            voice_executor, "_summarise", lambda messages: pytest.fail("nothing to summarise")
        )
        events = asyncio.run(
            drain(voice_executor.execute(session, Directive(DirectiveKind.COMPRESS_CONTEXT)))
        )
        assert events == []
        assert session.context.summaries == []
