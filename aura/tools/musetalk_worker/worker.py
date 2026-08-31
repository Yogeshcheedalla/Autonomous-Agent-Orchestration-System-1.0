"""
worker — the GPU half of the avatar link. Runs on Colab, not on the app machine.

Speaks the protocol documented in `backend/avatar/worker_client.py`:

    ← {"type":"hello","protocol":1}
    → {"type":"ready","protocol":1,"model":...,"fps":25,"avatars":[...]}
    ← {"type":"speak","id":...,"audio":"<b64>","format":"mp3","avatar":...}
    → {"type":"meta",...}  then  <uint32be index ‖ JPEG> × N  then {"type":"end"}
    ← {"type":"cancel","id":...}

Two engines, one transport. That split is the point of this file: the protocol,
the framing, the cancellation and the backpressure are all testable without a
GPU, and only `MuseTalkEngine` needs one.

  * `MuseTalkEngine` — the real thing. Audio-driven neural lipsync on a prepared
    avatar. Needs CUDA and the MuseTalk repo on `sys.path`.
  * `PortraitEngine` — a deliberate stand-in that warps a still photo from the
    audio envelope. It exists to prove the *pipeline* (framing, ordering, A/V
    sync, cancellation, fallback) on a machine with no GPU. It is not a face
    model and is not meant to look like one; if you are seeing its output when
    you wanted MuseTalk, the log line at startup says so.

Run:
    python worker.py --engine musetalk --avatar-image akansha.png --port 8765
    python worker.py --engine portrait --avatar-image akansha.png --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import contextlib
import io
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Protocol

import numpy as np
import websockets

log = logging.getLogger("avatar.worker")

PROTOCOL = 1
DEFAULT_FPS = 25.0

#: JPEG quality for streamed frames. 82 is where the tunnel stops being the
#: bottleneck without visible blocking on skin gradients.
JPEG_QUALITY = 82


# — audio helpers ---------------------------------------------------------


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required to decode TTS audio")
    return exe


def decode_audio(raw: bytes, fmt: str, sr: int = 16000) -> np.ndarray:
    """
    Decode arbitrary TTS output to mono float32 PCM at `sr`.

    Goes through ffmpeg rather than librosa/soundfile so the worker has no
    opinion about the container: `edge_tts` emits mp3 today, and a future voice
    might emit ogg or wav without needing a change here.
    """
    proc = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-f", fmt if fmt in {"mp3", "wav", "ogg", "flac"} else "mp3",
            "-i", "pipe:0",
            "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1",
        ],
        input=raw,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def envelope(pcm: np.ndarray, sr: int, fps: float) -> np.ndarray:
    """
    Per-frame loudness in 0..1, used by `PortraitEngine` and for `meta.frames`.

    RMS per frame window, then normalised against the 95th percentile rather
    than the max: one plosive should not flatten a whole sentence.
    """
    hop = max(1, int(round(sr / fps)))
    n = max(1, int(math.ceil(len(pcm) / hop)))
    out = np.zeros(n, dtype=np.float32)
    for i in range(n):
        w = pcm[i * hop : (i + 1) * hop]
        if w.size:
            out[i] = float(np.sqrt(np.mean(np.square(w))))
    ref = float(np.percentile(out, 95)) or 1.0
    return np.clip(out / ref, 0.0, 1.0)


# — engines ---------------------------------------------------------------


class Engine(Protocol):
    """Turns one utterance's audio into a sequence of JPEG frames."""

    name: str
    fps: float

    def avatars(self) -> List[str]: ...

    def render(self, pcm: np.ndarray, sr: int, avatar: str) -> Iterator[bytes]:
        """Yield JPEG bytes, one per frame, in order."""
        ...


