"""
Avatar streaming: transport, ordering, A/V metadata, and fallback.

These tests cover everything about the photoreal-avatar path *except* the neural
model. That split is deliberate and is the reason `PortraitEngine` exists: the
parts that break in practice are the framing (a 4-byte index prefix on a binary
frame), the ordering guarantee (audio must precede meta must precede frames),
and the fallback (a stale Colab URL must not take the conversation down). None of
those need a GPU, so all of them are tested here.

What is *not* covered, and cannot be on this machine: MuseTalk itself. There is
no CUDA device, so `MuseTalkEngine.__post_init__` raises by design.

`pytest-asyncio` is not installed in this project, so async setup is driven with
`asyncio.run` on a dedicated thread rather than with async test functions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
WORKER_DIR = REPO / "tools" / "musetalk_worker"
PORTRAIT = REPO / "public" / "assets" / "images" / "akansha-presence.webp"

pytest.importorskip("PIL", reason="PortraitEngine needs Pillow")
pytest.importorskip("websockets", reason="worker transport needs websockets")

if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg is required to decode test audio", allow_module_level=True)
if not PORTRAIT.exists():
    pytest.skip(f"portrait asset missing: {PORTRAIT}", allow_module_level=True)

sys.path.insert(0, str(WORKER_DIR))
import worker as worker_mod  # noqa: E402  — path is set immediately above

from backend import main as backend_main  # noqa: E402
from backend.avatar import api as avatar_api  # noqa: E402
from backend.avatar import worker_settings  # noqa: E402


def _test_mp3(seconds: float = 1.2) -> bytes:
    """A short tone. Content is irrelevant; only its duration drives frame count."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-b:a", "64k", "-f", "mp3", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


class _WorkerServer:
    """The GPU worker, in-process, on an ephemeral port."""

    def __init__(self) -> None:
        self.port: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._shutdown: Optional[asyncio.Event] = None

    def start(self) -> None:
        def run() -> None:
            async def serve() -> None:
                import websockets

                engine = worker_mod.PortraitEngine(image_path=str(PORTRAIT), fps=25.0)
                w = worker_mod.Worker(engine)
                self._shutdown = asyncio.Event()
                async with websockets.serve(w.handle, "127.0.0.1", 0, max_size=None) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    self._ready.set()
                    # Waited on rather than `asyncio.Future()` so teardown can
                    # unwind `websockets.serve` properly. Stopping the loop
                    # instead leaves its __aexit__ trying to create a task on a
                    # closed loop, and the resulting noise lands after the test
                    # summary where it is easy to mistake for a real failure.
                    await self._shutdown.wait()

            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(serve())
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name="avatar-worker")
        self._thread.start()
        assert self._ready.wait(timeout=30), "worker did not start"

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def stop(self) -> None:
        loop, shutdown = self._loop, self._shutdown
        if loop is not None and shutdown is not None:
            loop.call_soon_threadsafe(shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=10)


@pytest.fixture(scope="module")
def worker_server() -> Any:
    srv = _WorkerServer()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture()
def client(worker_server: Any) -> Any:
    """App wired to the in-process worker, with a deterministic offline voice."""
    audio = _test_mp3()

    async def fake_tts(text: str, gender: str, tone: Any, lang: Any) -> bytes:
        return audio

    previous = avatar_api._tts
    avatar_api.set_tts_provider(fake_tts)
    worker_settings.url = worker_server.url
    worker_settings.model = None
    # The socket to the worker is closed by the app's own `shutdown` handler,
    # which `TestClient.__exit__` runs on the loop that opened it. Closing it
    # here instead — after the `with` block, via `asyncio.run` — cannot work:
    # the pending `recv()` is an overlapped operation owned by the app's loop,
    # and a fresh loop cannot cancel it. Leaving it outstanding hangs
    # `IocpProactor.close()` on Windows in a poll loop with no exit.
    with TestClient(backend_main.app) as c:
        c.post("/api/avatar/worker", json={"url": worker_server.url, "avatar": "akansha"})
        yield c
    avatar_api.set_tts_provider(previous)
    worker_settings.url = ""


def _drain(ws: Any, limit: int = 500) -> tuple[List[Dict[str, Any]], List[bytes]]:
    """Collect messages until `end`/`fallback`, keeping text and binary apart."""
    texts: List[Dict[str, Any]] = []
    blobs: List[bytes] = []
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("type") == "websocket.disconnect":
            break
        if msg.get("bytes") is not None:
            blobs.append(msg["bytes"])
            continue
        payload = json.loads(msg["text"])
        texts.append(payload)
        if payload.get("type") in {"end", "fallback", "error"}:
            break
    return texts, blobs


def test_worker_reports_ready(client: Any) -> None:
    """`/api/avatar/status` must reflect the engine actually on the other end."""
    body = client.get("/api/avatar/status").json()
    assert body["worker"] == "connected", body
    assert body["model"] == "portrait-stub"
    assert body["fps"] == 25.0


