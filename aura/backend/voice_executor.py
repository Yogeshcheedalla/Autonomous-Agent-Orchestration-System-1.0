"""
voice_executor — the side-effecting half of the voice loop.
==========================================================

`voice_ws` deliberately does no I/O beyond its socket: the kernel decides *what*
should happen and hands out `Directive`s, and something else has to actually call
a model and drive a browser. That something is registered with
`voice_ws.set_executor`.

Nothing registered it. `set_executor` existed, was documented, and was never
called anywhere in the product — so `/ws/voice/{session_id}`, the only duplex
channel in the backend and the only path on which barge-in is possible at all,
forwarded `generate_response` and `execute_plan` to a client that does not
implement them. The kernel was complete, unit-tested, and inert. This module is
the missing half.

Three directives need doing:

  * `generate_response` — call the model and stream it back. This is an async
    generator, not a coroutine returning a list, because §19 wants the first
    sentence spoken while the third is still being generated. Returning a list
    would make every token arrive after the whole reply existed.
  * `execute_plan` — actually control the browser or the desktop, via the same
    two paths `/api/automation/browser/prompt` uses. No new automation is
    invented here; this is a bridge, not an engine.
  * `compress_context` — §12. Summarise the oldest turns so a long conversation
    does not silently start dropping them.

The prompt is taken from the kernel, not rebuilt. `directive.payload["context"]`
is an assembled OpenAI-shaped message list that already carries the §11 memory
block, the §12 summary chain, the situation line and the current task — and was
already trimmed to the window without evicting the memory block. Routing through
`ai_engine.generate_chat_stream` instead would throw all of that away, because it
takes a bare `user_input` and rebuilds history from the database. That is the
older path's context model, and having two is how "deploy to staging, not
production" gets lost.

Cancellation is cooperative and only honest at step boundaries. `Task.cancel()`
reaches an `await`, not a thread already inside pyautogui, so long work is handed
a `threading.Event` and checks it between steps. "Stop" therefore means "stop
before the next step", and the narration says so rather than implying the current
keystroke can be recalled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, AsyncIterator, Dict, List, Optional

from .model_output import StreamSanitizer, polish_for_speech
from .voice_kernel import Directive, DirectiveKind, Event, EventKind, VoiceSession

log = logging.getLogger("akansha.voice_executor")

#: How long to wait for the model to produce anything at all before giving up and
#: telling the user. Long enough for a cold OpenRouter route, short enough that
#: silence is never mistaken for thinking.
FIRST_TOKEN_TIMEOUT_S = 25.0

#: Between-token timeout. A stream that stalls mid-sentence has failed, and the
#: user is sitting there listening to half an answer.
TOKEN_TIMEOUT_S = 30.0

#: Ceiling on one automation step. pyautogui can block indefinitely on a modal
#: dialog, and a wedged step must not wedge the conversation.
STEP_TIMEOUT_S = 180.0


def _messages(directive: Directive) -> List[Dict[str, str]]:
    context = directive.payload.get("context") or {}
    messages = context.get("messages") or []
    return [m for m in messages if m.get("content")]


def _stop_flag(directive: Directive) -> threading.Event:
    flag = directive.payload.get("stop_flag")
    return flag if isinstance(flag, threading.Event) else threading.Event()


# ── §19: streaming generation ────────────────────────────────────────────────
def _stream_into(
    queue: "asyncio.Queue[tuple[str, Any]]",
    loop: asyncio.AbstractEventLoop,
    messages: List[Dict[str, str]],
    model: Optional[str],
    stop: threading.Event,
) -> None:
    """Pump an OpenRouter stream into `queue` from a worker thread.

    The OpenAI client here is the synchronous one, so iterating it blocks. Doing
    that on the event loop would freeze the endpointing heartbeat and every other
    socket — the barge-in this whole path exists for would stop working for as
    long as the model was talking.
    """
    def push(kind: str, value: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

    try:
        from .ai_engine import _client_for_model, _voice_model_candidates, note_model_failure
        from . import model_routes

        candidates = [model] if model else []
        candidates += [m for m in _voice_model_candidates() if m not in candidates]

        last_error: Optional[Exception] = None
        for candidate in candidates:
            if stop.is_set():
                push("cancelled", None)
                return
            try:
                stream = _client_for_model(candidate).chat.completions.create(
                    model=model_routes.wire_name(candidate),
                    messages=messages,
                    stream=True,
                )
            except Exception as exc:  # try the next model rather than giving up
                last_error = exc
                # Demote a route that failed structurally so the *next* utterance
                # does not open with the same dead round trip. Audible here: this
                # cost sits in front of the first spoken word.
                note_model_failure(candidate, exc)
                log.warning("voice_executor: %s unavailable (%s)", candidate, exc)
                continue

            # Filtering here rather than downstream is deliberate: `produced` has
            # to mean "produced something speakable". A model that streamed 1700
            # characters of chain-of-thought and nothing else has failed, and the
            # next candidate should get a turn instead of the user hearing it.
            sanitizer = StreamSanitizer(for_speech=True)
            produced = False
            for chunk in stream:
                if stop.is_set():
                    push("cancelled", None)
                    return
                try:
                    delta = chunk.choices[0].delta.content
                except (AttributeError, IndexError):
                    continue
                if not delta:
                    continue
                clean = sanitizer.feed(delta)
                if clean:
                    produced = True
                    push("token", clean)
            tail = sanitizer.finish()
            if tail:
                produced = True
                push("token", tail)
            if produced:
                push("done", None)
                return
            # A model that connected but said nothing is a failure, not an empty
            # answer — fall through to the next candidate.
            last_error = last_error or RuntimeError(f"{candidate} returned no speakable content")

        push("error", str(last_error) if last_error else "no model produced a response")
    except Exception as exc:
        push("error", str(exc))


async def _generate_response(
    session: VoiceSession, directive: Directive
) -> AsyncIterator[Event]:
    """Stream the model's reply back as `response_token` events."""
    messages = _messages(directive)
    if not messages:
        yield Event(EventKind.RESPONSE_COMPLETE, {"text": "I did not catch that."})
        return

    stop = _stop_flag(directive)
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[tuple[str, Any]]" = asyncio.Queue()
    model = (directive.payload.get("context") or {}).get("model")

    worker = loop.run_in_executor(None, _stream_into, queue, loop, messages, model, stop)
    collected: List[str] = []
    timeout = FIRST_TOKEN_TIMEOUT_S
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                stop.set()
                if collected:
                    # Half an answer already reached the user's ears, so finish
                    # the turn rather than contradicting it with an error.
                    yield Event(
                        EventKind.RESPONSE_COMPLETE,
                        {"text": polish_for_speech("".join(collected))},
                    )
                else:
                    yield Event(EventKind.STT_ERROR, {"error": "the model did not respond"})
                return

            if kind == "token":
                collected.append(value)
                timeout = TOKEN_TIMEOUT_S
                yield Event(EventKind.RESPONSE_TOKEN, {"text": value})
            elif kind == "done":
                # `polish_for_speech` handles what a stream cannot: link syntax and
                # line-leading bullets need a whole line in hand.
                yield Event(
                    EventKind.RESPONSE_COMPLETE,
                    {"text": polish_for_speech("".join(collected))},
                )
                return
            elif kind == "cancelled":
                # The user cut in. The kernel already reset its buffer for the new
                # turn, so completing this one would speak the old answer.
                return
            else:
                log.warning("voice_executor: generation failed: %s", value)
                if collected:
                    yield Event(
                        EventKind.RESPONSE_COMPLETE,
                        {"text": polish_for_speech("".join(collected))},
                    )
                else:
                    yield Event(EventKind.ERROR, {"error": str(value), "source": "model"})
                return
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        stop.set()
        worker.cancel()


