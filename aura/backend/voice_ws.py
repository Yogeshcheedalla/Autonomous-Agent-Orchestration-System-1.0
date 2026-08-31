"""
voice_ws — the WebSocket transport for the voice kernel (§1, §2, §4, §19).
=========================================================================

This is the first duplex channel in the backend. It exists because the old
voice path could not do barge-in: `/api/voice/chat` is a single POST, so the
server had no way to tell the client "stop speaking, the user cut in" until
the request it was already serving had finished. Everything §4 asks for is
impossible over one-shot HTTP.

Responsibilities, and deliberately nothing else:

  * accept `Event`s from the client, hand them to a long-lived `VoiceSession`
    from the registry, and stream back every `Directive` the kernel produced;
  * run the endpointing heartbeat (§6) server-side, so a partial transcript
    that stops arriving is still evaluated;
  * serialise all outbound frames through one queue, because the kernel is
    driven from two places (the reader loop and any executor task) and
    concurrent `send_json` on a single socket is undefined;
  * survive a dropped socket without destroying the plan (§37): a disconnect
    is not a `session_close`, and reconnecting to the same id resumes.

The kernel decides *what* should happen; actually doing it (LLM calls,
automation) is injected via `set_executor` so this module keeps the property
the kernel has — no I/O of its own beyond the socket. With no executor
registered, actionable directives are forwarded to the client, which is how
the existing UI already drives `/api/voice/chat`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .voice_kernel import Directive, DirectiveKind, Event, EventKind, VoiceSession, get_registry
from .voice_kernel.session import SessionConfig

log = logging.getLogger("akansha.voice_ws")

router = APIRouter(tags=["voice-kernel"])

#: Endpointing heartbeat. 120ms is well under the 240ms minimum-silence floor,
#: so the detector never overshoots its own resolution.
TICK_INTERVAL_S = 0.12

#: When the socket has been quiet for this long, send a small liveness frame.
#: Two reasons it exists: a client (or an intermediate proxy) needs to know the
#: socket is still alive, and the §24 3D core / §29 context meter want a state
#: refresh even when the conversation is idle.
KEEPALIVE_S = 1.0

#: A voice event is small. Anything larger is a bug or an attack.
MAX_MESSAGE_BYTES = 64_000

#: Directives that need real work done. Handed to the executor when one is
#: registered, otherwise forwarded to the client.
ACTIONABLE = (
    DirectiveKind.EXECUTE_PLAN,
    DirectiveKind.GENERATE_RESPONSE,
    DirectiveKind.COMPRESS_CONTEXT,
)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient", None, ""}

#: `async def executor(session, directive) -> Optional[Iterable[Event]]`, or an
#: async generator yielding `Event`s as they happen.
#:
#: Both shapes are accepted, and the generator one is the point. §19 wants the
#: first sentence spoken while the third is still being generated, which is
#: impossible if the executor can only hand back a list once it has finished:
#: every `response_token` would arrive after the whole reply existed, and the
#: kernel's sentence-boundary draining would fire all at once. So an executor
#: that streams should `yield`, and the awaitable form stays for the simple
#: fire-and-report cases.
Executor = Callable[
    [VoiceSession, Directive],
    "Awaitable[Optional[Iterable[Event]]] | AsyncIterator[Event]",
]
_executor: Optional[Executor] = None


def set_executor(fn: Optional[Executor]) -> None:
    """Register the side-effecting half of the loop. Pass None to unregister."""
    global _executor
    _executor = fn


# ── access control ──────────────────────────────────────────────────────────
def _authorised(websocket: WebSocket) -> tuple[bool, str]:
    """Gate the socket.

    A voice socket can cancel work, read the situation model and drive
    automation, so it is not something to leave open. Two accepted proofs:

      * `AKANSHA_VOICE_WS_TOKEN` is set and the client presents it — the only
        option that is safe once the backend is reachable off-box;
      * no token is configured *and* the peer is loopback, which is the
        single-user desktop case this app actually ships as.

    Anything else is refused rather than silently allowed.
    """
    expected = (os.getenv("AKANSHA_VOICE_WS_TOKEN") or "").strip()
    if expected:
        presented = (
            websocket.query_params.get("token")
            or websocket.headers.get("x-akansha-voice-token")
            or ""
        )
        if presented != expected:
            return False, "invalid voice token"
        return True, "token"
    host = getattr(websocket.client, "host", None)
    if host in _LOOPBACK:
        return True, "loopback"
    return False, "set AKANSHA_VOICE_WS_TOKEN to allow non-local voice sockets"


def _config_from_query(websocket: WebSocket) -> SessionConfig:
    q = websocket.query_params
    kwargs: Dict[str, Any] = {}
    if q.get("model"):
        kwargs["model"] = q["model"]
    if q.get("language"):
        kwargs["language"] = q["language"]
    if q.get("voice"):
        kwargs["voice"] = q["voice"]
    if q.get("mode"):
        kwargs["mode"] = q["mode"]
    wake = q.get("wake_word")
    if wake is not None:
        kwargs["require_wake_word"] = wake not in ("0", "false", "no", "")
    for name in ("speech_speed", "pitch", "volume"):
        if q.get(name):
            with contextlib.suppress(ValueError):
                kwargs[name] = float(q[name])
    return SessionConfig(**kwargs)


def _parse(raw: str) -> tuple[Optional[EventKind], Dict[str, Any], Optional[str]]:
    """Wire frame → (kind, payload, error)."""
    if len(raw) > MAX_MESSAGE_BYTES:
        return None, {}, "frame too large"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, {}, f"invalid json: {exc.msg}"
    if not isinstance(data, dict):
        return None, {}, "frame must be an object"
    name = data.get("event") or data.get("type") or data.get("kind")
    if not isinstance(name, str):
        return None, {}, "missing 'event'"
    try:
        kind = EventKind(name.strip().lower())
    except ValueError:
        return None, {}, f"unknown event {name!r}"
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {k: v for k, v in data.items() if k not in ("event", "type", "kind")}
    return kind, payload, None


class _Connection:
    """One socket bound to one kernel session.

    All outbound traffic goes through `_out`. The reader loop, the endpointing
    ticker and every executor task push directives onto it; exactly one writer
    task drains it. That is the only way to keep frame ordering meaningful
    while three producers exist.
    """

    def __init__(self, websocket: WebSocket, session: VoiceSession) -> None:
        self.ws = websocket
        self.session = session
        self._out: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=512)
        self._tasks: List[asyncio.Task[Any]] = []
        self._writer_task: Optional[asyncio.Task[Any]] = None
        self._closing = False
        self._last_sent_at = time.monotonic()
        #: In-flight executor work, keyed so it can be cancelled by name (§20).
        #: Without this, `cancel_execution` was a frame the client received and
        #: nothing else: the generation or automation it referred to carried on,
        #: kept emitting `speak` directives, and the user's "stop" only stopped
        #: the audio that had already been queued.
        self._work: Dict[str, asyncio.Task[Any]] = {}
        #: Cooperative stop flags handed to blocking automation. A `Task.cancel()`
        #: cannot interrupt a thread running pyautogui, so the step loop has to
        #: check a flag between steps and give up. Mid-step is not interruptible
        #: and pretending otherwise would be a lie about what "stop" does.
        self._stop_flags: Dict[str, threading.Event] = {}

    # ── outbound ──────────────────────────────────────────────────────────
    async def send(self, frame: Dict[str, Any]) -> None:
        if self._closing:
            return
        self._last_sent_at = time.monotonic()
        try:
            self._out.put_nowait(frame)
        except asyncio.QueueFull:
            # Dropping a frame is bad; blocking the kernel is worse. Progress
            # chatter is the only thing that can realistically flood, and it is
            # already rate-limited, so a drop here is a diagnostic, not a leak.
            log.warning("voice_ws: outbound queue full, dropped %s", frame.get("directive"))

    async def emit(self, directives: Iterable[Directive]) -> None:
        for directive in directives:
            await self.send(directive.as_dict())
            # Kill work before starting any, so a list that contains both a
            # cancellation and a fresh plan lands in that order.
            self._apply_cancellations(directive)
            if directive.kind in ACTIONABLE and _executor is not None:
                self._start_work(directive)

    # ── §20: cancellation ─────────────────────────────────────────────────
    @staticmethod
    def _work_key(directive: Directive) -> str:
        """Name the work a directive represents, so it can be cancelled later.

        `generate_response` is deliberately a singleton: a second one means the
        user said something new, and the previous reply is now the answer to a
        question that has been superseded. Letting both run interleaves two
        answers into one voice — the kernel resets its response buffer for the
        new turn, so stale tokens get spoken as if they belonged to the new one.
        """
        if directive.kind is DirectiveKind.GENERATE_RESPONSE:
            return "response"
        if directive.kind is DirectiveKind.COMPRESS_CONTEXT:
            return "compress"
        node = directive.payload.get("node_id")
        return f"node:{node}" if node else "plan"

    def _cancel_work(self, key: str) -> bool:
        """Cancel one named unit of work. Returns whether there was any."""
        flag = self._stop_flags.pop(key, None)
        if flag is not None:
            flag.set()
        task = self._work.pop(key, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _apply_cancellations(self, directive: Directive) -> None:
        if directive.kind is DirectiveKind.CANCEL_EXECUTION:
            for node in directive.payload.get("nodes") or ():
                self._cancel_work(f"node:{node}")
            self._cancel_work("plan")
            # A cancellation is also "stop answering": otherwise the model keeps
            # streaming sentences into a turn the user just abandoned.
            self._cancel_work("response")
        elif directive.kind is DirectiveKind.STOP_TTS:
            # §4. The audio is being cut off because the user cut in, so there is
            # nothing left for the rest of the reply to be spoken *as*.
            self._cancel_work("response")
        elif directive.kind is DirectiveKind.PAUSE_EXECUTION:
            # Pause is not cancel (§34, and the audit item that a pause used to
            # end autonomous execution for good), so the task is left alone. The
            # flag is set so a blocking step loop stops taking new steps and can
            # be resumed from its checkpoint.
            for node in directive.payload.get("nodes") or ():
                flag = self._stop_flags.get(f"node:{node}")
                if flag is not None:
                    flag.set()

    def _start_work(self, directive: Directive) -> None:
        key = self._work_key(directive)
        self._cancel_work(key)
        flag = threading.Event()
        self._stop_flags[key] = flag
        task = asyncio.ensure_future(self._run_executor(directive, flag))
        self._work[key] = task
        self._tasks.append(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            if finished in self._tasks:
                self._tasks.remove(finished)
            if self._work.get(key) is finished:
                self._work.pop(key, None)
                self._stop_flags.pop(key, None)

        task.add_done_callback(_done)

    async def _writer(self) -> None:
        while True:
            frame = await self._out.get()
            if frame is None:
                return
            try:
                await self.ws.send_text(json.dumps(frame, default=str))
            except (WebSocketDisconnect, RuntimeError):
                return

    # ── inbound ───────────────────────────────────────────────────────────
    async def dispatch(self, kind: EventKind, payload: Dict[str, Any]) -> None:
        """Feed one event to the kernel and stream what it decided.

        `VoiceSession.handle` is documented never to raise, but this is the
        process boundary: if that promise is ever broken the socket must report
        it rather than die silently.
        """
        try:
            directives = self.session.handle(Event(kind, payload))
        except Exception:  # pragma: no cover - defence in depth
            log.exception("voice_ws: kernel raised on %s", kind.value)
            await self.send({"directive": "error", "reason": "kernel error", "event": kind.value})
            return
        await self.emit(directives)

    async def _run_executor(
        self, directive: Directive, stop: Optional[threading.Event] = None
    ) -> None:
        assert _executor is not None
        if stop is not None:
            # Passed through the payload rather than the signature so an executor
            # that does not care about cancellation needs no changes.
            directive = Directive(directive.kind, {**directive.payload, "stop_flag": stop})
        try:
            result = _executor(self.session, directive)
            if hasattr(result, "__aiter__"):
                # Streaming executor: dispatch each event the moment it exists,
                # which is what lets the kernel start speaking sentence one while
                # sentence three is still coming off the model (§19).
                async for event in result:  # type: ignore[union-attr]
                    await self.dispatch(event.kind, event.payload)
                return
            follow_up = await result  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice_ws: executor failed on %s", directive.kind.value)
            await self.dispatch(EventKind.ERROR, {"error": str(exc), "source": "executor"})
            return
        for event in follow_up or ():
            await self.dispatch(event.kind, event.payload)

    # ── endpointing heartbeat (§6) ────────────────────────────────────────
    async def _ticker(self) -> None:
        """Re-evaluate the open turn while the user is silent.

        Without this, a client that stops sending partials (the recogniser went
        quiet mid-sentence) would leave the turn open forever: the kernel only
        endpoints when something arrives.
        """
        while True:
            await asyncio.sleep(TICK_INTERVAL_S)
            if self.session.closed:
                return
            await self.dispatch(EventKind.SILENCE_TICK, {})
            if time.monotonic() - self._last_sent_at >= KEEPALIVE_S:
                await self.send(self._liveness())

    def _liveness(self) -> Dict[str, Any]:
        """The idle frame: enough for the UI to stay truthful, nothing more."""
        return {
            "directive": "tick",
            "state": self.session.state.value,
            "task_active": self.session.graph.is_running,
            "progress": round(self.session.graph.progress, 3),
            "context_fill": round(self.session.context.fill, 4),
            "context_indicator": self.session.context.indicator(),
        }

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    def start(self) -> None:
        """Bring up the writer and the heartbeat. Called once, after accept."""
        self._writer_task = asyncio.ensure_future(self._writer())
        self._spawn(self._ticker())

    #: How long `close` will wait for queued frames to actually reach the client.
    DRAIN_TIMEOUT_S = 2.0

    async def close(self) -> None:
        """Shut the connection down without losing what the kernel already said.

        Order is the whole point. `session_close` queues a `stop_listening`
        directive *before* we get here, and cancelling the writer first threw
        that frame away — the client saw the socket vanish instead of being told
        the session had ended. So: stop accepting new frames, push the sentinel,
        cancel the producers, then let the writer finish. The timeout is there
        because a half-dead socket must not wedge the request handler.
        """
        self._closing = True
        # Blocking automation runs in a worker thread that `Task.cancel()` cannot
        # reach, so tell it to stop before cancelling anything.
        for flag in self._stop_flags.values():
            flag.set()
        self._stop_flags.clear()
        self._work.clear()
        with contextlib.suppress(asyncio.QueueFull):
            self._out.put_nowait(None)
        for task in list(self._tasks):
            task.cancel()
        if self._writer_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(self._writer_task), timeout=self.DRAIN_TIMEOUT_S
                )
            self._writer_task.cancel()
        for task in [*self._tasks, self._writer_task]:
            if task is None:
                continue
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


@router.websocket("/ws/voice/{session_id}")
async def voice_socket(websocket: WebSocket, session_id: str) -> None:
    """The live voice channel.

    Client → server frames are `{"event": "<EventKind>", ...}`; server → client
    frames are `Directive.as_dict()`. Reconnecting with the same `session_id`
    picks the same kernel session back up, which is what makes §37's "resume the
    unfinished task after a restart" possible at all.
    """
    ok, why = _authorised(websocket)
    if not ok:
        await websocket.close(code=1008, reason=why)
        log.warning("voice_ws: refused %s (%s)", getattr(websocket.client, "host", "?"), why)
        return

    await websocket.accept()
    registry = get_registry()
    registry.sweep()
    reconnected = registry.get(session_id) is not None
    session = registry.get_or_create(session_id, config=_config_from_query(websocket))
    conn = _Connection(websocket, session)
    conn.start()

    await conn.send({
        "directive": "session_ready",
        "session_id": session.id,
        "reconnected": reconnected,
        "state": session.state.value,
        "config": {
            "model": session.config.model,
            "language": session.config.language,
            "mode": session.config.mode,
            "require_wake_word": session.config.require_wake_word,
        },
    })
    if reconnected:
        resume = session.resume_plan()
        if resume.get("outstanding"):
            await conn.send({"directive": "resume_available", **resume})

    try:
        await conn.dispatch(EventKind.SESSION_OPEN, {"reconnect": reconnected})
        while True:
            raw = await websocket.receive_text()
            kind, payload, error = _parse(raw)
            if error or kind is None:
                await conn.send({"directive": "error", "reason": error})
                continue
            await conn.dispatch(kind, payload)
            if kind is EventKind.SESSION_CLOSE:
                break
    except WebSocketDisconnect:
        # Not a session_close: the plan survives so a reconnect can resume it.
        log.info("voice_ws: %s disconnected, session retained", session.id)
    except Exception:  # pragma: no cover - transport level
        log.exception("voice_ws: socket error on %s", session.id)
    finally:
        await conn.close()
        with contextlib.suppress(Exception):
            await websocket.close()
        if session.closed:
            registry.drop(session.id)