@dataclass
class PortraitEngine:
    """
    No-GPU stand-in: opens the mouth region of a still photo by the audio
    envelope. Proves transport, not realism — see the module docstring.
    """

    image_path: str
    fps: float = DEFAULT_FPS
    name: str = "portrait-stub"

    def __post_init__(self) -> None:
        from PIL import Image  # imported late: only this engine needs Pillow

        self._Image = Image
        img = Image.open(self.image_path).convert("RGB")
        # 512 wide is MuseTalk's output scale, so the browser sees one geometry
        # regardless of which engine produced the frames.
        w = 512
        self._base = img.resize((w, int(img.height * w / img.width)), Image.LANCZOS)

    def avatars(self) -> List[str]:
        return [os.path.splitext(os.path.basename(self.image_path))[0]]

    def render(self, pcm: np.ndarray, sr: int, avatar: str) -> Iterator[bytes]:
        env = envelope(pcm, sr, self.fps)
        W, H = self._base.size
        # Mouth box as a fraction of the portrait, matching the landmarks the
        # frontend rig measured on this same photograph.
        mx, my, mw, mh = int(0.498 * W), int(0.4372 * H), int(0.16 * W), int(0.05 * H)
        for level in env:
            frame = self._base.copy()
            open_px = int(level * mh)
            if open_px > 1:
                # Darken an ellipse between the lips. Crude on purpose.
                from PIL import ImageDraw

                d = ImageDraw.Draw(frame, "RGBA")
                d.ellipse(
                    [mx - mw // 3, my, mx + mw // 3, my + open_px],
                    fill=(32, 16, 14, 220),
                )
            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=JPEG_QUALITY)
            yield buf.getvalue()


@dataclass
class MuseTalkEngine:
    """
    Real audio-driven lipsync.

    MuseTalk ships as a repo rather than a package, so this adapts its realtime
    inference path instead of importing a stable API. `musetalk_root` must be the
    checkout; `avatar_id` must already be prepared (the notebook does that once,
    because avatar preparation is the slow part — face parsing and latent
    caching — and repeating it per utterance would make the assistant unusable).
    """

    musetalk_root: str
    avatar_image: str
    avatar_id: str = "akansha"
    fps: float = DEFAULT_FPS
    bbox_shift: int = 0
    name: str = "musetalk"

    def __post_init__(self) -> None:
        if self.musetalk_root not in sys.path:
            sys.path.insert(0, self.musetalk_root)
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "MuseTalkEngine needs CUDA. Use --engine portrait on a CPU-only host."
            )
        self._torch = torch
        # Imported here, not at module scope, so the file still loads (and
        # PortraitEngine still runs) on a machine without the MuseTalk checkout.
        from scripts.realtime_inference import Avatar  # type: ignore

        self._avatar = Avatar(
            avatar_id=self.avatar_id,
            video_path=self.avatar_image,
            bbox_shift=self.bbox_shift,
            batch_size=4,
            preparation=not self._prepared(),
        )
        log.info("musetalk avatar ready: %s", self.avatar_id)

    def _prepared(self) -> bool:
        return os.path.isdir(os.path.join(self.musetalk_root, "results", "avatars", self.avatar_id))

    def avatars(self) -> List[str]:
        return [self.avatar_id]

    def render(self, pcm: np.ndarray, sr: int, avatar: str) -> Iterator[bytes]:
        import cv2  # type: ignore

        # MuseTalk's inference entry point reads audio from a path.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            wav_path = fh.name
        try:
            _write_wav(wav_path, pcm, sr)
            for bgr in self._avatar.inference_stream(wav_path, fps=self.fps):
                ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                if ok:
                    yield enc.tobytes()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(wav_path)