def _try_connected_app(prompt: str, stop: threading.Event) -> Dict[str, Any] | None:
    """First refusal goes to the connections registry. `None` means "not mine".

    This is the bridge the sign-in work was missing. `build_browser_prompt_plan`
    below knows nothing about which sites the user is signed into, so "post this on
    X" used to become a desktop plan -- it would launch something, or write a file,
    and the account the person authenticated was never touched. Asking the registry
    first means a named, connected app is driven through the route it is actually
    connected by.

    Returning `None` rather than a failure for anything it does not recognise is the
    important half: this must not become a second, worse plan builder. No app named,
    or an app with nothing to drive, and the old path runs exactly as before.
    """
    try:
        from .database import SessionLocal
        from .main import _run_decision, operate_decision
    except Exception as exc:  # pragma: no cover - import-time only
        log.warning("voice_executor: connected-app route unavailable: %s", exc)
        return None

    db = SessionLocal()
    try:
        decision = operate_decision(db, prompt)
        if decision.grant is None:
            return None  # No app named. Not ours.
        if not decision.runnable:
            # A named, known app that cannot do this. That *is* an answer -- and a much
            # better one than letting the pyautogui plan launch something instead.
            return {
                "success": False,
                "summary": decision.blocked,
                "steps": [],
                "engine": "connected_app",
                "reasoning": decision.reasoning,
                "needs": decision.needs,
            }
        report = _run_decision(db, decision, stop=stop.is_set)
    finally:
        db.close()

    return {
        "success": bool(report.get("ok")),
        "summary": report.get("summary") or decision.summary,
        "steps": report.get("steps") or [],
        "engine": "connected_app",
        "reasoning": report.get("reasoning") or decision.reasoning,
        "text": report.get("text") or "",
        "cancelled": bool(report.get("cancelled")),
        "held": bool(report.get("truncated")),
    }


