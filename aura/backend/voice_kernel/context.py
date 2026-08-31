"""
voice_kernel.context — memory layers, rolling compression, token budget (§11, §12, §29).
========================================================================================

The failure this replaces: every `/api/voice/chat` call built a fresh prompt
from one transcript string, so turn 40 knew nothing about turn 3, and there was
no notion of a context window filling up.

Five layers, per §11, each with a different lifetime:

  short_term_context     The last N verbatim turns. Highest fidelity, smallest.
  working_memory         Facts extracted from this session — entities, decisions.
  task_memory            Open tasks and their state (the TaskGraph owns detail;
                         this holds the one-line view the prompt needs).
  conversation_summary   Rolling compression of everything evicted from
                         short_term_context. §12's Summary A / Summary B chain.
  long_term_memory       Cross-session facts. Injected, never written here —
                         persistence belongs to the storage layer, not the kernel.

`ContextBudget.build()` assembles a prompt payload that fits a real model
window and reports the fill percentage the UI shows as `Context ████████░░ 78%`.
Compression is *requested*, not performed: summarising needs an LLM, which is
I/O, which this package does not do. `needs_compression` plus
`take_compression_batch()` hand the work to the caller, and
`apply_summary()` folds the result back in.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .persona import CONTEXT_PREAMBLE

#: Rough characters-per-token for mixed English/Telugu/Hindi text. Telugu and
#: Devanagari tokenise worse than English, so this is deliberately pessimistic;
#: over-estimating the prompt is safe, under-estimating truncates mid-task.
CHARS_PER_TOKEN = 3.4

#: Context windows for the models this app actually routes to. Used for §29's
#: indicator. Unknown models fall back to `DEFAULT_WINDOW`.
MODEL_WINDOWS: Dict[str, int] = {
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "o3": 200_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "deepseek-chat": 64_000,
    "llama-3.3-70b": 128_000,
    "qwen-2.5-72b": 32_000,
    "mistral-large": 128_000,
}
DEFAULT_WINDOW = 32_000


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN) + 1)


def window_for(model: str) -> int:
    """Context window for a model id, matching on the longest known prefix."""
    if not model:
        return DEFAULT_WINDOW
    lowered = model.lower()
    # OpenRouter ids look like "anthropic/claude-sonnet-4"; strip the vendor.
    if "/" in lowered:
        lowered = lowered.split("/", 1)[1]
    best = 0
    found = DEFAULT_WINDOW
    for key, size in MODEL_WINDOWS.items():
        if key in lowered and len(key) > best:
            best = len(key)
            found = size
    return found


@dataclass(slots=True)
class Turn:
    """One verbatim conversational turn."""

    role: str                       # "user" | "assistant" | "system" | "tool"
    text: str
    at: float = field(default_factory=time.time)
    #: Populated for user turns so a low-confidence turn can be flagged later.
    stt_confidence: Optional[float] = None
    intent: Optional[str] = None
    #: Directives / tool names this turn produced, for the audit trail.
    actions: List[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text) + 4   # role overhead

    def as_message(self) -> Dict[str, str]:
        role = self.role if self.role in ("user", "assistant", "system") else "user"
        return {"role": role, "content": self.text}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "at": self.at,
            "stt_confidence": self.stt_confidence,
            "intent": self.intent,
            "actions": list(self.actions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Turn":
        return cls(
            role=data.get("role", "user"),
            text=data.get("text", ""),
            at=float(data.get("at") or time.time()),
            stt_confidence=data.get("stt_confidence"),
            intent=data.get("intent"),
            actions=list(data.get("actions") or []),
        )


@dataclass
class WorkingMemory:
    """§12 — the things that must survive compression verbatim."""

    #: "we decided to deploy to staging, not production"
    decisions: List[str] = field(default_factory=list)
    #: Named things the user has referred to: files, repos, sites, people.
    entities: Dict[str, str] = field(default_factory=dict)
    #: Stated preferences: "always ask before deleting", "speak in Telugu".
    preferences: List[str] = field(default_factory=list)
    #: One-line status of anything still open.
    open_tasks: List[str] = field(default_factory=list)
    #: Last known project/workspace/tool state, kept short.
    project_state: Dict[str, Any] = field(default_factory=dict)
    tool_state: Dict[str, Any] = field(default_factory=dict)

    #: Caps — working memory that grows without bound defeats the purpose.
    MAX_DECISIONS = 30
    MAX_PREFERENCES = 20
    MAX_ENTITIES = 60

    def note_decision(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.decisions:
            self.decisions.append(text)
            del self.decisions[: max(0, len(self.decisions) - self.MAX_DECISIONS)]

    def note_preference(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.preferences:
            self.preferences.append(text)
            del self.preferences[: max(0, len(self.preferences) - self.MAX_PREFERENCES)]

    def note_entity(self, name: str, value: str) -> None:
        if not name:
            return
        self.entities[name] = value
        while len(self.entities) > self.MAX_ENTITIES:
            self.entities.pop(next(iter(self.entities)))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decisions": list(self.decisions),
            "entities": dict(self.entities),
            "preferences": list(self.preferences),
            "open_tasks": list(self.open_tasks),
            "project_state": dict(self.project_state),
            "tool_state": dict(self.tool_state),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkingMemory":
        wm = cls()
        wm.decisions = list(data.get("decisions") or [])
        wm.entities = dict(data.get("entities") or {})
        wm.preferences = list(data.get("preferences") or [])
        wm.open_tasks = list(data.get("open_tasks") or [])
        wm.project_state = dict(data.get("project_state") or {})
        wm.tool_state = dict(data.get("tool_state") or {})
        return wm


#: Statements that are worth remembering as standing preferences (§12).
_PREFERENCE_HINT = re.compile(
    r"\b(always|never|from now on|prefer|don'?t ever|remember to|by default|"
    r"eppudu|eppatiki|hamesha|kabhi)\b",
    re.IGNORECASE,
)
#: Statements that record a choice, so compression must not lose them.
_DECISION_HINT = re.compile(
    r"\b(instead|actually|let'?s use|we'?ll use|go with|switch to|not production|"
    r"use staging|decided)\b",
    re.IGNORECASE,
)


@dataclass
class SummaryBlock:
    """§12 — one link in the Summary A → Summary B → … chain."""

    index: int
    text: str
    covers_turns: int
    from_at: float
    to_at: float

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "covers_turns": self.covers_turns,
            "from_at": self.from_at,
            "to_at": self.to_at,
        }


class ContextBudget:
    """Assembles a prompt that fits the model window, and reports the fill.

    Nothing here calls a model. `needs_compression` and
    `take_compression_batch()` externalise the one step that requires one.
    """

    #: Verbatim turns kept before compression is considered.
    keep_verbatim_turns: int = 24
    #: Turns handed to the summariser in one batch (§12's "1–100 → Summary A").
    compression_batch: int = 20
    #: Fraction of the window we are willing to fill before compressing.
    high_water: float = 0.72
    #: Fraction reserved for the model's own reply.
    reply_reserve: float = 0.18

    def __init__(
        self,
        *,
        model: str = "",
        window: Optional[int] = None,
        system_prompt: str = "",
    ) -> None:
        self.model = model
        self.window = int(window) if window else window_for(model)
        self.system_prompt = system_prompt
        self.turns: List[Turn] = []
        self.summaries: List[SummaryBlock] = []
        self.working = WorkingMemory()
        #: Injected by the storage layer; the kernel only reads it.
        self.long_term: List[str] = []
        self.total_turns_seen = 0
        self._pending_compression: List[Turn] = []

    # ── ingestion ─────────────────────────────────────────────────────────
    def add_turn(self, turn: Turn) -> None:
        self.turns.append(turn)
        self.total_turns_seen += 1
        if turn.role == "user":
            self._harvest(turn.text)

    def add_user(self, text: str, **kw: Any) -> Turn:
        turn = Turn("user", text, **kw)
        self.add_turn(turn)
        return turn

    def add_assistant(self, text: str, **kw: Any) -> Turn:
        turn = Turn("assistant", text, **kw)
        self.add_turn(turn)
        return turn

    def _harvest(self, text: str) -> None:
        """Cheap extraction so a preference stated once outlives compression."""
        stripped = text.strip()
        if not stripped or len(stripped) > 400:
            return
        if _PREFERENCE_HINT.search(stripped):
            self.working.note_preference(stripped)
        if _DECISION_HINT.search(stripped):
            self.working.note_decision(stripped)

    # ── §29: accounting ───────────────────────────────────────────────────
    @property
    def reserved(self) -> int:
        return int(self.window * self.reply_reserve)

    @property
    def usable(self) -> int:
        return max(1, self.window - self.reserved)

    def used_tokens(self) -> int:
        total = estimate_tokens(self.system_prompt)
        total += sum(s.tokens for s in self.summaries)
        total += sum(t.tokens for t in self.turns)
        total += estimate_tokens(self._memory_block())
        return total

    @property
    def fill(self) -> float:
        """0.0–1.0 against the usable window. This is what the UI bar shows."""
        return min(1.0, round(self.used_tokens() / self.usable, 4))

    def indicator(self, width: int = 10) -> str:
        """`████████░░ 78%` — §29's exact shape."""
        pct = self.fill
        filled = int(round(pct * width))
        return f"{'█' * filled}{'░' * (width - filled)} {int(round(pct * 100))}%"

    @property
    def needs_compression(self) -> bool:
        return (
            self.fill >= self.high_water
            and len(self.turns) > self.keep_verbatim_turns
        )

    # ── §12: rolling compression ──────────────────────────────────────────
    def take_compression_batch(self) -> List[Turn]:
        """Remove the oldest turns and hand them to the caller to summarise.

        The turns are held in `_pending_compression` until `apply_summary()`
        lands, so a failed summarisation can be rolled back with
        `abort_compression()` rather than silently losing history.
        """
        if self._pending_compression:
            return list(self._pending_compression)
        surplus = len(self.turns) - self.keep_verbatim_turns
        if surplus <= 0:
            return []
        take = min(self.compression_batch, surplus)
        self._pending_compression = self.turns[:take]
        self.turns = self.turns[take:]
        return list(self._pending_compression)

    def apply_summary(self, summary_text: str) -> SummaryBlock:
        """Fold a summariser's output in, replacing the batch it covers."""
        batch = self._pending_compression or []
        block = SummaryBlock(
            index=len(self.summaries) + 1,
            text=summary_text.strip(),
            covers_turns=len(batch),
            from_at=batch[0].at if batch else time.time(),
            to_at=batch[-1].at if batch else time.time(),
        )
        self.summaries.append(block)
        self._pending_compression = []
        self._collapse_summaries()
        return block

    def abort_compression(self) -> None:
        """Put the batch back — summarisation failed."""
        if self._pending_compression:
            self.turns = self._pending_compression + self.turns
            self._pending_compression = []

    def _collapse_summaries(self) -> None:
        """Keep the summary chain from becoming the new context hog.

        Older summaries are truncated rather than dropped: losing the fact that
        something happened is worse than losing its detail.
        """
        budget = int(self.usable * 0.22)
        while sum(s.tokens for s in self.summaries) > budget and len(self.summaries) > 1:
            oldest = self.summaries[0]
            if oldest.tokens <= 60:
                merged = self.summaries.pop(0)
                self.summaries[0].text = f"{merged.text}\n{self.summaries[0].text}"
                self.summaries[0].covers_turns += merged.covers_turns
                self.summaries[0].from_at = merged.from_at
            else:
                keep = int(len(oldest.text) * 0.6)
                oldest.text = oldest.text[:keep].rstrip() + " …"

    # ── §11: prompt assembly ──────────────────────────────────────────────
    def _memory_block(self) -> str:
        parts: List[str] = []
        w = self.working
        if w.open_tasks:
            parts.append("OPEN TASKS:\n" + "\n".join(f"- {t}" for t in w.open_tasks[-8:]))
        if w.decisions:
            parts.append("DECISIONS:\n" + "\n".join(f"- {d}" for d in w.decisions[-8:]))
        if w.preferences:
            parts.append("USER PREFERENCES:\n" + "\n".join(f"- {p}" for p in w.preferences[-8:]))
        if w.entities:
            named = ", ".join(f"{k}={v}" for k, v in list(w.entities.items())[-12:])
            parts.append(f"ENTITIES: {named}")
        if w.project_state:
            parts.append("PROJECT: " + ", ".join(f"{k}={v}" for k, v in w.project_state.items()))
        if w.tool_state:
            parts.append("TOOL STATE: " + ", ".join(f"{k}={v}" for k, v in w.tool_state.items()))
        if self.long_term:
            parts.append("LONG-TERM MEMORY:\n" + "\n".join(f"- {m}" for m in self.long_term[:8]))
        return "\n\n".join(parts)

    def build(
        self,
        *,
        situation_line: str = "",
        task_line: str = "",
        extra_system: str = "",
    ) -> Dict[str, Any]:
        """Assemble the message list, trimming oldest-first if still over budget.

        Order matters: system prompt, then durable memory, then the summary
        chain, then verbatim turns. Trimming removes verbatim turns from the
        front, never the memory block — losing "deploy to staging, not
        production" to save tokens is exactly the §12 failure mode.

        Everything after the system prompt is machine-readable state, and it is
        introduced as such. Unlabelled, a `key=value` situation line is the only
        formatting example a model has been shown, and it will answer in that
        format — see `persona.py` for the measured case.
        """
        system_parts = [p for p in (self.system_prompt, extra_system) if p]
        state_parts: List[str] = []
        memory = self._memory_block()
        if memory:
            state_parts.append(memory)
        if situation_line:
            state_parts.append(f"CURRENT SITUATION: {situation_line}")
        if task_line:
            state_parts.append(f"CURRENT TASK: {task_line}")
        if self.summaries:
            chain = "\n\n".join(
                f"[summary {s.index}, {s.covers_turns} turns] {s.text}" for s in self.summaries
            )
            state_parts.append("EARLIER CONVERSATION:\n" + chain)

        if state_parts:
            if self.system_prompt:
                system_parts.append(CONTEXT_PREAMBLE)
            system_parts.extend(state_parts)

        system = "\n\n".join(system_parts)
        head = estimate_tokens(system)
        budget = self.usable - head

        kept: List[Turn] = []
        running = 0
        for turn in reversed(self.turns):
            cost = turn.tokens
            if running + cost > budget and kept:
                break
            kept.append(turn)
            running += cost
        kept.reverse()

        messages = [{"role": "system", "content": system}] if system else []
        messages.extend(t.as_message() for t in kept)
        return {
            "messages": messages,
            "model": self.model,
            "window": self.window,
            "prompt_tokens": head + running,
            "fill": min(1.0, round((head + running) / self.usable, 4)),
            "indicator": self.indicator(),
            "turns_included": len(kept),
            "turns_total": self.total_turns_seen,
            "turns_dropped": len(self.turns) - len(kept),
            "summaries": len(self.summaries),
            "needs_compression": self.needs_compression,
        }

    # ── §37/§38: serialisation ────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "window": self.window,
            "system_prompt": self.system_prompt,
            "turns": [t.as_dict() for t in self.turns],
            "summaries": [s.as_dict() for s in self.summaries],
            "working": self.working.as_dict(),
            "long_term": list(self.long_term),
            "total_turns_seen": self.total_turns_seen,
            "fill": self.fill,
        }

    @classmethod
    def restore(cls, data: Dict[str, Any]) -> "ContextBudget":
        budget = cls(
            model=data.get("model", ""),
            window=data.get("window"),
            system_prompt=data.get("system_prompt", ""),
        )
        budget.turns = [Turn.from_dict(t) for t in (data.get("turns") or [])]
        budget.summaries = [
            SummaryBlock(
                index=int(s.get("index") or i + 1),
                text=s.get("text", ""),
                covers_turns=int(s.get("covers_turns") or 0),
                from_at=float(s.get("from_at") or 0.0),
                to_at=float(s.get("to_at") or 0.0),
            )
            for i, s in enumerate(data.get("summaries") or [])
        ]
        budget.working = WorkingMemory.from_dict(data.get("working") or {})
        budget.long_term = list(data.get("long_term") or [])
        budget.total_turns_seen = int(data.get("total_turns_seen") or len(budget.turns))
        return budget

    def set_model(self, model: str, window: Optional[int] = None) -> None:
        """§29 — the indicator must follow the *actually selected* model."""
        self.model = model
        self.window = int(window) if window else window_for(model)

    def recent(self, count: int = 6) -> List[Turn]:
        return self.turns[-count:]

    def last_user_text(self) -> Optional[str]:
        for turn in reversed(self.turns):
            if turn.role == "user":
                return turn.text
        return None
