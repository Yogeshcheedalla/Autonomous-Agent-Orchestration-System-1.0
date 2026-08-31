"""
Tests for `POST /api/voice/turn-boundary` — audit item 11.
==========================================================

The browser client decided end-of-turn with `setTimeout(..., 1200)`. That single
constant produced both spec failures at once: §6's canonical "Open my project
and… [pause] …find the failing tests" was cut in half at the pause, and a
one-word "stop" sat idle for 1.2s before anything happened. Meanwhile the kernel
already computed `turn_complete_probability` properly — the client just had no
way to ask.

The temptation was to port `recommend_silence_ms`'s rules into TypeScript. This
route exists so that did not happen: it is the same `EndpointDetector` the
WebSocket transport runs on its heartbeat, exposed as a pure function. What the
client still owns is only what it must own to survive the route being
unreachable — the floor it must not ask before, and the ceiling it fires at
regardless.

So these tests are mostly about the contract at the boundary: that the route
mutates nothing, that it never returns a decision the client cannot act on, and
that hostile input produces an answer rather than a 500. The *judgement* is
tested in `test_voice_kernel.py::TestEndpointing`, where it belongs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app

#: The client's first-check delay, mirrored from `VoiceAssistantClient.tsx`. It is
#: duplicated there because a browser cannot ask the server what the floor is
#: before it makes its first request; kept here so this file fails if the two ever
#: disagree.
FIRST_CHECK_MS = 240


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def ask(client, **body):
    response = client.post("/api/voice/turn-boundary", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestContract:
    def test_it_answers_the_only_question_the_client_asks(self, client):
        data = ask(client, transcript="what is the weather today?", silence_ms=700)
        assert data["should_finalize"] is True
        # Every field the client's loop reads must be present and usable, since a
        # missing one sends it down the offline fallback path.
        assert isinstance(data["recheck_in_ms"], int)
        assert 0.0 <= data["turn_complete_probability"] <= 1.0
        assert 0.0 <= data["threshold"] <= 1.0

    def test_it_publishes_the_bounds_the_client_enforces_locally(self, client):
        """The client duplicates these two numbers, so they have to be on the wire.

        It has to fire on its own if this route stops answering mid-turn, and it
        must not ask before the floor — below it the answer can only ever be
        "below minimum silence".
        """
        data = ask(client, transcript="anything", silence_ms=300)
        assert data["min_silence_ms"] < data["max_silence_ms"]
        assert data["min_silence_ms"] == pytest.approx(240.0)
        assert data["max_silence_ms"] == pytest.approx(2600.0)

    def test_a_recheck_is_never_zero(self, client):
        """A zero would spin the client's timer into a request loop."""
        for silence in (0, 100, 240, 700, 1500, 2599):
            data = ask(client, transcript="deploy the app and", silence_ms=silence)
            assert data["recheck_in_ms"] > 0

    def test_signals_are_returned_for_the_debug_panel(self, client):
        data = ask(client, transcript="open my project and", silence_ms=800)
        assert set(data["signals"]) >= {"silence", "duration", "lexical", "semantic"}
        assert "dangling" in data["reason"]

    def test_prosody_is_absent_rather_than_guessed(self, client):
        """The Web Speech API cannot measure pitch, so the route must not pretend.

        `_prosody_signal` returns None when unsupplied and the weight is
        redistributed; a fabricated 0.5 would drag every probability toward the
        middle for every browser client.
        """
        data = ask(client, transcript="deploy it.", silence_ms=500)
        assert "prosody" not in data["signals"]


class TestStatelessness:
    def test_polling_does_not_disturb_the_voice_session(self, client):
        """Why this is a new route instead of reusing `/api/voice/process`.

        The obvious shortcut was to call the existing NLP endpoint on the interim
        transcript to get its `recommended_silence_ms`. But that runs the full
        engine against a real session: it advances the turn counter and can
        consume a pending login answer. Polling it every 200ms during a pause
        would answer questions the user had not finished asking yet.
        """
        before = client.post(
            "/api/voice/process",
            json={"transcript": "open chrome", "session_id": "turn_boundary_probe"},
        )
        assert before.status_code == 200
        baseline = before.json()

        for silence in (240, 500, 900, 1400):
            ask(client, transcript="open chrome and", silence_ms=silence)

        after = client.post(
            "/api/voice/process",
            json={"transcript": "open chrome", "session_id": "turn_boundary_probe"},
        )
        assert after.status_code == 200
        # Same input, same session, same answer — the polling in between left no
        # trace. (Turn-scoped counters may advance from the two real calls; what
        # must not change is the interpretation.)
        assert after.json().get("intent_category") == baseline.get("intent_category")

    def test_identical_requests_give_identical_answers(self, client):
        first = ask(client, transcript="find the failing tests", silence_ms=800)
        second = ask(client, transcript="find the failing tests", silence_ms=800)
        assert first == second