#: Only an explicit declaration counts as setting a goal. "Open WhatsApp" is an
#: instruction and must stay one; hijacking ordinary commands into multi-step
#: goal pursuit would make every sentence unpredictable.
_GOAL_DECLARATIONS = (
    "set a goal",
    "set the goal",
    "new goal",
    "my goal is",
    "the goal is",
    "goal for today",
    "i want to achieve",
    "help me achieve",
    "work towards",
    "work toward",
)


def _try_goal(prompt: str, stop: threading.Event) -> Dict[str, Any] | None:
    """“Set a goal to …” — create it, decompose it, and start on it. `None` if not a goal.

    Pursuit runs with `through_commit=False`, so a goal that ends in something
    irreversible drafts it and stops for a second ask, exactly as a single spoken
    instruction does. The cap on steps per turn is the engine's, not this
    function's: an agent that runs until it decides it is done is one the user
    cannot interrupt.
    """
    low = prompt.lower()
    if not any(cue in low for cue in _GOAL_DECLARATIONS):
        return None
    try:
        from .database import SessionLocal
        from .main import GoalPursueRequest, _cognitive_os, _pursue_goal
        from .goal_pursuit import goal_from_utterance
    except Exception as exc:  # pragma: no cover - import-time only
        log.warning("voice_executor: goal route unavailable: %s", exc)
        return None

    read = goal_from_utterance(prompt)
    db = SessionLocal()
    try:
        brain = _cognitive_os()
        created = brain.goal_graph.create_goal(title=read["title"], goal_context=read["context"])
        goal_id = str(created.get("id") or "")
        if goal_id:
            brain.goal_graph.decompose_goal(goal_id)
        detail = brain.goal_graph.details(goal_id) if goal_id else {}
        goal = dict(detail.get("goal") or created) | {"tasks": detail.get("tasks", [])}
        outcome = _pursue_goal(db, goal, GoalPursueRequest(dry_run=False), stop=stop.is_set)
    except Exception as exc:
        log.warning("voice_executor: goal pursuit failed: %s", exc)
        return {
            "success": False,
            "summary": f"I noted the goal but could not start on it: {exc}",
            "steps": [],
            "engine": "goal",
        }
    finally:
        db.close()

    report = outcome.get("report") or {}
    spoken = " ".join(read["reasoning"] + [str(report.get("summary") or "")]).strip()
    return {
        "success": bool(report.get("completed")),
        "summary": spoken or "Goal set.",
        "steps": report.get("results") or [],
        "engine": "goal",
        "reasoning": report.get("reasoning") or read["reasoning"],
        "goal_id": goal_id,
        "needs_you": report.get("needs_you") or [],
        "cancelled": bool(report.get("cancelled")),
        "held": bool(report.get("held")),
    }


