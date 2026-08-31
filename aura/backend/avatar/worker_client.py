"""
worker_client — the backend half of the MuseTalk link.

Wire protocol (this module ⇄ the GPU worker), JSON text frames plus raw binary:

    → {"type":"hello","protocol":1}
    ← {"type":"ready","protocol":1,"model":"musetalk","fps":25,"avatars":[...]}

    → {"type":"speak","id":"<utt>","audio":"<base64>","format":"mp3","avatar":"akansha"}
    ← {"type":"meta","id":"<utt>","fps":25,"width":512,"height":512,"frames":74}
    ← <binary>  uint32be frame index ‖ JPEG bytes     (× frames)
    ← {"type":"end","id":"<utt>"}

    → {"type":"cancel","id":"<utt>"}         barge-in; worker stops mid-stream
    ← {"type":"error","id":"<utt>","message":"..."}

Frames are binary rather than base64-in-JSON on purpose: a 512×512 JPEG is
~25 KB, base64 inflates that by a third, and at 25 fps that is ~200 KB/s of
pure encoding overhead over a Colab tunnel that is already the bottleneck. The
4-byte index prefix exists because a dropped or reordered frame must be
detectable — the browser seeks by index, so a silent gap would desync A/V
rather than just drop a frame.

Audio is forwarded in whatever container `edge_tts` produced (mp3) rather than
transcoded to wav here. MuseTalk's audio front end goes through librosa, which
decodes mp3 via ffmpeg, and ffmpeg is guaranteed present on the Colab side but
not on every machine that might run this backend. Transcoding locally would add
a dependency and a copy for no benefit.

Cancellation is explicit and worker-side. Barge-in has to stop the *inference*,
not merely stop reading its output: MuseTalk keeps burning GPU time for the
rest of the utterance otherwise, which on a shared free T4 is the difference
between a responsive assistant and one that falls further behind on every turn.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("akansha.avatar.worker")

#: Protocol version. Bumped only on a breaking frame-format change, so an old
#: Colab notebook fails loudly at `hello` instead of streaming garbage.
PROTOCOL = 1

#: How long to wait for the worker's `ready` before giving up on a URL. Colab
#: tunnels are slow to establish but not *this* slow; beyond this the URL is
#: almost always stale from a previous notebook session.
CONNECT_TIMEOUT_S = 12.0

#: A worker that has gone quiet mid-utterance is worse than one that is absent,
#: because the browser is holding a half-buffered face. Give up and let it fall
#: back to the CSS rig.
FRAME_TIMEOUT_S = 20.0

#: Keepalive for an idle link. Colab's tunnel drops silent sockets after ~60s.
PING_INTERVAL_S = 20.0

#: MuseTalk emits 512×512 at 25 fps; a whole sentence is a few hundred frames.
#: The cap is a backstop against a runaway worker, not a real limit.
MAX_FRAMES_PER_UTTERANCE = 2000

#: Reject an oversized frame rather than buffering it. A 512×512 JPEG that is
#: over a megabyte means the worker is misconfigured (raw PNG, or 4K output).
MAX_FRAME_BYTES = 1_500_000


class WorkerUnavailable(RuntimeError):
    """No worker is configured, or the configured one could not be reached."""


@dataclass(frozen=True)
class WorkerMeta:
    """Everything the browser needs to schedule playback before frame one."""

    utterance_id: str
    fps: float
    width: int
    height: int
    frames: int


@dataclass(frozen=True)
class WorkerFrame:
    """One rendered face, tagged with the index the browser seeks by."""

    index: int
    jpeg: bytes


@dataclass
class _Settings:
    """
    Mutable worker configuration.

    Deliberately not a pydantic settings object read once at import: the Colab
    tunnel URL changes on every notebook restart, and the whole point of the
    settings API is to accept a new one without bouncing the backend.
    """

    url: str = field(default_factory=lambda: (os.getenv("AKANSHA_AVATAR_WORKER_URL") or "").strip())
    avatar: str = field(default_factory=lambda: (os.getenv("AKANSHA_AVATAR_ID") or "akansha").strip())
    #: Populated from the worker's `ready` frame, so `/api/avatar/status` can
    #: report what the GPU side actually is rather than what we hoped for.
    model: Optional[str] = None
    fps: Optional[float] = None
    last_error: Optional[str] = None
    last_ok_at: Optional[float] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "avatar": self.avatar,
            "model": self.model,
            "fps": self.fps,
            "configured": bool(self.url),
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
        }


worker_settings = _Settings()


class AvatarWorkerClient:
    """
    One connection to the GPU worker, shared by every browser session.

    Shared rather than per-session because the worker is a single GPU: two
    concurrent inferences on a free T4 do not go twice as fast, they thrash and
    both miss real-time. `_lock` serialises utterances so the queue is explicit
    instead of emergent.
    """

    def __init__(self) -> None:
        self._ws: Optional[Any] = None
        self._lock = asyncio.Lock()
        #: Guards connect/teardown against concurrent first-use.
        self._connect_lock = asyncio.Lock()

    # — connection ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(worker_settings.url)

    def _is_open(self) -> bool:
        # websockets ≥14 dropped `.closed` from the asyncio client in favour of
        # a `State` enum, so probe both rather than pinning to one version.
        ws = self._ws
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        if state is not None:
            return getattr(state, "name", str(state)) == "OPEN"
        return not getattr(ws, "closed", True)

    async def _ensure(self) -> Any:
        if not self.configured:
            raise WorkerUnavailable("no avatar worker URL configured")
        if self._is_open():
            return self._ws

        async with self._connect_lock:
            if self._is_open():
                return self._ws
            url = worker_settings.url
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        max_size=MAX_FRAME_BYTES,
                        ping_interval=PING_INTERVAL_S,
                        open_timeout=CONNECT_TIMEOUT_S,
                    ),
                    timeout=CONNECT_TIMEOUT_S,
                )
                await ws.send(json.dumps({"type": "hello", "protocol": PROTOCOL}))
                raw = await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT_S)
                hello = json.loads(raw) if isinstance(raw, str) else {}
                if hello.get("type") != "ready":
                    await ws.close()
                    raise WorkerUnavailable(f"worker did not greet with ready: {hello!r}")
                if int(hello.get("protocol") or 0) != PROTOCOL:
                    await ws.close()
                    raise WorkerUnavailable(
                        f"worker protocol {hello.get('protocol')} != {PROTOCOL}; update the notebook"
                    )
                worker_settings.model = str(hello.get("model") or "unknown")
                worker_settings.fps = float(hello.get("fps") or 25.0)
                worker_settings.last_error = None
                worker_settings.last_ok_at = time.time()
                self._ws = ws
                log.info("avatar worker ready: model=%s fps=%s", worker_settings.model, worker_settings.fps)
                return ws
            except WorkerUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 — any failure means "fall back"
                worker_settings.last_error = f"{type(exc).__name__}: {exc}"
                self._ws = None
                raise WorkerUnavailable(worker_settings.last_error) from exc

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def probe(self) -> Dict[str, Any]:
        """Connect if needed and report status. Used by `/api/avatar/status`."""
        try:
            await self._ensure()
            return {"worker": "connected", **worker_settings.snapshot()}
        except WorkerUnavailable as exc:
            return {
                "worker": "absent",
                "reason": str(exc),
                **worker_settings.snapshot(),
            }

    # — inference ----------------------------------------------------------

    async def speak(
        self,
        utterance_id: str,
        audio_bytes: bytes,
        *,
        fmt: str = "mp3",
        avatar: Optional[str] = None,
        cancel: Optional[asyncio.Event] = None,
    ) -> AsyncIterator[WorkerMeta | WorkerFrame]:
        """
        Drive one utterance and yield its `WorkerMeta` then its `WorkerFrame`s.

        Yields meta first so a caller can forward timing to the browser before
        any pixels exist — playback scheduling needs fps and frame count, and
        waiting for the last frame to learn them would defeat streaming.

        Raises `WorkerUnavailable` if the worker is missing or dies mid-stream;
        callers treat that as "render the CSS rig instead".
        """
        async with self._lock:
            ws = await self._ensure()
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "speak",
                            "id": utterance_id,
                            "audio": base64.b64encode(audio_bytes).decode("ascii"),
                            "format": fmt,
                            "avatar": (avatar or worker_settings.avatar),
                        }
                    )
                )
            except Exception as exc:  # noqa: BLE001
                await self.close()
                raise WorkerUnavailable(f"send failed: {exc}") from exc

            meta: Optional[WorkerMeta] = None
            seen = 0
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        # Stop the GPU, not just our reading of it.
                        with contextlib.suppress(Exception):
                            await ws.send(json.dumps({"type": "cancel", "id": utterance_id}))
                        return

                    raw = await asyncio.wait_for(ws.recv(), timeout=FRAME_TIMEOUT_S)

                    if isinstance(raw, (bytes, bytearray)):
                        if len(raw) < 5:
                            continue  # index prefix with no payload: nothing to draw
                        index = struct.unpack(">I", bytes(raw[:4]))[0]
                        yield WorkerFrame(index=index, jpeg=bytes(raw[4:]))
                        seen += 1
                        if seen > MAX_FRAMES_PER_UTTERANCE:
                            raise WorkerUnavailable("worker exceeded frame cap; assuming runaway")
                        continue

                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "meta":
                        meta = WorkerMeta(
                            utterance_id=str(msg.get("id") or utterance_id),
                            fps=float(msg.get("fps") or worker_settings.fps or 25.0),
                            width=int(msg.get("width") or 512),
                            height=int(msg.get("height") or 512),
                            frames=int(msg.get("frames") or 0),
                        )
                        yield meta
                    elif kind == "end":
                        worker_settings.last_ok_at = time.time()
                        return
                    elif kind == "error":
                        raise WorkerUnavailable(str(msg.get("message") or "worker error"))
                    elif kind == "pong":
                        continue
                    else:
                        log.debug("avatar worker: ignoring %r", kind)
            except asyncio.TimeoutError as exc:
                await self.close()
                raise WorkerUnavailable("worker stopped sending frames") from exc
            except ConnectionClosed as exc:
                await self.close()
                raise WorkerUnavailable(f"worker closed the socket: {exc}") from exc


_client: Optional[AvatarWorkerClient] = None


def get_worker_client() -> AvatarWorkerClient:
    """Process-wide worker client. One GPU, one link."""
    global _client
    if _client is None:
        _client = AvatarWorkerClient()
    return _client
