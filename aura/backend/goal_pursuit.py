"""Turning a goal into finished work.

The cognitive OS in `backend/hermes` can already hold a goal, decompose it into
milestones and tasks, remember what happened and draw lessons from failures.
`backend/app_operator.py` can already drive a real browser window or a real
desktop application on behalf of a connection the user signed into. Until this
module there was no path between them: `grep -rn app_operator backend/hermes`
returns nothing, and `"app_control"` appears inside hermes only as a *string in
a capability list*. The goal engine described work; the operator did work; a
goal therefore never moved on its own.

This module is that path. It reads a goal, derives the operations the goal
actually asks for, runs them through the operator, and writes what happened
back into the learning layer so the next attempt starts better informed.

Three rules hold the design together, and the tests pin each one:

1. **A goal is not reached by describing it.** Every derived action either
   resolves to a real operation the operator can run, or is marked as needing a
   person. A step that only reads well is never reported as done. The template
   milestones the goal graph writes ("Clarify objective", "Learn and optimise")
   are prose, and prose is classified `manual` — it is surfaced, not simulated.

2. **A lesson is read before the step, not only written after it.** Prior
   failure lessons for the same action are looked up during planning and
   attached to the action's reasoning, so a selector that broke last week is
   named before it is tried again rather than after.

3. **Nothing in this module performs a side effect.** `plan_pursuit` is pure and
   takes a `resolve` callable; `pursue` takes its runner and its learner as
   required keyword arguments with no defaults. The module cannot open a
   browser, move a mouse or write to a database on its own — the API layer binds
   the real ones in exactly one place, the same discipline `app_operator` uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .app_operator import OP_POST, OP_SEND, ROUTE_NONE, ControlDecision

# ── what an action turns out to be ───────────────────────────────────────────
#: A connected app can carry this out: the operator returned a runnable plan.
KIND_OPERATE = "operate"
#: Nothing is connected for it, but it is a question, so it can be read up on.
KIND_RESEARCH = "research"
#: It needs a person — a decision, a credential, a judgement call, or it is
#: simply a sentence of intent with no operation in it. Never faked as done.
KIND_MANUAL = "manual"

#: Milestone and task titles the goal graph writes from a static template. They
#: describe the shape of any project rather than anything to actually do, so
#: they must not be mistaken for operations.
#:
#: Two templates feed this: the generic one ("Clarify objective") and the product
#: one ("Architecture and data model"). Both were measured against a live goal --
#: the first plan this engine produced classified all six product milestones as
#: research and would have opened six search tabs for noun phrases.
_TEMPLATE_PROSE = (
    "clarify objective",
    "clarify the objective",
    "create execution plan",
    "execute core work",
    "validate output",
    "learn and optimize",
    "learn and optimise",
    "define success criteria",
    "review progress",
    # the product template
    "requirements and user flows",
    "architecture and data model",
    "frontend implementation",
    "backend implementation",
    "testing and quality gates",
    "deployment and monitoring",
    "mvp build plan",
    "research and validation",
    "launch and iterate",
)

#: Verbs that mean a human has to decide, sign, pay or judge. Recognised so the
#: engine stops and says so instead of inventing a click.
_NEEDS_A_PERSON = (
    "decide",
    "choose between",
    "approve",
    "sign the",
    "pay",
    "confirm with",
    "ask ",
    "negotiate",
    "interview",
    "review and approve",
)

#: Where one instruction ends and the next begins. Ordered longest-first so
#: " and then " wins over " and ".
_SPLITTERS = re.compile(
    r"(?:\r?\n|•|·|•|;|\band then\b|\bafter that\b|\bthen\b|\bnext,\b|\balso\b)",
    re.IGNORECASE,
)
#: A bare "and" is *not* a splitter -- "message Amma and Nanna" is one
#: instruction with two recipients. But "post this and search github" is two, and
#: the tell is that the words after "and" open with an operation. So the split
#: happens only when the right-hand side starts with a verb the operator knows.
_AND_THEN_A_VERB = re.compile(
    r"\s+and\s+(?=(?:post|tweet|send|message|search|look up|find|open|launch|read|"
    r"close|play|type|write|share|reply)\b)",
    re.IGNORECASE,
)

#: Research answers a *question*. It does not turn a noun phrase into a web
#: search: "Architecture and data model" is a heading, and opening a results page
#: for it is noise dressed up as work. So a line has to look like something being
#: asked or looked up before the open web is allowed to answer it.
_A_QUESTION = re.compile(
    r"(?:\?$|^(?:what|why|how|when|who|which|where|is|are|does|do|can|should)\b|"
    r"\b(?:look up|find out|research|read about|check whether|check if|compare|"
    r"how much|how many|latest|news about|documentation for)\b)",
    re.IGNORECASE,
)
#: "1. do this" / "2) do that" / "- do the other"
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]|[-*—])\s*")

#: Phrases that state a horizon. Kept plain: a wrong deadline is worse than
#: none, so only unambiguous words count.
_HORIZONS: tuple[tuple[str, str], ...] = (
    ("today", "day"),
    ("by tonight", "day"),
    ("tomorrow", "day"),
    ("this week", "week"),
    ("by friday", "week"),
    ("by monday", "week"),
    ("next week", "week"),
    ("this month", "month"),
    ("this quarter", "quarter"),
    ("this year", "year"),
    ("long term", "year"),
    ("eventually", "year"),
)

#: Openers people use to state a goal. Stripped so the goal's title is the
#: thing itself, not the sentence that introduced it.
_GOAL_OPENERS = re.compile(
    r"^(?:my goal is to|the goal is to|i want to|i need to|i'd like to|"
    r"help me|please|set a goal to|set a goal|goal:|new goal:?|"
    r"remind me to|make sure (?:that )?(?:i|we)|let's|lets)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalAction:
    """One derived unit of work, and what the engine believes about it."""

    text: str
    kind: str
    #: The `goal_tasks` row this came from, when it came from one.
    task_id: str = ""
    #: The operator's plan. `None` for `manual`, and for `research` until the
    #: caller resolves it.
    decision: ControlDecision | None = None
    #: Sentences, in the order the conclusions were reached.
    reasoning: tuple[str, ...] = ()
    #: Why it cannot run now. Empty does not mean runnable — check `decision`.
    blocked: str = ""
    #: The one thing that would clear the doubt, phrased as a question to ask out
    #: loud. Empty when there is no doubt. A statement of the problem is not a
    #: question: "I could not match that" leaves the person guessing what to say
    #: back, so an unresolved action carries the question instead.
    question: str = ""

    @property
    def runnable(self) -> bool:
        return (
            self.kind in (KIND_OPERATE, KIND_RESEARCH)
            and not self.blocked
            and self.decision is not None
            and self.decision.runnable
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "task_id": self.task_id,
            "runnable": self.runnable,
            "blocked": self.blocked,
            "question": self.question,
            "reasoning": list(self.reasoning),
            "decision": self.decision.as_dict() if self.decision else None,
        }


@dataclass
class PursuitPlan:
    """Everything the engine intends to do about one goal, before it does any."""

    goal_id: str
    title: str
    actions: list[GoalAction] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    #: The first action that would do something irreversible, if any. The runner
    #: stops *before* it unless the caller has said to go through.
    hold_at: int | None = None

    @property
    def runnable(self) -> list[GoalAction]:
        return [a for a in self.actions if a.runnable]

    @property
    def needs_a_person(self) -> list[GoalAction]:
        return [a for a in self.actions if a.kind == KIND_MANUAL]

    @property
    def blocked(self) -> list[GoalAction]:
        return [a for a in self.actions if a.kind != KIND_MANUAL and not a.runnable]

    @property
    def questions(self) -> list[str]:
        """The doubts, in plan order, without repeats.

        Capped at three deliberately: reading out every question at once is an
        interrogation, and the answer to the first usually changes the rest.
        """
        asked: list[str] = []
        for action in self.actions:
            if action.question and action.question not in asked:
                asked.append(action.question)
        return asked[:3]

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "actions": [a.as_dict() for a in self.actions],
            "reasoning": list(self.reasoning),
            "hold_at": self.hold_at,
            "questions": self.questions,
            "runnable_count": len(self.runnable),
            "needs_a_person_count": len(self.needs_a_person),
            "blocked_count": len(self.blocked),
            "summary": self.summary,
        }

    @property
    def summary(self) -> str:
        if not self.actions:
            return f"I could not find anything to do for “{self.title}” yet."
        parts = [f"{len(self.runnable)} of {len(self.actions)} steps I can run now"]
        if self.needs_a_person:
            parts.append(f"{len(self.needs_a_person)} that need you")
        if self.blocked:
            parts.append(f"{len(self.blocked)} blocked")
        return f"“{self.title}”: " + ", ".join(parts) + "."


# ── reading a goal out of a sentence ─────────────────────────────────────────
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _LIST_MARKER.sub("", text or "")).strip(" .,-—")


def split_intentions(text: str) -> list[str]:
    """One sentence in, the separate things it asks for out, order preserved.

    Splitting is deliberately conservative. A missed split leaves one larger
    action, which the operator will either handle or decline for a stated
    reason; a wrong split invents an instruction the user never gave.
    """
    if not text:
        return []
    out: list[str] = []
    for chunk in _SPLITTERS.split(text):
        for piece in _AND_THEN_A_VERB.split(chunk):
            cleaned = _clean(piece)
            # Two characters cannot be an instruction, and a bare conjunction left
            # behind by a split is noise.
            if len(cleaned) < 3 or cleaned.lower() in {"and", "then", "also", "next"}:
                continue
            if cleaned not in out:
                out.append(cleaned)
    return out


def goal_from_utterance(utterance: str) -> dict[str, Any]:
    """“Set a goal to X by Friday, and Y” → a goal the graph can store.

    Pure: it returns the fields, it does not create anything. Setting the goal
    is the caller's decision, so the caller performs it.
    """
    said = _clean(utterance)
    horizon = ""
    lowered = said.lower()
    for phrase, span in _HORIZONS:
        if phrase in lowered:
            horizon = span
            break
    intentions = split_intentions(said)
    headline = _clean(_GOAL_OPENERS.sub("", intentions[0] if intentions else said))
    # The title carries the first intention; the rest are the work, and are kept
    # verbatim in the context so nothing the user said is dropped.
    return {
        "title": headline or said,
        "context": said,
        "horizon": horizon or "week",
        "intentions": intentions or ([said] if said else []),
        "reasoning": (
            [f"I read the goal as “{headline}”."]
            + ([f"You said “{horizon}”, so I set the horizon to this {horizon}." ] if horizon else [])
            + (
                [f"There are {len(intentions)} separate things in that sentence, so I kept them as {len(intentions)} steps."]
                if len(intentions) > 1
                else []
            )
        ),
    }


# ── deciding what each step really is ────────────────────────────────────────
def _is_template_prose(text: str) -> bool:
    low = text.lower().strip()
    low = re.sub(r"^milestone\s*\d+\s*:\s*", "", low)
    return any(low == p or low.startswith(p) for p in _TEMPLATE_PROSE)


def _needs_a_person(text: str) -> str:
    low = f" {text.lower()} "
    for cue in _NEEDS_A_PERSON:
        if cue in low:
            return cue.strip()
    return ""


def _doubt(text: str, decision: ControlDecision | None) -> str:
    """The one question that would clear this up, or "" if nothing is unclear.

    An agent that says "I could not match that" has told the truth and left the
    person guessing what to say back. Asking is the useful half, so every
    unresolved action carries a question phrased the way it would be spoken, and
    exactly one question -- a list of three is an interrogation, not a check.
    """
    if decision is None or decision.grant is None:
        return f"I don't know which app to use for “{text}” — which one should I open?"
    grant = decision.grant
    if decision.runnable:
        return ""
    needs = [n for n in decision.needs if n.strip()]
    if not grant.live:
        return f"{grant.label} isn't connected yet. Shall I open it so you can sign in?"
    operation = decision.intent.operation
    if operation == OP_SEND and not decision.intent.recipient:
        return f"Who should I send that to on {grant.label}?"
    if operation in (OP_SEND, OP_POST) and not decision.intent.payload:
        return f"What should I say on {grant.label}?"
    if grant.route == ROUTE_NONE or not grant.verbs:
        return f"I can reach {grant.label}, but not in a way that can do “{text}”. Do you want me to try something else?"
    if needs:
        return f"To do “{text}” I need one thing first: {needs[0]}. Shall I?"
    return f"I couldn't work out how to do “{text}” on {grant.label}. How would you do it?"


def _lessons_for(text: str, lessons: Sequence[dict[str, Any]]) -> list[str]:
    """Prior failures worth naming before this step runs.

    Matching is on the words the two tasks share rather than an exact string,
    because the same work is rarely phrased identically twice. The threshold is
    deliberately high: a lesson attached to the wrong step is misleading advice
    delivered with confidence.
    """
    words = {w for w in re.findall(r"[a-z]{4,}", text.lower())}
    if not words:
        return []
    out: list[str] = []
    for lesson in lessons:
        task = str(lesson.get("task") or "")
        other = {w for w in re.findall(r"[a-z]{4,}", task.lower())}
        if not other:
            continue
        overlap = len(words & other) / len(words | other)
        if overlap < 0.4:
            continue
        fix = str(lesson.get("fix") or "").strip()
        failure = str(lesson.get("failure") or "").strip()
        if fix:
            out.append(f"Last time this failed — {failure or 'it did not work'}. What fixed it: {fix}")
    return out[:3]


def classify(
    text: str,
    *,
    resolve: Callable[[str], ControlDecision],
    research: Callable[[str], ControlDecision] | None = None,
    lessons: Sequence[dict[str, Any]] = (),
    task_id: str = "",
) -> GoalAction:
    """Work out what one line of a goal is, and how it would be carried out."""
    reasoning: list[str] = []
    prior = _lessons_for(text, lessons)
    reasoning.extend(prior)

    if _is_template_prose(text):
        return GoalAction(
            text=text,
            kind=KIND_MANUAL,
            task_id=task_id,
            reasoning=tuple(
                reasoning
                + ["That is a planning placeholder, not an operation, so I left it for you rather than pretending to do it."]
            ),
            question=f"“{text}” is a heading rather than a step. What would actually finish it?",
        )

    cue = _needs_a_person(text)
    if cue:
        return GoalAction(
            text=text,
            kind=KIND_MANUAL,
            task_id=task_id,
            reasoning=tuple(reasoning + [f"“{cue}” is your call, so I stopped and brought it to you."]),
            question=f"“{text}” is yours to decide. What do you want to do?",
        )

    decision = resolve(text)
    if decision.grant is not None:
        reasoning.extend(decision.reasoning)
        return GoalAction(
            text=text,
            kind=KIND_OPERATE,
            task_id=task_id,
            decision=decision,
            reasoning=tuple(reasoning),
            blocked=decision.blocked,
            question=_doubt(text, decision),
        )

    # Nothing connected answers to it. If it is a question, read it up; otherwise
    # say plainly that it needs a person. Never invent a click, and never turn a
    # heading into a web search -- a results page for "Backend implementation" is
    # noise wearing the costume of work.
    if research is not None and _A_QUESTION.search(text):
        read = research(text)
        if read.grant is not None and read.runnable:
            reasoning.append("No connected app covers that, so I looked it up instead of guessing at one.")
            reasoning.extend(read.reasoning)
            return GoalAction(
                text=text,
                kind=KIND_RESEARCH,
                task_id=task_id,
                decision=read,
                reasoning=tuple(reasoning),
            )
    return GoalAction(
        text=text,
        kind=KIND_MANUAL,
        task_id=task_id,
        reasoning=tuple(reasoning + ["I could not match that to an app you have connected, so it needs you."]),
        question=_doubt(text, None),
    )


# ── planning a whole goal ────────────────────────────────────────────────────
def _action_lines(goal: dict[str, Any]) -> list[tuple[str, str]]:
    """(text, task_id) for everything this goal asks for, in order.

    The goal's own stored tasks come first because they carry an id and can have
    their status written back. Anything the user said that the template did not
    capture is added afterwards, so a concrete instruction is never lost behind
    five generic milestones.
    """
    lines: list[tuple[str, str]] = []
    seen: set[str] = set()
    # The goal's own title is not one of its steps. "Ship the voice agent" is the
    # destination; queuing it as work produced a step that could only ever be
    # marked done by fiat.
    title = _clean(str(goal.get("title") or "")).lower()
    if title:
        seen.add(title)

    def add(text: str, task_id: str = "") -> None:
        cleaned = _clean(_GOAL_OPENERS.sub("", text))
        key = cleaned.lower()
        if len(cleaned) < 3 or key in seen:
            return
        seen.add(key)
        lines.append((cleaned, task_id))

    for task in goal.get("tasks") or ():
        if str(task.get("status") or "") in {"done", "completed", "cancelled"}:
            continue
        add(str(task.get("title") or ""), str(task.get("id") or ""))

    for source in (goal.get("goal_context"), goal.get("title")):
        for intention in split_intentions(str(source or "")):
            add(intention)
    return lines


def plan_pursuit(
    goal: dict[str, Any],
    *,
    resolve: Callable[[str], ControlDecision],
    research: Callable[[str], ControlDecision] | None = None,
    lessons: Sequence[dict[str, Any]] = (),
    hold_point: Callable[[ControlDecision], int | None] | None = None,
) -> PursuitPlan:
    """Read a goal, decide what would move it, and say why. Runs nothing."""
    title = _clean(str(goal.get("title") or "")) or "this goal"
    plan = PursuitPlan(goal_id=str(goal.get("id") or ""), title=title)
    lines = _action_lines(goal)
    if not lines:
        plan.reasoning.append("The goal has no tasks and no detail yet, so there is nothing I can act on.")
        return plan

    for text, task_id in lines:
        plan.actions.append(
            classify(text, resolve=resolve, research=research, lessons=lessons, task_id=task_id)
        )

    runnable = plan.runnable
    plan.reasoning.append(
        f"I found {len(lines)} things in this goal and can run {len(runnable)} of them now."
    )
    if plan.needs_a_person:
        plan.reasoning.append(
            f"{len(plan.needs_a_person)} need you — I have listed them rather than guessing."
        )
    for action in plan.blocked:
        if action.blocked:
            plan.reasoning.append(action.blocked)

    # The first irreversible step in the whole plan, so a goal that ends in
    # "post it" drafts the post and stops, exactly as a single spoken post does.
    if hold_point is not None:
        for index, action in enumerate(plan.actions):
            if action.decision is None or not action.runnable:
                continue
            if hold_point(action.decision) is not None:
                plan.hold_at = index
                plan.reasoning.append(
                    f"Step {index + 1} is the one that cannot be undone, so I will stop just before it and ask."
                )
                break
    return plan


# ── carrying it out ──────────────────────────────────────────────────────────
def pursue(
    plan: PursuitPlan,
    *,
    run: Callable[[ControlDecision], dict[str, Any]],
    learn: Callable[[dict[str, Any]], None],
    mark_task: Callable[[str, str], None] | None = None,
    stop: Callable[[], bool] = lambda: False,
    through_commit: bool = False,
    max_actions: int = 6,
) -> dict[str, Any]:
    """Run the runnable steps of a plan and record what happened.

    `run` and `learn` are required and injected — this module has no route to a
    browser, a mouse or a database of its own. `max_actions` is a real cap and
    not a formality: an agent that keeps going until it decides it is finished is
    an agent that cannot be interrupted, so it stops and reports instead.
    """
    results: list[dict[str, Any]] = []
    narrative: list[str] = list(plan.reasoning)
    completed = 0
    failed = 0
    held = False
    cancelled = False

    for index, action in enumerate(plan.actions):
        if stop():
            cancelled = True
            narrative.append("You stopped me, so I left the rest of the goal alone.")
            break
        if not action.runnable:
            continue
        if plan.hold_at is not None and index >= plan.hold_at and not through_commit:
            held = True
            narrative.append(
                f"I have stopped before “{action.text}” because that step cannot be undone. Say go ahead and I will finish it."
            )
            break
        if len(results) >= max_actions:
            narrative.append(
                f"That is {max_actions} steps in one go, which is my limit — tell me to carry on and I will take the next few."
            )
            break

        assert action.decision is not None  # runnable implies a decision
        report = run(action.decision)
        ok = bool(report.get("ok"))
        results.append(
            {
                "text": action.text,
                "kind": action.kind,
                "task_id": action.task_id,
                "ok": ok,
                "summary": str(report.get("summary") or ""),
                "text_read": str(report.get("text") or ""),
                "steps": report.get("steps") or [],
            }
        )
        if ok:
            completed += 1
            narrative.append(f"Done: {action.text}.")
        else:
            failed += 1
            narrative.append(f"Could not finish “{action.text}” — {report.get('summary') or 'no reason given'}.")

        if mark_task is not None and action.task_id:
            try:
                mark_task(action.task_id, "done" if ok else "blocked")
            except Exception as err:  # a status write must not lose the work
                narrative.append(f"The step ran, but I could not update its status: {err}")

        # One experience per step, so a partial success teaches as much as a
        # clean run. Recorded whether it worked or not — a failure with a reason
        # is the more useful of the two.
        try:
            learn(
                {
                    "task": action.text,
                    "goal": plan.title,
                    "actions_taken": [str(s.get("verb") or "") for s in (report.get("steps") or [])],
                    "tools_used": [str(report.get("route") or action.kind)],
                    "successful_steps": [action.text] if ok else [],
                    "errors": [] if ok else [str(report.get("summary") or "failed")],
                    "task_success": ok,
                    "score": 0.85 if ok else 0.2,
                    "feedback": str(report.get("summary") or ""),
                }
            )
        except Exception as err:
            narrative.append(f"I could not file what I learned from that step: {err}")

    remaining = [a.text for a in plan.runnable][completed + failed :]
    # The questions come last on purpose. Asking before trying wastes the
    # person's attention on doubts the work itself would have settled; asking
    # after means every question left is one that really is still open.
    if plan.questions:
        narrative.append("Before I go further: " + " ".join(plan.questions))
    return {
        "goal_id": plan.goal_id,
        "title": plan.title,
        "ran": bool(results),
        "completed": completed,
        "failed": failed,
        "held": held,
        "cancelled": cancelled,
        "results": results,
        "remaining": remaining,
        "questions": plan.questions,
        "needs_you": [
            {"text": a.text, "why": a.reasoning[-1] if a.reasoning else "", "question": a.question}
            for a in plan.needs_a_person
        ],
        "blocked": [{"text": a.text, "why": a.blocked, "question": a.question} for a in plan.blocked if a.blocked],
        "reasoning": narrative,
        "summary": _pursuit_summary(plan.title, completed, failed, held, cancelled, plan),
    }


def _pursuit_summary(
    title: str, completed: int, failed: int, held: bool, cancelled: bool, plan: PursuitPlan
) -> str:
    if cancelled:
        return f"Stopped part-way through “{title}”. {completed} steps were finished before that."
    if not completed and not failed:
        if plan.needs_a_person and not plan.runnable:
            return f"Nothing on “{title}” is mine to do yet — {len(plan.needs_a_person)} steps need you first."
        return f"I did not get anything done on “{title}”."
    parts = [f"{completed} step{'s' if completed != 1 else ''} done on “{title}”"]
    if failed:
        parts.append(f"{failed} failed")
    if held:
        parts.append("and I am holding before the step that cannot be undone")
    tail = ", ".join(parts)
    if plan.needs_a_person:
        tail += f". {len(plan.needs_a_person)} still need you"
    return tail + "."


__all__ = [
    "KIND_OPERATE",
    "KIND_RESEARCH",
    "KIND_MANUAL",
    "GoalAction",
    "PursuitPlan",
    "split_intentions",
    "goal_from_utterance",
    "classify",
    "plan_pursuit",
    "pursue",
]