# ── §16/§40: automation ──────────────────────────────────────────────────────
def _run_automation(prompt: str, stop: threading.Event) -> Dict[str, Any]:
    """Drive the real browser or desktop. Blocking, hence always in a thread.

    Four paths, in order of how much they know about what the user has actually
    asked for: a declared goal first, then the connections registry, then the
    Playwright site dispatcher when the prompt names a site it has a real skill
    for, then the pyautogui plan. The difference from
    `/api/automation/browser/prompt` is the stop flag, checked between steps --
    and the registry, which that route still does not consult.
    """
    from .main import build_browser_prompt_plan
    from .automation import execute_desktop_command

    try:
        goal = _try_goal(prompt, stop)
    except Exception as exc:
        log.warning("voice_executor: goal route failed: %s", exc)
        goal = None
    if goal is not None:
        return goal

    try:
        connected = _try_connected_app(prompt, stop)
    except Exception as exc:
        log.warning("voice_executor: connected-app route failed: %s", exc)
        connected = None
    if connected is not None:
        return connected

    try:
        from .browser.skills.site_dispatcher import should_use_playwright, dispatch_playwright

        if should_use_playwright(prompt):
            result = dispatch_playwright(prompt)
            return {
                "success": bool(result.get("success")),
                "summary": result.get("message") or "Browser automation finished.",
                "steps": result.get("steps") or [],
                "engine": "playwright",
            }
    except ImportError:
        pass  # Playwright not installed — the pyautogui plan below still works.
    except Exception as exc:
        log.warning("voice_executor: playwright dispatch failed: %s", exc)

    plan = build_browser_prompt_plan(prompt)
    if plan.get("needs_clarification"):
        return {
            "success": False,
            "needs_clarification": True,
            "summary": plan.get("summary") or "I need more detail before I can do that.",
            "steps": [],
            "engine": "plan",
        }

    executed: List[Dict[str, Any]] = []
    for step in plan.get("steps") or []:
        if stop.is_set():
            return {
                "success": False,
                "cancelled": True,
                "summary": f"Stopped after {len(executed)} step(s).",
                "steps": executed,
                "engine": "desktop",
            }
        # `execute_desktop_command` is async but does blocking pyautogui work; we
        # are already off the loop, so a private loop per step is the honest way
        # to call it without reaching back into the one serving the socket.
        result = asyncio.run(
            execute_desktop_command(step.get("action"), step.get("target"), step.get("payload"))
        )
        executed.append({"step": step, "result": result})
        if not result.get("success"):
            return {
                "success": False,
                "summary": result.get("message") or "A step failed.",
                "steps": executed,
                "engine": "desktop",
            }

    last = executed[-1]["result"] if executed else {}
    return {
        "success": bool(executed),
        "summary": (plan.get("summary") or last.get("message") or "Done.").strip(),
        "steps": executed,
        "engine": "desktop",
    }


async def _execute_plan(
    session: VoiceSession, directive: Directive
) -> AsyncIterator[Event]:
    """Run one node of the task graph for real."""
    node_id = directive.payload.get("node_id")
    description = (directive.payload.get("description") or "").strip()
    if not description:
        yield Event(EventKind.STEP_FAILED, {"node_id": node_id, "error": "no step description"})
        return

    stop = _stop_flag(directive)
    yield Event(EventKind.STEP_STARTED, {"node_id": node_id, "description": description})

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_automation, description, stop),
            timeout=STEP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        stop.set()
        yield Event(EventKind.STEP_FAILED, {
            "node_id": node_id,
            "error": f"timed out after {int(STEP_TIMEOUT_S)}s",
        })
        return
    except asyncio.CancelledError:
        stop.set()
        raise
    except Exception as exc:
        log.exception("voice_executor: automation raised on %r", description)
        yield Event(EventKind.STEP_FAILED, {"node_id": node_id, "error": str(exc)})
        return

    if result.get("cancelled"):
        # Not a failure: the user asked for it, and the kernel has already spoken
        # its own "Stopped." acknowledgement at P0.
        return

    if result.get("needs_clarification"):
        yield Event(EventKind.STEP_FAILED, {"node_id": node_id, "error": result["summary"]})
        return

    if not result.get("success"):
        yield Event(EventKind.STEP_FAILED, {"node_id": node_id, "error": result["summary"]})
        return

    yield Event(EventKind.STEP_COMPLETED, {
        "node_id": node_id,
        "result": result.get("summary"),
        "summary": result.get("summary"),
    })
    if not session.graph.is_running:
        yield Event(EventKind.TASK_COMPLETED, {
            "node_id": node_id,
            "summary": result.get("summary"),
        })