def test_speak_streams_ordered_frames(client: Any) -> None:
    with client.websocket_connect("/ws/avatar/t-stream") as ws:
        status = json.loads(ws.receive()["text"])
        assert status["type"] == "status"
        assert status["worker"] == "connected"

        ws.send_text(json.dumps({"type": "speak", "id": "u1", "text": "hello there"}))
        texts, blobs = _drain(ws)

    kinds = [t["type"] for t in texts]
    # Audio is the clock and must arrive before any frame metadata.
    assert kinds[0] == "audio", kinds
    assert kinds[1] == "meta", kinds
    assert kinds[-1] == "end", kinds

    audio = texts[0]
    assert audio["format"] == "mp3"
    assert len(base64.b64decode(audio["b64"])) > 0

    meta = texts[1]
    assert meta["fps"] == 25.0
    assert meta["frames"] > 0
    assert meta["width"] == 512

    assert len(blobs) == meta["frames"], "every announced frame must arrive"
    indices = [struct.unpack(">I", b[:4])[0] for b in blobs]
    assert indices == sorted(indices), "frames must be monotonic"
    assert indices == list(range(len(indices))), "frame indices must be gapless"
    # Each payload past the prefix is a real JPEG.
    for blob in blobs[:5]:
        assert blob[4:6] == b"\xff\xd8", "payload is not JPEG"
    assert texts[-1]["frames"] == len(blobs)


def test_frame_count_tracks_audio_duration(client: Any) -> None:
    """A 1.2s utterance at 25fps is ~30 frames; drift means broken A/V sync."""
    with client.websocket_connect("/ws/avatar/t-duration") as ws:
        ws.receive()
        ws.send_text(json.dumps({"type": "speak", "id": "u2", "text": "timing"}))
        texts, blobs = _drain(ws)
    meta = next(t for t in texts if t["type"] == "meta")
    assert 25 <= meta["frames"] <= 35, meta["frames"]
    assert len(blobs) == meta["frames"]


def test_missing_worker_falls_back_without_dropping_audio(client: Any) -> None:
    """
    The whole point of the fallback: a dead worker still speaks.

    The browser must receive the audio it needs to finish the sentence and an
    explicit instruction to render the CSS rig — not an error, and not a close.

    The worker is cleared through the public settings endpoint rather than by
    reaching into the client, because that is what actually happens when a Colab
    tunnel goes stale — and because the endpoint closes the socket on the loop
    that opened it, which a bare `asyncio.run(...close())` cannot do.
    """
    cleared = client.post("/api/avatar/worker", json={"url": ""}).json()
    assert cleared["worker"] == "absent", cleared

    with client.websocket_connect("/ws/avatar/t-fallback") as ws:
        status = json.loads(ws.receive()["text"])
        assert status["worker"] == "absent"

        ws.send_text(json.dumps({"type": "speak", "id": "u3", "text": "still talking"}))
        texts, blobs = _drain(ws)

    kinds = [t["type"] for t in texts]
    assert kinds == ["audio", "fallback"], kinds
    assert len(base64.b64decode(texts[0]["b64"])) > 0
    assert not blobs


def test_empty_speak_is_rejected(client: Any) -> None:
    with client.websocket_connect("/ws/avatar/t-empty") as ws:
        ws.receive()
        ws.send_text(json.dumps({"type": "speak", "id": "u4", "text": "   "}))
        payload = json.loads(ws.receive()["text"])
    assert payload["type"] == "error"
    assert "text" in payload["message"]


def test_malformed_json_does_not_close_socket(client: Any) -> None:
    """A relay must survive a bad frame; the conversation outlives one typo."""
    with client.websocket_connect("/ws/avatar/t-bad") as ws:
        ws.receive()
        ws.send_text("{not json")
        first = json.loads(ws.receive()["text"])
        assert first["type"] == "error"
        # Still usable afterwards.
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive()["text"])["type"] == "pong"


def test_speak_audio_does_not_resynthesize(client: Any) -> None:
    """
    The voice page supplies audio it is already playing.

    Two properties matter: the relay must not call TTS (a second synthesis is a
    different waveform, so the face would lipsync to audio nobody hears), and it
    must not echo the audio back (the browser already has it).
    """
    calls: List[str] = []

    async def counting_tts(text: str, gender: str, tone: Any, lang: Any) -> bytes:
        calls.append(text)
        return _test_mp3()

    previous = avatar_api._tts
    avatar_api.set_tts_provider(counting_tts)
    try:
        audio = _test_mp3()
        with client.websocket_connect("/ws/avatar/t-audio") as ws:
            ws.receive()
            ws.send_text(
                json.dumps(
                    {
                        "type": "speak_audio",
                        "id": "u5",
                        "format": "mp3",
                        "b64": base64.b64encode(audio).decode("ascii"),
                    }
                )
            )
            texts, blobs = _drain(ws)
    finally:
        avatar_api.set_tts_provider(previous)

    assert calls == [], "speak_audio must not invoke TTS"
    kinds = [t["type"] for t in texts]
    assert kinds[0] == "audio" and texts[0]["client_audio"] is True
    assert "b64" not in texts[0], "must not echo audio the client already has"
    assert kinds[1] == "meta"
    assert kinds[-1] == "end"
    assert len(blobs) == texts[1]["frames"] > 0


