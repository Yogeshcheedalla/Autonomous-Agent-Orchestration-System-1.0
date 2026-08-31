"""
The cascade has a wall clock.
=============================

Reported from a real session: *"a very high amount of latency is seen between the
responses ... No fallback reply is coming."* Both halves of that are one defect.

`generate_chat_stream` retries across message variants, then models, then token
budgets. Each individual request had a tight timeout -- 8 s -- but the product had
no limit at all: two variants times nine candidates is eighteen sequential
failures, 144 s of nothing, and the honest fallback that follows arrives so late
that it reads as no answer rather than as a late one. Nobody waits two minutes to
be told the provider is down.

So the cascade now gets a deadline, and these tests are about the deadline only.
What the fallback *says* is a separate concern with its own tests; here it is
stubbed to a sentinel so that a slow network cannot make a timing test flaky.
"""

from __future__ import annotations

import time

import pytest

from backend import ai_engine
from backend.database import SessionLocal

#: Deliberately not a question about anything live, current or attachable: those
#: take earlier branches, and this test is about the model cascade.
PROMPT = "Describe write-ahead logging in one short line."

SENTINEL = "[fallback reached]"


@pytest.fixture()
def dead_cascade(monkeypatch):
    """Nine candidates that all fail after 1 s, and a stubbed fallback.

    The budgets are shrunk so the test costs seconds rather than minutes. That is
    the same arithmetic either way: without a deadline this cascade takes
    `variants x candidates x delay`, with one it takes the budget.
    """
    attempts: list[float] = []
    candidates = [f"vendor/model-{index}:free" for index in range(9)]
    monkeypatch.setattr(ai_engine, "_openrouter_model_candidates", lambda: candidates)
    monkeypatch.setattr(ai_engine, "_voice_model_candidates", lambda: candidates[:2])
    monkeypatch.setattr(
        ai_engine, "_client_for_model", lambda *a, **k: _SlowlyFailingClient(1.0, attempts)
    )
    monkeypatch.setattr(ai_engine, "_provider_failure_fallback", lambda *a, **k: SENTINEL)
    monkeypatch.setattr(ai_engine, "_CASCADE_BUDGET_TEXT_S", 3.0)
    monkeypatch.setattr(ai_engine, "_CASCADE_BUDGET_VOICE_S", 2.0)
    monkeypatch.setattr(ai_engine, "_CASCADE_MIN_ATTEMPT_S", 0.5)
    return attempts


def _drain(session_id: str) -> tuple[str, float]:
    db = SessionLocal()
    started = time.time()
    try:
        reply = "".join(ai_engine.generate_chat_stream(db, PROMPT, session_id=session_id))
    finally:
        db.close()
    return reply, time.time() - started


def test_a_dead_cascade_gives_up_on_time_and_still_answers(dead_cascade):
    """Text: 3 s of budget against 18 attempts x 1 s of willing failure."""
    reply, elapsed = _drain("chat-test")

    assert SENTINEL in reply, "the user must get something, and get it from the fallback"
    # Generous ceiling on purpose. The claim is "bounded", not "precise": one
    # attempt may start just under the deadline and run its full second.
    assert elapsed < 8.0, f"cascade took {elapsed:.1f}s despite a 3s budget"
    assert len(dead_cascade) < 18, "it should stop trying, not try everything faster"


def test_voice_gets_a_shorter_clock_than_text(dead_cascade):
    """A listener waiting in silence is a harder deadline than a chat window."""
    _reply, elapsed = _drain("voice-test")

    assert elapsed < 6.0, f"voice cascade took {elapsed:.1f}s despite a 2s budget"


class _SlowlyFailingClient:
    """Every request costs `delay` seconds and then fails, like a dead route."""

    def __init__(self, delay: float, attempts: list[float]):
        self._delay = delay
        self._attempts = attempts
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **_kwargs):
        self._attempts.append(time.time())
        time.sleep(self._delay)
        raise RuntimeError("provider is unreachable")