# ── §12: context compression ─────────────────────────────────────────────────
def _summarise(messages: List[Dict[str, str]]) -> str:
    """Ask the model for a summary. Blocking, called in a thread."""
    from .ai_engine import _client_for_model, _openrouter_model_candidates, note_model_failure
    from . import model_routes

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    for candidate in _openrouter_model_candidates():
        try:
            reply = _client_for_model(candidate).chat.completions.create(
                model=model_routes.wire_name(candidate),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarise this assistant conversation in under 120 words. "
                            "Keep every decision, file path, name and constraint the user "
                            "stated; drop pleasantries. Write it as notes, not prose."
                        ),
                    },
                    {"role": "user", "content": transcript[:12000]},
                ],
                stream=False,
            )
            text = (reply.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as exc:
            note_model_failure(candidate, exc)
            log.warning("voice_executor: summary via %s failed: %s", candidate, exc)
    raise RuntimeError("could not summarise the conversation")


async def _compress_context(
    session: VoiceSession, directive: Directive
) -> AsyncIterator[Event]:
    """Fold the oldest turns into a summary so §12 does not become §12's failure.

    The kernel asks for this; if nobody answers, the context keeps filling and
    `build()` starts trimming verbatim turns off the front instead — which is
    exactly the silent forgetting the spec calls out.

    `take_compression_batch` / `apply_summary` / `abort_compression` is a
    transaction, and it is used as one: the batch leaves `turns` immediately, so
    anything that throws before `apply_summary` must put it back rather than let
    those turns vanish. That is why *everything* after the batch is taken sits
    inside the try — building the message list used to be outside it, and when
    that line raised (it read `Turn.content`, which does not exist; the field is
    `text`) the batch was stranded in `_pending_compression` with no summary to
    replace it. The visible effect was §12 inverted: instead of compressing the
    oldest turns, every attempt silently deleted a batch of them from the
    verbatim window and produced nothing.
    """
    context = session.context
    batch = context.take_compression_batch()
    if not batch:
        return
    try:
        messages = [{"role": t.role, "content": t.text} for t in batch if t.text]
        if not messages:
            context.abort_compression()
            return
        summary = await asyncio.to_thread(_summarise, messages)
    except asyncio.CancelledError:
        context.abort_compression()
        raise
    except Exception as exc:
        log.warning("voice_executor: compression failed, turns restored: %s", exc)
        context.abort_compression()
        return
    context.apply_summary(summary)
    log.info(
        "voice_executor: compressed %d turns, context now %s",
        len(batch),
        context.indicator(),
    )
    return
    yield  # pragma: no cover - makes this an async generator


# ── entry point ──────────────────────────────────────────────────────────────
async def execute(session: VoiceSession, directive: Directive) -> AsyncIterator[Event]:
    """The `voice_ws` executor. Dispatches on directive kind."""
    if directive.kind is DirectiveKind.GENERATE_RESPONSE:
        async for event in _generate_response(session, directive):
            yield event
    elif directive.kind is DirectiveKind.EXECUTE_PLAN:
        async for event in _execute_plan(session, directive):
            yield event
    elif directive.kind is DirectiveKind.COMPRESS_CONTEXT:
        async for event in _compress_context(session, directive):
            yield event


def install() -> None:
    """Wire the executor into the transport. Idempotent."""
    from . import voice_ws

    voice_ws.set_executor(execute)
    log.info("voice_executor: installed")