def _write_wav(path: str, pcm: np.ndarray, sr: int) -> None:
    import wave

    clipped = np.clip(pcm, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((clipped * 32767.0).astype("<i2").tobytes())


# — server ----------------------------------------------------------------


class _Session:
    """
    One client connection.

    Cancellation state and the in-flight task belong here rather than on the
    `Worker`, because a single `Worker` serves every connection: held there,
    one browser's barge-in would cancel another browser's sentence, and two
    clients that both happen to label an utterance `u1` would cancel each
    other. The engine lock stays on the `Worker` — that one really is global,
    because there is only one GPU.
    """

    def __init__(self) -> None:
        self.cancelled: set[str] = set()
        self.task: Optional[asyncio.Task[None]] = None


class Worker:
    """
    One engine, serialised across all clients.

    Inference runs in a thread (`asyncio.to_thread`) because both engines are
    synchronous and CPU/GPU-bound; running them on the event loop would stall
    the socket and the cancel message would never be read — which is precisely
    the message that needs to arrive mid-inference.
    """

    def __init__(self, engine: Engine, sr: int = 16000) -> None:
        self.engine = engine
        self.sr = sr
        self._lock = asyncio.Lock()

    async def handle(self, ws: Any) -> None:
        peer = getattr(ws, "remote_address", None)
        log.info("client connected: %s", peer)
        session = _Session()
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue  # clients never send binary
                msg = json.loads(raw)
                kind = msg.get("type")

                if kind == "hello":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ready",
                                "protocol": PROTOCOL,
                                "model": self.engine.name,
                                "fps": self.engine.fps,
                                "avatars": self.engine.avatars(),
                            }
                        )
                    )
                elif kind == "speak":
                    # Launched as a task rather than awaited. Awaiting it here
                    # would block this read loop for the whole render, so the
                    # `cancel` meant to interrupt it would sit unread in the
                    # socket until the render it was cancelling had finished —
                    # exactly backwards. A speak also supersedes its predecessor,
                    # because you cannot say two sentences at once.
                    await self._stop_current(session)
                    # Cleared here rather than inside `_speak`: `create_task`
                    # does not run the coroutine until the next loop tick, so a
                    # `cancel` for this very utterance can be read before it
                    # starts. Clearing there would discard that cancel and the
                    # barge-in would be silently lost. This loop is the only
                    # writer, so clearing at dispatch closes the gap — and it
                    # keeps the set to just the cancels seen since this speak.
                    session.cancelled.clear()
                    session.task = asyncio.create_task(self._speak(ws, session, msg))
                elif kind == "cancel":
                    session.cancelled.add(str(msg.get("id") or ""))
                elif kind == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._stop_current(session)
            log.info("client gone: %s", peer)

    async def _stop_current(self, session: _Session) -> None:
        """Retire the in-flight utterance, if any, before starting another."""
        task = session.task
        session.task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _speak(self, ws: Any, session: _Session, msg: Dict[str, Any]) -> None:
        utt = str(msg.get("id") or "")
        try:
            pcm = decode_audio(base64.b64decode(msg.get("audio") or ""), str(msg.get("format") or "mp3"), self.sr)
        except Exception as exc:  # noqa: BLE001
            await ws.send(json.dumps({"type": "error", "id": utt, "message": f"audio decode: {exc}"}))
            return

        fps = self.engine.fps
        total = int(math.ceil(len(pcm) / (self.sr / fps)))
        avatar = str(msg.get("avatar") or "")
        # One utterance at a time: two concurrent inferences on one GPU are
        # slower than two sequential ones and neither hits real-time.
        async with self._lock:
            t0 = time.monotonic()
            loop = asyncio.get_running_loop()
            # Bounded, so a slow tunnel applies back-pressure to the renderer
            # instead of letting finished frames pile up in memory. 8 frames is
            # a third of a second at 25fps — enough to absorb a network hiccup,
            # small enough that cancelling stops the GPU almost immediately.
            queue: asyncio.Queue = asyncio.Queue(maxsize=8)
            done = object()
            # Set when nobody is reading the queue any more — a failed send, a
            # vanished client, or this coroutine being cancelled outright.
            stop = threading.Event()

            def put(item: Any) -> bool:
                """
                Hand one item to the consumer. False once nobody is listening.

                Bounded on purpose. The obvious `run_coroutine_threadsafe(...)
                .result()` blocks forever if the consumer walks away, and this
                runs on an `asyncio.to_thread` pool thread — so every abandoned
                render would strand one thread for the life of the process.
                A dozen of those and `to_thread` never starts again: the worker
                stops rendering with no error and no log line, which reads as a
                hang. Re-checking `stop` on a timeout is what makes the producer
                responsible for its own exit instead of trusting the consumer.
                """
                while not stop.is_set():
                    fut = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
                    try:
                        fut.result(timeout=0.25)
                        return True
                    except concurrent.futures.TimeoutError:
                        if not fut.cancel():
                            # It landed in the gap between the timeout and the
                            # cancel, so the item is on the queue after all.
                            return True
                    except (asyncio.CancelledError, Exception):
                        return False  # the loop is gone, and so is the client
                return False

            def produce() -> None:
                """
                Pull frames off the engine one at a time, on a worker thread.

                Consuming the generator lazily rather than materialising a list
                is what makes `cancel` mean something: the check below runs
                between frames of *inference*, so barge-in stops the GPU part
                way through the sentence. Rendering everything up front and
                cancelling the send loop would leave MuseTalk finishing a
                sentence nobody will hear — and on a shared free T4 that stolen
                time is the next utterance's latency.
                """
                try:
                    for jpeg in self.engine.render(pcm, self.sr, avatar):
                        if stop.is_set() or utt in session.cancelled:
                            break
                        # Blocks until the consumer has room. That is the
                        # back-pressure; without it the queue bound is a lie.
                        if not put(jpeg):
                            break
                except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
                    put(exc)
                finally:
                    put(done)

            producer = asyncio.create_task(asyncio.to_thread(produce))
            sent = 0
            failed: Optional[BaseException] = None
            try:
                while True:
                    item = await queue.get()
                    if item is done:
                        break
                    if isinstance(item, BaseException):
                        failed = item
                        continue
                    if sent == 0:
                        h, w = _jpeg_size(item)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "meta",
                                    "id": utt,
                                    "fps": fps,
                                    "width": w,
                                    "height": h,
                                    # Estimated from audio duration, because the
                                    # engine is still rendering. The browser
                                    # sizes its buffer from this and grows it if
                                    # a later index overshoots.
                                    "frames": total,
                                }
                            )
                        )
                    await ws.send(struct.pack(">I", sent) + item)
                    sent += 1
            finally:
                # Tell the producer to stop and let it unwind itself. It polls
                # `stop` both between frames and inside `put`, so it exits within
                # one queue timeout whether or not this coroutine keeps draining
                # — which is what makes cleanup correct even when this task is
                # cancelled out from under us by a superseding speak. The await
                # is best-effort tidiness: the thread is already leaving.
                stop.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer

            if failed is not None:
                log.error("render failed for %s", utt, exc_info=failed)
                await ws.send(json.dumps({"type": "error", "id": utt, "message": f"render: {failed}"}))
                return
            if sent == 0:
                await ws.send(json.dumps({"type": "error", "id": utt, "message": "engine produced no frames"}))
                return

            await ws.send(json.dumps({"type": "end", "id": utt, "frames": sent}))
            log.info(
                "utterance %s: %d frames (%.1fs audio) in %.2fs",
                utt, sent, total / fps, time.monotonic() - t0,
            )


