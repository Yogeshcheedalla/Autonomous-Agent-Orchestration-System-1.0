"""
voice_drive — drive the real voice loop end to end and print what happened.
==========================================================================

This is not a unit test and it is not a mock. It starts the actual FastAPI app on
a real uvicorn server, opens a real WebSocket to `/ws/voice/{session_id}`, and
feeds it the events a browser would send. Everything downstream is the product:
the real kernel, the real executor, real OpenRouter calls, real edge-tts audio.

What it cannot do, stated plainly so no output from it is over-read: there is no
microphone and no speaker in this loop. Transcripts are injected as
`final_transcript` / `partial_transcript` events rather than spoken, because that
is the boundary the browser's Web Speech API sits on — the client sends text, not
audio. So this proves everything from the transcript inward, and nothing about
acoustic accuracy. TTS is verified by synthesising real audio to a real file and
measuring it, which proves the voice renders, not that it sounds good.

Run it:  python -m backend.scripts.voice_drive
         python -m backend.scripts.voice_drive --scenario pause
         python -m backend.scripts.voice_drive --no-llm      (skip model calls)
         python -m backend.scripts.voice_drive --desktop     (REAL mouse/keyboard)

`--desktop` is off by default and stays off unless someone types it. Automation
scenarios otherwise stop at the plan: `build_browser_prompt_plan` is called and
its steps are printed, but `execute_desktop_command` is never reached. That
function moves the real cursor and launches real applications, and a test harness
must not do that to a machine because it happened to be run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import uvicorn
import websockets

PORT = int(os.getenv("VOICE_DRIVE_PORT", "8123"))
HOST = "127.0.0.1"

# Windows consoles default to cp1252, which cannot encode the box-drawing rules or
# a single Telugu character — and this harness prints both. Without this the run
# dies in `print` before it has tested anything, which is a poor way to discover
# your locale. `errors="replace"` rather than raising: a mangled glyph is a
# cosmetic problem, a crashed test run is not.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: Wall-clock ceiling on one scenario. Long enough for a cold OpenRouter route
#: plus a real TTS render, short enough that a wedged step is visible as a
#: failure rather than a hang.
SCENARIO_TIMEOUT_S = 90.0

C = {
    "reset": "\x1b[0m", "dim": "\x1b[2m", "bold": "\x1b[1m",
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m",
}


def paint(text: str, colour: str) -> str:
    if os.getenv("NO_COLOR"):
        return text
    return f"{C[colour]}{text}{C['reset']}"


# ── server lifecycle ─────────────────────────────────────────────────────────
class Server:
    """The real app on a real port, brought up in-process.

    Why not `TestClient`: it drives the ASGI app through a portal thread, which
    serialises the very concurrency under test. The endpointing heartbeat ticks
    every 120ms while an executor task streams tokens off a worker thread — the
    interleaving *is* the behaviour, and a harness that flattens it would report
    success on a loop that cannot barge in.
    """

    def __init__(self) -> None:
        from backend.main import app

        config = uvicorn.Config(
            app, host=HOST, port=PORT, log_level="error", access_log=False,
            ws_ping_interval=None,   # our own frames are the liveness signal
        )
        self.server = uvicorn.Server(config)
        self._task: Optional[asyncio.Task[Any]] = None

    async def __aenter__(self) -> "Server":
        self._task = asyncio.ensure_future(self.server.serve())
        deadline = time.monotonic() + 30.0
        while not self.server.started:
            if self._task.done():        # startup raised; surface it
                await self._task
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start within 30s")
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10.0)


# ── the client ───────────────────────────────────────────────────────────────
class Voice:
    """A browser, minus the microphone.

    Frames are collected in the background rather than pulled on demand, because
    the interesting failures are things arriving that should not have — a `speak`
    for a backchannel, a token from a superseded turn — and a harness that only
    reads when it expects something cannot see those.
    """

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.frames: List[Dict[str, Any]] = []
        self._detached = False
        self._clock = time.monotonic()
        self._first_speak_at: Optional[float] = None
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                frame = json.loads(raw)
                # Stamped on arrival, because latency is the one thing a harness
                # cannot reconstruct afterwards. `scenario_reply` used to report
                # `time.monotonic() - started` after an unconditional
                # `await v.wait(20.0)`, so it always printed ~20s no matter how
                # fast the reply actually was -- it was timing its own sleep.
                if (
                    self._first_speak_at is None
                    and frame.get("directive") == "speak"
                    and (frame.get("text") or "").strip()
                ):
                    self._first_speak_at = time.monotonic()
                self.frames.append(frame)
                self._show(frame)
        except Exception:
            pass

    def _show(self, frame: Dict[str, Any]) -> None:
        kind = frame.get("directive", "?")
        if kind == "tick":
            return  # 8/second; the state changes are reported by state_changed
        if kind == "speak":
            text = (frame.get("text") or "").strip()
            prio = frame.get("priority")
            print(f"   {paint('🔊 AKANSHA', 'green')} {paint(f'[P{prio}]', 'dim')} {text}")
        elif kind == "state_changed":
            print(f"   {paint('· state', 'dim')} {paint(frame.get('state', '?'), 'cyan')}")
        elif kind in ("execute_plan", "cancel_execution", "stop_tts", "pause_execution",
                      "ask_clarification", "request_confirmation", "compress_context"):
            detail = frame.get("description") or frame.get("question") or frame.get("reason") or ""
            print(f"   {paint('⚙ ' + kind, 'yellow')} {paint(str(detail)[:90], 'dim')}")
        elif kind == "error":
            print(f"   {paint('✖ error', 'red')} {frame.get('reason') or frame.get('error')}")

    async def send(self, event: str, **payload: Any) -> None:
        await self.ws.send(json.dumps({"event": event, **payload}))

    async def say(self, text: str, *, partial_pause: float = 0.0) -> None:
        """Speak an utterance the way a recogniser delivers one.

        Partials first, then a final — this ordering matters, because the kernel
        endpoints on interim text and a harness that only ever sent finals would
        never exercise §6 at all.
        """
        print(f"   {paint('🎤 USER', 'blue')}     {text}")
        await self.send("speech_start")
        words = text.split()
        for i in range(1, len(words) + 1):
            await self.send("partial_transcript", text=" ".join(words[:i]), confidence=0.92)
            await asyncio.sleep(0.03)
        if partial_pause:
            await asyncio.sleep(partial_pause)
        await self.send("speech_end")
        await self.send("final_transcript", text=text, confidence=0.94)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_for_speech(self, timeout: float) -> bool:
        """Wait until something is actually spoken, not for a fixed duration.

        A flat `wait(20.0)` makes every scenario cost its worst case and hides the
        real latency underneath it. Returns False on timeout so a caller can still
        assert on silence.
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._first_speak_at is not None:
                return True
            await asyncio.sleep(0.05)
        return self._first_speak_at is not None

    def mark(self) -> None:
        """Start the latency clock, so it measures the turn and not the setup."""
        self._clock = time.monotonic()
        self._first_speak_at = None

    def time_to_first_speech(self) -> Optional[float]:
        if self._first_speak_at is None:
            return None
        return self._first_speak_at - self._clock

    def said(self) -> List[str]:
        return [(f.get("text") or "").strip() for f in self.frames if f.get("directive") == "speak"]

    def kinds(self) -> List[str]:
        return [f.get("directive", "") for f in self.frames if f.get("directive") != "tick"]

    def last_tick(self) -> Dict[str, Any]:
        for frame in reversed(self.frames):
            if frame.get("directive") == "tick":
                return frame
        return {}

    async def tick(self, timeout: float = 4.0) -> Dict[str, Any]:
        """Wait for a *fresh* liveness frame and return it.

        Reading `last_tick()` directly is unreliable and produced three spurious
        failures on the first run: the keepalive only fires after a second of
        outbound quiet, so during a busy stretch there is simply no tick to read
        and an assertion on it fails for want of a frame rather than want of a
        task. Waiting for one makes the check about the session's state instead of
        about frame timing.
        """
        seen = len(self.frames)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames[seen:]:
                if frame.get("directive") == "tick":
                    return frame
            await asyncio.sleep(0.05)
        return self.last_tick()

    def clear(self) -> None:
        self.frames.clear()

    def detach(self) -> None:
        """Leave without saying goodbye.

        This is what a closing browser tab looks like: the socket dies and no
        `session_close` is ever sent. The kernel has to treat the two differently
        (§34, §37) — a disconnect keeps the plan, a close ends it — so the resume
        scenario needs a way to leave rudely.

        The flag matters. Cancelling the reader alone is not enough, because the
        context manager still runs `close()` on the way out and *that* sends
        `session_close` — which ends the session for real and made the resume
        check fail against a product that was behaving correctly.
        """
        self._detached = True
        self._reader.cancel()

    async def close(self) -> None:
        if not self._detached:
            with contextlib.suppress(Exception):
                await self.send("session_close")
                await asyncio.sleep(0.3)
        self._reader.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._reader


