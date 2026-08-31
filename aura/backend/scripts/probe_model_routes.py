"""Judge OpenRouter routes on the prompt Akansha actually sends.

    python -m backend.scripts.probe_model_routes
    python -m backend.scripts.probe_model_routes minimax/minimax-m3:free ...

Free routes are withdrawn, throttled and re-priced without notice, so the model
lists in `ai_engine` go stale on their own. This is the check that refreshes
them, and it exists as a script rather than a test because it spends real
requests against a real key.

Two traps it exists to avoid, both of which produced a wrong answer when this
was done by hand:

1. *A toy prompt measures the wrong model.* Asked "what is a compiler?" with no
   system prompt, `nvidia/nemotron-3-super-120b-a12b:free` returned 147
   characters of clean content in 1.22 s with its thinking neatly separated into
   `delta.reasoning`. Given the real system prompt and history it put 1029
   characters of "Okay, the user is asking for..." into `content` and was cut off
   mid-thought. So every candidate is run through `generate_chat_stream`.

2. *A total provider failure still returns prose.* When no model answers,
   `generate_chat_stream` falls back to a cited web extract. It is honest -- it
   carries a "Source: [...]" line -- but it looks like a reply, and three
   different routes "answering" with the same 325 characters is what exposed it.
   So the run starts by asking a deliberately nonexistent model for the same
   thing and treats anything matching that baseline as NOFETCH, not ANSWER.

Verdicts: ANSWER (usable), NOTES (unmarked reasoning in the content channel --
text-only, never voice), EMPTY (spent its budget thinking), NOFETCH (the web
fallback answered, so the route itself is still unproven), FAIL (transport error,
with the provider's own words).
"""

from __future__ import annotations

import sys
import time

from backend.database import SessionLocal
from backend import ai_engine

#: The question is deliberately dull. What is being measured is the channel the
#: text arrives in and how long the first token takes, not the answer's quality.
PROMPT = "In one short sentence, what does a compiler do?"

BOGUS_MODEL = "definitely/not-a-real-model:free"

#: Phrases a model uses when it is talking to itself rather than to the user.
#: Deliberately not used to *strip* anything -- see `model_output`, which refuses
#: to guess at unmarked prose -- only to flag a route as unfit for voice.
NOTE_MARKERS = (
    "the user is asking",
    "the user wants",
    "thinking process",
    "okay, so",
    "first, i need",
    "let me think",
)

#: Give the free tier room between requests. Probing six routes back to back is
#: itself enough to earn a 429, which then reads as a dead route.
GAP_S = 4.0


def _looks_like_notes(text: str) -> bool:
    head = text[:400].lower()
    return any(marker in head for marker in NOTE_MARKERS)


def _reply_for(model: str) -> tuple[str, float | None, float]:
    """Stream one real reply, pinning the cascade to `model` alone."""
    ai_engine._openrouter_model_candidates = lambda: [model]
    ai_engine._voice_model_candidates = lambda: [model]
    db = SessionLocal()
    started = time.time()
    first: float | None = None
    pieces: list[str] = []
    try:
        for piece in ai_engine.generate_chat_stream(db, PROMPT):
            if piece and first is None:
                first = round(time.time() - started, 2)
            pieces.append(piece)
    finally:
        db.close()
    return "".join(pieces), first, time.time() - started


def main(models: list[str]) -> int:
    cloud = _openrouter_lists() if not models else models

    baseline, _, _ = _reply_for(BOGUS_MODEL)
    print(f"web-fallback baseline: {len(baseline)} chars\n")
    time.sleep(GAP_S)

    worst = 0
    for model in cloud:
        try:
            reply, first, total = _reply_for(model)
        except Exception as exc:  # noqa: BLE001 - the provider's words are the result
            print(f"FAIL    {model:42} {ascii(str(exc))[:100]}")
            worst = max(worst, 1)
            time.sleep(GAP_S)
            continue

        if reply.strip() and reply.strip() == baseline.strip():
            verdict = "NOFETCH"
        elif not reply.strip():
            verdict = "EMPTY"
        elif _looks_like_notes(reply):
            verdict = "NOTES"
        else:
            verdict = "ANSWER"
        print(f"{verdict:7} {model:42} first={first} total={total:.1f}s chars={len(reply)}")
        print(f"        {ascii(reply[:140])}")
        if verdict != "ANSWER":
            worst = max(worst, 1)
        time.sleep(GAP_S)
    return worst


def _openrouter_lists() -> list[str]:
    seen: list[str] = []
    for model in (*ai_engine.VOICE_MODELS, *ai_engine.OPENROUTER_FALLBACK_MODELS):
        if model not in seen:
            seen.append(model)
    return seen


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