def _jpeg_size(jpeg: bytes) -> tuple[int, int]:
    """(height, width) from a JPEG header, without a decode."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        return im.height, im.width


def build_engine(args: argparse.Namespace) -> Engine:
    if args.engine == "musetalk":
        return MuseTalkEngine(
            musetalk_root=args.musetalk_root,
            avatar_image=args.avatar_image,
            avatar_id=args.avatar_id,
            fps=args.fps,
            bbox_shift=args.bbox_shift,
        )
    return PortraitEngine(image_path=args.avatar_image, fps=args.fps)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Avatar streaming worker")
    ap.add_argument("--engine", choices=["musetalk", "portrait"], default="musetalk")
    ap.add_argument("--avatar-image", required=True, help="portrait or driving video for the avatar")
    ap.add_argument("--avatar-id", default="akansha")
    ap.add_argument("--musetalk-root", default=os.getenv("MUSETALK_ROOT", "./MuseTalk"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--bbox-shift", type=int, default=0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = build_engine(args)
    if engine.name != "musetalk":
        log.warning(
            "running the %s engine — this is a transport stand-in, NOT a face model", engine.name
        )
    worker = Worker(engine)

    async with websockets.serve(
        worker.handle,
        args.host,
        args.port,
        max_size=None,      # frames are outbound; inbound audio can be large
        ping_interval=20,
    ):
        log.info("worker listening on ws://%s:%d  engine=%s", args.host, args.port, engine.name)
        await asyncio.Future()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