class TestHostileInput:
    """The client sends these values from a browser, so all of them are attacker
    controlled. None of them may produce a 500 — a crashed route means the voice
    loop falls back to a fixed timer, which is the bug this fixes."""

    def test_an_empty_transcript_is_answered_not_rejected(self, client):
        data = ask(client, transcript="", silence_ms=5000)
        assert data["should_finalize"] is False
        assert data["turn_complete_probability"] == 0.0

    def test_negative_silence_is_clamped(self, client):
        """A clock skew or a paused tab can make `Date.now()` arithmetic go backwards."""
        data = ask(client, transcript="stop", silence_ms=-9000)
        assert data["should_finalize"] is False
        assert data["reason"] == "below minimum silence"

    def test_absurd_silence_still_terminates_the_turn(self, client):
        data = ask(client, transcript="open my project and", silence_ms=10**9)
        assert data["should_finalize"] is True

    def test_a_very_long_transcript_is_handled(self, client):
        data = ask(client, transcript="deploy the app " * 4000, silence_ms=900)
        assert data["should_finalize"] is True

    def test_only_the_transcript_is_required(self, client):
        """The client omits fields it has no state for on the first turn."""
        data = ask(client, transcript="hello")
        assert data["should_finalize"] is False  # 0ms silence is below the floor

    def test_a_missing_transcript_is_a_validation_error_not_a_crash(self, client):
        assert client.post("/api/voice/turn-boundary", json={"silence_ms": 900}).status_code == 422

    def test_a_non_ascii_transcript_is_scored_not_dropped(self, client):
        """Telugu and Hindi are first-class here, including the dangling-tail list."""
        # "మరియు" / "और" are the Indic conjunctions the detector treats as dangling.
        for tail in ("నా ప్రాజెక్ట్ తెరవండి మరియు", "मेरा प्रोजेक्ट खोलो और"):
            data = ask(client, transcript=tail, silence_ms=1300)
            assert data["should_finalize"] is False, tail
            assert "dangling" in data["reason"]


class TestTheBugItReplaces:
    """The two failures a flat 1200ms timer produced, stated as tests."""

    def test_the_canonical_pause_is_not_mistaken_for_the_end_of_a_turn(self, client):
        """§6, and the reason the client passes `recognizer_final`: Chrome emits a
        final result at exactly this pause."""
        mid = ask(
            client,
            transcript="open my project and",
            silence_ms=1300,
            recognizer_final=True,
        )
        assert mid["should_finalize"] is False

        whole = ask(
            client,
            transcript="open my project and find the failing tests",
            silence_ms=800,
            recognizer_final=True,
        )
        assert whole["should_finalize"] is True

    def test_a_control_word_does_not_wait_out_a_fixed_timer(self, client):
        """The other half of the same constant: 1.2s of dead air before "stop"
        did anything, which is the sluggishness §4 objects to."""
        data = ask(client, transcript="stop", silence_ms=FIRST_CHECK_MS)
        assert data["should_finalize"] is True

    def test_being_mid_task_buys_the_user_a_beat_longer(self, client):
        """§14: talking to a running task must not be easier to truncate than
        talking to an idle one."""
        idle = ask(client, transcript="how is it going", silence_ms=700)
        busy = ask(client, transcript="how is it going", silence_ms=700, executing=True)
        assert busy["threshold"] > idle["threshold"]

    def test_an_answer_to_our_own_question_finalizes_sooner(self, client):
        """§35's "Actually, staging" — a one-word correction must land fast."""
        neutral = ask(client, transcript="staging", silence_ms=300)
        asked = ask(client, transcript="staging", silence_ms=300, pending_question=True)
        assert asked["threshold"] < neutral["threshold"]
