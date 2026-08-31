"""
voice_kernel — Akansha's continuous multimodal conversation core.
=================================================================

This package replaces the request/response voice path (transcript in →
answer out → stop) with a *live session kernel*: one long-lived object per
conversation that owns a state machine, an endpointing detector, a
situation model, a task graph, a context budget and a speech policy.

Design rules that the rest of the codebase must respect:

  1.  Nothing here performs I/O. No HTTP, no LLM calls, no audio. The
      kernel is a pure decision core so it can be unit-tested exhaustively
      and driven from a WebSocket, a test harness, or a CLI identically.
  2.  Everything is event-driven. Callers push `Event`s in and read
      `Directive`s out. There is no hidden control flow.
  3.  Every mutation is observable. `VoiceSession.handle` returns the full
      list of directives produced by an event, so the transport can stream
      them without re-deriving state.

Layering::

    transport (voice_ws.py)
        │  Event
        ▼
    VoiceSession ──── VoiceStateMachine   (§3  states + transitions)
        ├──────────── EndpointDetector    (§6  turn_complete_probability)
        ├──────────── IntentClassifier    (§10 14-way routing + §30 confidence)
        ├──────────── SituationModel      (§8  situation, §13 references)
        ├──────────── SpeechPolicy        (§9  should I respond, §18 priority)
        ├──────────── TaskGraph           (§15 nodes, §20 cancellation)
        └──────────── ContextBudget       (§11 memory, §12 compression)
        │  Directive
        ▼
    transport
"""

from __future__ import annotations

from .events import Directive, DirectiveKind, Event, EventKind
from .states import ILLEGAL, VoiceState, VoiceStateMachine
from .endpointing import EndpointDecision, EndpointDetector, SpeechSegment
from .intent import Intent, IntentCategory, IntentClassifier
from .situation import ConversationMode, References, SituationModel
from .speech_policy import Priority, ResponseAction, SpeechPolicy, Utterance
from .task_graph import CancellationToken, NodeStatus, TaskGraph, TaskNode
from .context import ContextBudget, Turn
from .session import SessionRegistry, VoiceSession, get_registry

__all__ = [
    "Directive",
    "DirectiveKind",
    "Event",
    "EventKind",
    "ILLEGAL",
    "VoiceState",
    "VoiceStateMachine",
    "EndpointDecision",
    "EndpointDetector",
    "SpeechSegment",
    "Intent",
    "IntentCategory",
    "IntentClassifier",
    "ConversationMode",
    "References",
    "SituationModel",
    "Priority",
    "ResponseAction",
    "SpeechPolicy",
    "Utterance",
    "CancellationToken",
    "NodeStatus",
    "TaskGraph",
    "TaskNode",
    "ContextBudget",
    "Turn",
    "SessionRegistry",
    "VoiceSession",
    "get_registry",
]
