"""
Tests for `backend.voice_ws` — the duplex channel the old voice path lacked.
===========================================================================

These are transport tests, not kernel tests: the kernel's decisions are covered
in `test_voice_kernel.py`. What matters here is that a frame in produces the
right frames out, that a bad frame cannot kill the socket, that a barge-in
reaches the client mid-utterance (§4 — the whole reason this endpoint exists),
and that a reconnect does not throw the plan away (§37).

The router is mounted on a bare app rather than importing `backend.main`, so a
failure here points at the transport instead of at 6k lines of unrelated routes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.voice_kernel import DirectiveKind, get_registry
from backend import voice_ws
from backend.voice_ws import router


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("AKANSHA_VOICE_WS_TOKEN", raising=False)
    # Idle frames arrive fast so `until` fails in seconds instead of hanging.
    monkeypatch.setattr(voice_ws, "KEEPALIVE_S", 0.2)
    app = FastAPI()
    app.include_router(router)
    registry = get_registry()
    for session_id in list(registry.ids()):
        registry.drop(session_id)
    return TestClient(app)


def until(ws, directive: str, limit: int = 30):
    """Read frames until `directive` arrives, returning it.

    The endpointing heartbeat emits frames on its own schedule, so a test that
    asserted on frame *order* would be flaky by construction. The idle `tick`
    frame guarantees this loop always terminates.
    """
    seen = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(frame.get("directive"))
        if frame.get("directive") == directive:
            return frame
    raise AssertionError(f"never saw {directive!r}; got {seen}")


def take_floor(ws):
    """Drive the session to SPEAKING the way the real pipeline does.

    LISTENING → UNDERSTANDING → RESPONDING → SPEAKING. Skipping steps would
    test a state the kernel never actually reaches.
    """
    ws.send_json({"event": "final_transcript", "text": "what is on my screen",
                  "confidence": 0.95, "silence_ms": 800})
    until(ws, DirectiveKind.GENERATE_RESPONSE.value)
    ws.send_json({"event": "response_token", "token": "Looking at it now."})
    ws.send_json({"event": "tts_started", "text": "Looking at it now."})


def open_socket(client, session_id="ws-test", **params):
    query = "&".join(f"{k}={v}" for k, v in {"wake_word": "0", **params}.items())
    return client.websocket_connect(f"/ws/voice/{session_id}?{query}")


class TestHandshake:
    def test_connect_announces_the_session_and_starts_listening(self, client):
        with open_socket(client) as ws:
            ready = ws.receive_json()
            assert ready["directive"] == "session_ready"
            assert ready["session_id"] == "ws-test"
            assert ready["reconnected"] is False
            assert ready["config"]["require_wake_word"] is False
            start = until(ws, DirectiveKind.START_LISTENING.value)
            assert start["needs_wake_word"] is False

    def test_reconnect_keeps_the_same_kernel_session(self, client):
        with open_socket(client, session_id="sticky") as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
        with open_socket(client, session_id="sticky") as ws:
            assert ws.receive_json()["reconnected"] is True

    def test_explicit_close_drops_the_session(self, client):
        with open_socket(client, session_id="bye") as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_json({"event": "session_close"})
            until(ws, DirectiveKind.STOP_LISTENING.value)
        assert "bye" not in get_registry().ids()

    def test_a_token_is_enforced_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("AKANSHA_VOICE_WS_TOKEN", "s3cret")
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/voice/guarded") as ws:
                ws.receive_json()
        with client.websocket_connect("/ws/voice/guarded?token=s3cret") as ws:
            assert ws.receive_json()["directive"] == "session_ready"


class TestTurnTaking:
    def test_unfinished_sentence_is_not_submitted(self, client):
        with open_socket(client) as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_json({
                "event": "partial_transcript",
                "text": "open my project and",
                "silence_ms": 900,
            })
            keep = until(ws, DirectiveKind.KEEP_LISTENING.value)
            assert "dangling" in keep["reason"]

    def test_completed_sentence_finalizes(self, client):
        with open_socket(client) as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_json({
                "event": "final_transcript",
                "text": "open my project and find the failing tests",
                "confidence": 0.97,
                "silence_ms": 800,
            })
            final = until(ws, DirectiveKind.FINALIZE_TURN.value)
            assert final["transcript"] == "open my project and find the failing tests"
            until(ws, DirectiveKind.EXECUTE_PLAN.value)

    def test_barge_in_reaches_the_client_mid_utterance(self, client):
        # §4: this is the exchange that one-shot HTTP physically cannot carry.
        with open_socket(client, session_id="barge") as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            take_floor(ws)
            ws.send_json({"event": "speech_start", "source": "user"})
            stop = until(ws, DirectiveKind.STOP_TTS.value)
            assert stop["reason"]

    def test_assistant_audio_is_not_mistaken_for_the_user(self, client):
        # §5: the echo of our own voice must never trigger a barge-in.
        with open_socket(client, session_id="echo") as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            take_floor(ws)
            ws.send_json({"event": "speech_start", "source": "assistant"})
            frames = []
            for _ in range(12):
                frame = ws.receive_json()
                frames.append(frame["directive"])
                if frame["directive"] == "tick":
                    break
            assert "stop_tts" not in frames, frames
            assert frames[-1] == "tick", frames


class TestBadFrames:
    def test_environment_updates_reach_the_situation_model(self, client):
        # §21 — the kernel's world model is fed over the same socket.
        with open_socket(client, session_id="env") as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_json({"event": "environment_update",
                          "environment": {"active_window": "Code"}})
            frame = until(ws, DirectiveKind.SITUATION_CHANGED.value)
            assert frame["environment"]["active_window"] == "Code"

    def test_unknown_event_is_reported_and_the_socket_survives(self, client):
        with open_socket(client) as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_json({"event": "make_me_a_sandwich"})
            error = until(ws, "error")
            assert "unknown event" in error["reason"]
            ws.send_json({"event": "final_transcript", "text": "stop", "confidence": 0.9})
            until(ws, DirectiveKind.SPEAK.value)

    def test_malformed_json_is_reported(self, client):
        with open_socket(client) as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_text("{not json")
            assert "invalid json" in until(ws, "error")["reason"]

    def test_oversized_frame_is_rejected_without_parsing(self, client):
        with open_socket(client) as ws:
            until(ws, DirectiveKind.START_LISTENING.value)
            ws.send_text('{"event":"final_transcript","text":"' + "a" * 70_000 + '"}')
            assert until(ws, "error")["reason"] == "frame too large"