def test_speak_audio_rejects_non_base64(client: Any) -> None:
    with client.websocket_connect("/ws/avatar/t-b64") as ws:
        ws.receive()
        ws.send_text(json.dumps({"type": "speak_audio", "id": "u6", "b64": "!!!not base64!!!"}))
        payload = json.loads(ws.receive()["text"])
    assert payload["type"] == "error"
    assert "base64" in payload["message"]


def test_musetalk_engine_refuses_without_cuda() -> None:
    """
    Guards the honest-failure property: the real engine must not silently
    degrade into something that is not a face model.
    """
    torch = pytest.importorskip("torch", reason="torch absent, so the guard cannot be exercised")
    if torch.cuda.is_available():
        pytest.skip("CUDA present; this guard only applies to CPU-only hosts")
    with pytest.raises(RuntimeError, match="CUDA"):
        worker_mod.MuseTalkEngine(musetalk_root=str(WORKER_DIR), avatar_image=str(PORTRAIT))


# — worker-side cancellation ----------------------------------------------
#
# These talk to the worker directly rather than through the relay, because what
# is being tested is a property of the worker's own read loop: that it keeps
# servicing the socket *during* a render. Going through the relay would prove
# only that the relay forwards a cancel.


def _worker_roundtrip(url: str, script: Any) -> Any:
    """Run `script(ws)` against the worker on its own event loop."""

    async def go() -> Any:
        import websockets

        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "hello", "protocol": 1}))
            await ws.recv()
            return await script(ws)

    return asyncio.run(go())


def test_cancel_stops_render_midway(worker_server: Any) -> None:
    """
    Barge-in must interrupt the render, not just the sending of it.

    The regression this guards is subtle and was real: with `speak` awaited
    inline in the read loop, the socket was not read at all during a render, so
    the `cancel` sat in the buffer until the render it was meant to interrupt had
    already finished. Every frame still arrived and the GPU did the full sentence.

    Ten seconds of audio is ~250 frames, long enough that a cancel sent right
    after the first frames arrive lands well before the render would have ended.
    """
    long_audio = _test_mp3(10.0)

    async def script(ws: Any) -> Any:
        await ws.send(
            json.dumps(
                {
                    "type": "speak",
                    "id": "c1",
                    "format": "mp3",
                    "audio": base64.b64encode(long_audio).decode("ascii"),
                }
            )
        )
        meta = None
        seen = 0
        cancelled_after = None
        kinds = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(raw, (bytes, bytearray)):
                seen += 1
                # Cancel once the stream is demonstrably flowing.
                if seen == 3:
                    await ws.send(json.dumps({"type": "cancel", "id": "c1"}))
                    cancelled_after = seen
                continue
            payload = json.loads(raw)
            kinds.append(payload["type"])
            if payload["type"] == "meta":
                meta = payload
            if payload["type"] in {"end", "error"}:
                break
        return meta, seen, cancelled_after, kinds

    meta, seen, cancelled_after, kinds = _worker_roundtrip(worker_server.url, script)

    assert meta is not None, kinds
    assert cancelled_after == 3, "never got enough frames to cancel mid-stream"
    assert meta["frames"] > 200, f"10s at 25fps should announce ~250 frames, got {meta['frames']}"
    # The point of the test: the render stopped early.
    assert seen < meta["frames"], f"cancel did not stop the render ({seen}/{meta['frames']})"
    assert "error" not in kinds, f"cancel is not an error condition: {kinds}"


def test_ping_is_answered_during_a_render(worker_server: Any) -> None:
    """
    The same property as above, stated without cancellation.

    If the read loop is blocked for the duration of a render, a `ping` sent
    mid-utterance cannot be answered until the render finishes. This asserts the
    socket stays responsive, which is what makes barge-in possible at all.
    """
    long_audio = _test_mp3(10.0)

    async def script(ws: Any) -> Any:
        await ws.send(
            json.dumps(
                {
                    "type": "speak",
                    "id": "p1",
                    "format": "mp3",
                    "audio": base64.b64encode(long_audio).decode("ascii"),
                }
            )
        )
        seen = 0
        pinged = False
        pong_after = None
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(raw, (bytes, bytearray)):
                seen += 1
                if seen == 3 and not pinged:
                    await ws.send(json.dumps({"type": "ping"}))
                    pinged = True
                continue
            payload = json.loads(raw)
            if payload["type"] == "pong":
                pong_after = seen
                # Stop the render; the point is already proven.
                await ws.send(json.dumps({"type": "cancel", "id": "p1"}))
            if payload["type"] in {"end", "error"}:
                break
        return pong_after, seen

    pong_after, seen = _worker_roundtrip(worker_server.url, script)
    assert pong_after is not None, "ping went unanswered for the whole render"
    assert pong_after < seen, "pong arrived only after the render finished"