@contextlib.asynccontextmanager
async def connect(session_id: str, **params: str):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"ws://{HOST}:{PORT}/ws/voice/{session_id}" + (f"?{query}" if query else "")
    async with websockets.connect(url, max_size=2**20) as ws:
        voice = Voice(ws)
        await asyncio.sleep(0.4)   # let session_ready + session_open settle
        try:
            yield voice
        finally:
            await voice.close()


# ── result bookkeeping ───────────────────────────────────────────────────────
class Report:
    def __init__(self) -> None:
        self.rows: List[tuple[str, str, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        mark = paint("PASS", "green") if passed else paint("FAIL", "red")
        print(f"   {mark} {name}" + (f" {paint('— ' + detail, 'dim')}" if detail else ""))
        self.rows.append(("pass" if passed else "fail", name, detail))
        return passed

    def note(self, name: str, detail: str) -> None:
        print(f"   {paint('INFO', 'yellow')} {name} {paint('— ' + detail, 'dim')}")
        self.rows.append(("info", name, detail))

    def summary(self) -> int:
        passed = sum(1 for r in self.rows if r[0] == "pass")
        failed = [r for r in self.rows if r[0] == "fail"]
        infos = sum(1 for r in self.rows if r[0] == "info")
        print()
        print(paint("═" * 78, "dim"))
        print(f"{paint('RESULT', 'bold')}  {passed} passed, {len(failed)} failed, {infos} informational")
        for _, name, detail in failed:
            print(f"        {paint('x', 'red')} {name} {paint(detail, 'dim')}")
        print(paint("═" * 78, "dim"))
        return 1 if failed else 0


async def main() -> int:
    # Imported here, not at module scope: the scenarios import this module for
    # `connect`/`Report`/`paint`, so a top-level import would be circular.
    from .voice_scenarios import NEEDS_LLM, SCENARIOS

    parser = argparse.ArgumentParser(description="Drive the real voice loop end to end.")
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS),
                        help="run only these (repeatable); default is all")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip scenarios that call OpenRouter")
    parser.add_argument("--desktop", action="store_true",
                        help="ALLOW REAL mouse and keyboard control in the automation scenario")
    opts = parser.parse_args()

    chosen = opts.scenario or list(SCENARIOS)
    if opts.no_llm:
        chosen = [name for name in chosen if name not in NEEDS_LLM]

    print(paint("═" * 78, "dim"))
    print(paint("  Akansha voice loop — live end-to-end drive", "bold"))
    print(paint(f"  real uvicorn on {HOST}:{PORT} · real kernel · real executor", "dim"))
    print(paint("  transcripts are injected as events; there is no microphone here", "dim"))
    if opts.desktop:
        print(paint("  --desktop: REAL mouse and keyboard control is ENABLED", "red"))
    print(paint("═" * 78, "dim"))

    report = Report()
    async with Server():
        for name in chosen:
            try:
                await asyncio.wait_for(SCENARIOS[name](report, opts), timeout=SCENARIO_TIMEOUT_S)
            except asyncio.TimeoutError:
                report.check(f"scenario {name!r} finished", False,
                             f"timed out after {SCENARIO_TIMEOUT_S:.0f}s")
            except Exception as exc:
                report.check(f"scenario {name!r} finished", False,
                             f"{type(exc).__name__}: {exc}")
    return report.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
