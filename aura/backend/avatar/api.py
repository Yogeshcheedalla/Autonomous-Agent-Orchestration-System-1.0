"""
router — `/ws/avatar/{session_id}` plus the small settings API Colab needs.

The backend is a relay, not a renderer. It owns three things the browser cannot:
the TTS voice, the single shared link to the GPU, and the decision to fall back.

Ordering on the wire is load-bearing:

    ← {"type":"audio", ...}    the whole utterance, first
    ← {"type":"meta",  ...}    fps / frame count
    ← <binary frames>          uint32be index ‖ JPEG
    ← {"type":"end"}   or  {"type":"fallback","reason":...}

Two ways in. `speak` takes text and synthesizes here; `speak_audio` takes audio
the client is already playing and only renders the face. The voice page uses the
latter, because `useVoice` owns the `<audio>` element and its analyser — see
`_relay_utterance` for why a second synthesis would desync the face.

Audio goes first because it is the clock. The browser starts playback and then
draws whichever frame `currentTime × fps` selects, so a frame arriving late is
skipped rather than shifting everything after it. Sending frames first and audio
last would force the browser to either buffer the whole utterance before making
a sound, or start speaking with no way to know where in the stream it is.

`fallback` is a first-class outcome, not an error path. A stale Colab URL, a
disconnected runtime, or an OOM on a shared T4 are all *expected* several times
a day, and the correct response to every one of them is for the browser to keep
talking with the CSS rig on its face.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import struct
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .worker_client import (
    WorkerFrame,
    WorkerMeta,
    WorkerUnavailable,
    get_worker_client,
    worker_settings,
)

log = logging.getLogger("akansha.avatar.router")

router = APIRouter(tags=["avatar"])

#: A single utterance of speech. Longer than this is a paragraph, and a
#: paragraph should be chunked by the caller so barge-in stays responsive.
MAX_SPEAK_CHARS = 1200

#: Ceiling on a `speak_audio` payload. `MAX_SPEAK_CHARS` of speech is roughly a
#: minute and a half, which `edge_tts` encodes to a few hundred KB; 2 MB leaves
#: room for a higher-bitrate voice without letting a client stream video at us.
MAX_AUDIO_BYTES = 2_000_000

#: Frame-size cap, applied to the raw text before parsing. It has to clear
#: base64-encoded audio (a third larger than the bytes), so it cannot be the
#: tight control-message bound it would otherwise be — `speak_audio` is the only
#: message that is ever more than a few hundred bytes.
MAX_MESSAGE_BYTES = int(MAX_AUDIO_BYTES * 4 / 3) + 4096


# — TTS injection ---------------------------------------------------------
#
# `main.py` owns `generate_edge_tts_audio` (voice gender, tone→prosody mapping,
# language mode). Importing it here would be circular, since `main` imports this
# router, so it is injected on startup exactly as `voice_ws.set_executor` does.

TtsProvider = Callable[..., Awaitable[bytes]]
_tts: Optional[TtsProvider] = None


def set_tts_provider(provider: Optional[TtsProvider]) -> None:
    """Register the coroutine that turns text into audio bytes."""
    global _tts
    _tts = provider


async def _synthesize(text: str, voice: str, tone: Optional[str], lang: Optional[str]) -> bytes:
    if _tts is None:
        raise WorkerUnavailable("no TTS provider registered")
    return await _tts(text, voice, tone, lang)


# — settings API ----------------------------------------------------------


class WorkerConfig(BaseModel):
    """Colab hands out a fresh tunnel URL on every restart; this accepts it."""

    url: str = Field(default="", description="wss:// URL of the MuseTalk worker")
    avatar: Optional[str] = Field(default=None, description="avatar id registered on the worker")


@router.get("/api/avatar/status")
async def avatar_status() -> Dict[str, Any]:
    """Report whether a GPU worker is reachable, and what it is."""
    return await get_worker_client().probe()


@router.post("/api/avatar/worker")
async def set_avatar_worker(cfg: WorkerConfig) -> Dict[str, Any]:
    """
    Point the backend at a worker, or clear it with an empty URL.

    Reconnects eagerly so the caller learns immediately whether the URL works,
    rather than discovering it on the first spoken sentence.
    """
    client = get_worker_client()
    await client.close()
    worker_settings.url = (cfg.url or "").strip()
    if cfg.avatar:
        worker_settings.avatar = cfg.avatar.strip()
    worker_settings.model = None
    worker_settings.fps = None
    worker_settings.last_error = None
    if not worker_settings.url:
        return {"worker": "absent", "reason": "cleared", **worker_settings.snapshot()}
    return await client.probe()


# — streaming socket ------------------------------------------------------


async def _relay_utterance(
    ws: WebSocket,
    utterance_id: str,
    text: str,
    voice: str,
    tone: Optional[str],
    lang: Optional[str],
    cancel: asyncio.Event,
    *,
    audio: Optional[bytes] = None,
    fmt: str = "mp3",
) -> None:
    """
    Speak one utterance: obtain audio, then stream the rendered face.

    `audio` may be supplied by the caller. `useVoice` already fetches
    `/api/voice/tts`, owns the `<audio>` element and has an `AnalyserNode` on
    it, so when the browser drives this socket it sends the audio it is
    *already playing* rather than asking for a second synthesis. Two syntheses
    would mean two different waveforms — the face would be lipsyncing to audio
    nobody can hear, drifting from the audio they can.

    Any failure past the audio send is a `fallback`, not a close: the browser
    already has the audio and can finish the sentence on the CSS rig.
    """
    started = time.monotonic()
    if audio is None:
        try:
            audio = await _synthesize(text, voice, tone, lang)
        except Exception as exc:  # noqa: BLE001 — no audio means nothing to salvage
            log.warning("avatar tts failed: %s", exc)
            await ws.send_json({"type": "error", "id": utterance_id, "message": f"tts failed: {exc}"})
            return
        # Only echo audio the client does not already have.
        await ws.send_json(
            {
                "type": "audio",
                "id": utterance_id,
                "format": fmt,
                "b64": base64.b64encode(audio).decode("ascii"),
            }
        )
    else:
        # The clock is already running in the browser; just acknowledge it so
        # the ordering guarantee (audio → meta → frames) still holds on the wire.
        await ws.send_json({"type": "audio", "id": utterance_id, "format": fmt, "client_audio": True})

    client = get_worker_client()
    if not client.configured:
        await ws.send_json({"type": "fallback", "id": utterance_id, "reason": "no worker configured"})
        return

    frames = 0
    try:
        async for item in client.speak(utterance_id, audio, fmt=fmt, cancel=cancel):
            if cancel.is_set():
                break
            if isinstance(item, WorkerMeta):
                await ws.send_json(
                    {
                        "type": "meta",
                        "id": item.utterance_id,
                        "fps": item.fps,
                        "width": item.width,
                        "height": item.height,
                        "frames": item.frames,
                    }
                )
            elif isinstance(item, WorkerFrame):
                await ws.send_bytes(struct.pack(">I", item.index) + item.jpeg)
                frames += 1
    except WorkerUnavailable as exc:
        log.info("avatar worker unavailable mid-utterance: %s", exc)
        await ws.send_json({"type": "fallback", "id": utterance_id, "reason": str(exc)})
        return
    except (WebSocketDisconnect, RuntimeError):
        return  # browser went away; nothing to report to

    await ws.send_json(
        {
            "type": "end",
            "id": utterance_id,
            "frames": frames,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    )


@router.websocket("/ws/avatar/{session_id}")
async def avatar_socket(ws: WebSocket, session_id: str) -> None:
    """
    One browser tab's face channel.

    Utterances are serialised per socket: `_task` holds the in-flight one, and a
    new `speak` cancels it first. That mirrors how speech actually works — you
    cannot say two sentences at once — and it is what makes barge-in a two-line
    operation instead of a queue-draining problem.
    """
    await ws.accept()
    cancel = asyncio.Event()
    task: Optional[asyncio.Task[None]] = None

    status = await get_worker_client().probe()
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "status", "session": session_id, **status})

    async def _stop_current() -> None:
        nonlocal task
        cancel.set()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        task = None

    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_BYTES:
                await ws.send_json({"type": "error", "message": "message too large"})
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "malformed json"})
                continue

            kind = msg.get("type")

            if kind == "speak":
                text = str(msg.get("text") or "").strip()
                if not text:
                    await ws.send_json({"type": "error", "message": "speak needs text"})
                    continue
                if len(text) > MAX_SPEAK_CHARS:
                    text = text[:MAX_SPEAK_CHARS]
                await _stop_current()
                cancel = asyncio.Event()
                task = asyncio.create_task(
                    _relay_utterance(
                        ws,
                        str(msg.get("id") or uuid.uuid4().hex[:12]),
                        text,
                        str(msg.get("voice") or "female"),
                        msg.get("tone"),
                        msg.get("lang"),
                        cancel,
                    )
                )
            elif kind == "speak_audio":
                # The browser is already playing this audio and wants a face for
                # it. Preferred over `speak` on any page that has its own TTS,
                # because it guarantees the face and the sound share a waveform.
                try:
                    audio = base64.b64decode(msg.get("b64") or "", validate=True)
                except (ValueError, TypeError):
                    await ws.send_json({"type": "error", "message": "speak_audio needs base64 b64"})
                    continue
                if not audio:
                    await ws.send_json({"type": "error", "message": "speak_audio needs audio"})
                    continue
                if len(audio) > MAX_AUDIO_BYTES:
                    await ws.send_json({"type": "error", "message": "audio too large"})
                    continue
                await _stop_current()
                cancel = asyncio.Event()
                task = asyncio.create_task(
                    _relay_utterance(
                        ws,
                        str(msg.get("id") or uuid.uuid4().hex[:12]),
                        "",
                        "female",
                        None,
                        None,
                        cancel,
                        audio=audio,
                        fmt=str(msg.get("format") or "mp3"),
                    )
                )
            elif kind == "cancel":
                await _stop_current()
                await ws.send_json({"type": "cancelled", "id": msg.get("id")})
            elif kind == "status":
                await ws.send_json({"type": "status", "session": session_id, **await get_worker_client().probe()})
            elif kind == "ping":
                await ws.send_json({"type": "pong", "t": time.time()})
            else:
                await ws.send_json({"type": "error", "message": f"unknown type {kind!r}"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — a relay must not take the app down
        log.warning("avatar socket %s failed: %s", session_id, exc)
    finally:
        await _stop_current()
        with contextlib.suppress(Exception):
            await ws.close()
