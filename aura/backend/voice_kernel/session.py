"""
voice_kernel.session — the live conversation kernel (§1, §2, §14, §19, §33, §37).
================================================================================

`VoiceSession` is the object the old code never had: one long-lived thing per
conversation that owns the state machine, the endpointer, the situation model,
the task graph and the context budget, and turns `Event`s into `Directive`s.

This is what makes §1 true. The pipeline

    Mic → VAD → STT → state → intent → turn-taking → planning → tools
        → observation → response → TTS → keep listening

is not a chain of HTTP requests here; it is a sequence of events against one
session whose state survives between them. `handle()` is the only entry point,
it never blocks, and it never performs I/O — the transport does that, driven by
the directives it gets back.

Read `handle()` top to bottom to understand the whole turn lifecycle.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .context import ContextBudget, Turn
from .endpointing import EndpointDecision, EndpointDetector
from .events import Directive, DirectiveKind, Event, EventKind
from .intent import Intent, IntentCategory, IntentClassifier, detect_control_phrase
from .persona import VOICE_SYSTEM_PROMPT
from .situation import ConversationMode, SituationModel, Speaker
from .speech_policy import Priority, PolicyDecision, ResponseAction, SpeechPolicy, Utterance
from .states import IllegalTransition, VoiceState, VoiceStateMachine
from .task_graph import NodeStatus, TaskGraph

#: §19 — where a TTS chunk may be cut. Streaming word-by-word to a TTS engine
#: produces robotic prosody; streaming whole sentences sounds human.
_SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s+|(?<=[.!?。？！])$|(?<=[:;])\s+")
#: Minimum characters before a chunk is worth sending to TTS.
_MIN_CHUNK_CHARS = 18
#: §33 — the wake word, matched case-insensitively as a whole word.
_WAKE = re.compile(r"\b(akansha|akaansha|akansa|ak(?:ka)?ansha|अकांशा|అకాంక్ష)\b", re.IGNORECASE)


@dataclass
class SessionConfig:
    """Everything tunable about a session, in one place for the settings UI."""

    model: str = ""
    #: Defaults to the real persona rather than "". An empty system prompt left
    #: the metadata line in `context.build()` as the only system content a model
    #: ever saw, and it answered in that format — see `persona.py`. Callers that
    #: genuinely want no instructions must now pass `system_prompt=""` on purpose.
    system_prompt: str = VOICE_SYSTEM_PROMPT
    mode: ConversationMode = ConversationMode.VOICE
    language: str = "english"
    #: §19 TTS shaping, passed through on every SPEAK directive.
    voice: Optional[str] = None
    speech_speed: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    #: §5 — endpointing overrides surfaced to the user.
    endpointing: Dict[str, float] = field(default_factory=dict)
    require_wake_word: Optional[bool] = None

    def tts_options(self) -> Dict[str, Any]:
        return {
            "voice": self.voice,
            "speech_speed": self.speech_speed,
            "pitch": self.pitch,
            "volume": self.volume,
            "language": self.language,
        }


class VoiceSession:
    """One live conversation. Push `Event`s in, read `Directive`s out."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        *,
        config: Optional[SessionConfig] = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.config = config or SessionConfig()

        self.fsm = VoiceStateMachine()
        self.endpointer = EndpointDetector(**self.config.endpointing)
        self.classifier = IntentClassifier()
        self.situation = SituationModel(
            session_id=self.id,
            mode=self.config.mode,
            language=self.config.language,
        )
        self.policy = SpeechPolicy()
        self.context = ContextBudget(
            model=self.config.model,
            system_prompt=self.config.system_prompt,
        )
        self.graph = TaskGraph(session_id=self.id)

        # ── turn-in-progress state ────────────────────────────────────────
        self._partial: str = ""
        self._last_speech_at: float = 0.0
        self._speech_started_at: Optional[float] = None
        self._prosody: Optional[Dict[str, float]] = None
        self._last_endpoint: Optional[EndpointDecision] = None
        self._last_intent: Optional[Intent] = None
        self._pending_intent: Optional[Intent] = None

        # ── response streaming state (§19) ────────────────────────────────
        self._response_buffer: str = ""
        self._response_text: str = ""
        #: True from the moment we hand the client something to say until it
        #: reports playback finished (or we cut it off). Deliberately *not*
        #: derived from the FSM: "am I making sound" and "whose turn is it" are
        #: different questions. A P1 read-back is spoken from CONFIRMING and
        #: progress from EXECUTING, and in both the FSM stays put — so keying
        #: barge-in off `holds_floor` alone left the user unable to cut into
        #: exactly the utterances they most need to interrupt (§4).
        self._tts_active = False

        self.created_at = time.time()
        self.last_activity_at = time.time()
        self.closed = False
        #: Anything the transport wants to hang off the session (ws handle, etc).
        self.transport: Dict[str, Any] = {}

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def state(self) -> VoiceState:
        return self.fsm.state

    @property
    def task_active(self) -> bool:
        return self.graph.is_running or bool(self.graph.ready_nodes())

    def status(self) -> Dict[str, Any]:
        """The payload the UI needs to render the 3D core and side panels."""
        return {
            "session_id": self.id,
            "state": self.state.value,
            "time_in_state": round(self.fsm.time_in_state, 3),
            "situation": self.situation.as_dict(),
            "task": self.graph.as_dict() if len(self.graph) else None,
            "context": {
                "fill": self.context.fill,
                "indicator": self.context.indicator(),
                "model": self.context.model,
                "window": self.context.window,
                "turns": self.context.total_turns_seen,
                "summaries": len(self.context.summaries),
            },
            "partial_transcript": self._partial,
            "endpoint": (
                {
                    "probability": self._last_endpoint.turn_complete_probability,
                    "threshold": self._last_endpoint.threshold,
                    "reason": self._last_endpoint.reason,
                    "signals": self._last_endpoint.signals,
                }
                if self._last_endpoint
                else None
            ),
            "intent": self._last_intent.as_dict() if self._last_intent else None,
        }

    # ── the single entry point ────────────────────────────────────────────
    def handle(self, event: Event) -> List[Directive]:
        """Apply one event. Never blocks, never performs I/O, never raises."""
        self.last_activity_at = time.time()
        before = self.state
        out: List[Directive] = []

        try:
            handler = self._HANDLERS.get(event.kind)
            if handler is None:
                out.append(Directive(DirectiveKind.NOOP, {"ignored": event.kind.value}))
            else:
                out.extend(handler(self, event) or [])
        except IllegalTransition as exc:
            # Not fatal: an out-of-order event from a flaky transport must not
            # wedge the session. Report it and carry on in the current state.
            out.append(Directive(DirectiveKind.NOOP, {"illegal": str(exc)}))
        except Exception as exc:  # pragma: no cover - defensive
            self.fsm.force(VoiceState.ERROR, EventKind.ERROR)
            out.append(Directive(DirectiveKind.STATE_CHANGED, {
                "state": VoiceState.ERROR.value, "error": str(exc),
            }))

        if self.state is not before and not any(
            d.kind is DirectiveKind.STATE_CHANGED for d in out
        ):
            out.insert(0, self._state_changed(before))

        if self.context.needs_compression:
            out.append(Directive(DirectiveKind.COMPRESS_CONTEXT, {
                "turns": len(self.context.turns),
                "fill": self.context.fill,
            }))
        return out

    def _state_changed(self, previous: VoiceState) -> Directive:
        """§24 — the 3D core reacts to this, so it carries enough to animate."""
        return Directive(DirectiveKind.STATE_CHANGED, {
            "state": self.state.value,
            "previous": previous.value,
            "task_active": self.task_active,
            "speaker": self.situation.current_speaker.value,
        })

    # ── session lifecycle ─────────────────────────────────────────────────
    def _on_session_open(self, event: Event) -> List[Directive]:
        patch = event.get("environment")
        if isinstance(patch, dict):
            self.situation.environment.apply(patch)
        model = event.get("model")
        if model:
            self.context.set_model(model, event.get("window"))
            self.config.model = model
        self.fsm.transition(EventKind.SESSION_OPEN)
        self.closed = False
        return [
            Directive(DirectiveKind.START_LISTENING, {
                "needs_wake_word": self._needs_wake_word(),
                "endpointing": {
                    "base_silence_ms": self.endpointer.base_silence_ms,
                    "min_silence_ms": self.endpointer.min_silence_ms,
                    "max_silence_ms": self.endpointer.max_silence_ms,
                },
            }),
        ]

    def _on_session_close(self, event: Event) -> List[Directive]:
        self.closed = True
        cancelled = self.graph.cancel_all("session closed")
        self.fsm.transition(EventKind.SESSION_CLOSE)
        out: List[Directive] = [Directive(DirectiveKind.STOP_LISTENING, {"reason": "session closed"})]
        if cancelled:
            out.append(Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": cancelled}))
        return out

    def _on_environment_update(self, event: Event) -> List[Directive]:
        patch = event.get("environment") or event.payload
        changed = self.situation.environment.apply(
            {k: v for k, v in patch.items() if k != "environment"}
        )
        if not changed:
            return []
        return [Directive(DirectiveKind.SITUATION_CHANGED, {
            "changed": changed,
            "environment": self.situation.environment.as_dict(),
        })]

    # ── §4/§5: VAD edges and barge-in ─────────────────────────────────────
    def _on_speech_start(self, event: Event) -> List[Directive]:
        source = (event.get("source") or "user").lower()
        if source in ("assistant", "system_audio", "music", "keyboard", "background"):
            # §5 — not the user. Do not treat our own playback as a barge-in.
            return [Directive(DirectiveKind.NOOP, {"ignored_source": source})]

        self._speech_started_at = event.at
        self._last_speech_at = event.at
        was_speaking = self.fsm.holds_floor or self._tts_active
        previous = self.state
        self.fsm.transition(EventKind.SPEECH_START)

        out: List[Directive] = []
        if was_speaking:
            # True barge-in (§4): kill playback before anything else, and drop
            # everything we were queued up to say.
            dropped = self.policy.flush_non_critical()
            self.situation.note_interruption()
            self._tts_active = False
            out.append(Directive(DirectiveKind.STOP_TTS, {
                "reason": "user barge-in",
                "dropped_utterances": dropped,
                "spoken_so_far": self._response_text,
            }))
            self._response_buffer = ""
        out.append(self._state_changed(previous))
        out.append(Directive(DirectiveKind.START_TRANSCRIBING, {
            "barge_in": was_speaking,
        }))
        return out

    def _on_speech_frame(self, event: Event) -> List[Directive]:
        self._last_speech_at = event.at
        prosody = event.get("prosody")
        if isinstance(prosody, dict):
            self._prosody = prosody
        return []

    def _on_speech_end(self, event: Event) -> List[Directive]:
        self._last_speech_at = event.at
        previous = self.state
        self.fsm.try_transition(EventKind.SPEECH_END)
        if self.state is not previous:
            return [self._state_changed(previous)]
        return []

    def _on_silence_tick(self, event: Event) -> List[Directive]:
        """The endpointing heartbeat (§6). Fires while no speech is detected."""
        if not self._partial.strip():
            return []
        silence_ms = float(event.get("silence_ms") or self._silence_ms(event.at))
        return self._evaluate_endpoint(silence_ms, recognizer_final=False)

    def _silence_ms(self, now: float) -> float:
        if not self._last_speech_at:
            return 0.0
        return max(0.0, (now - self._last_speech_at) * 1000.0)

    # ── §6: transcripts and turn completion ───────────────────────────────
    def _on_partial_transcript(self, event: Event) -> List[Directive]:
        text = (event.get("text") or "").strip()
        if not text:
            return []
        self._partial = text
        previous = self.state
        self.fsm.try_transition(EventKind.PARTIAL_TRANSCRIPT)
        out: List[Directive] = []
        if self.state is not previous:
            out.append(self._state_changed(previous))

        # §20 — a stop must land from a *partial*. Waiting for the final
        # transcript is what makes "stop" feel unresponsive mid-execution.
        fast = self.classifier.classify(
            text,
            stt_confidence=float(event.get("confidence") or 1.0),
            task_active=self.task_active,
            assistant_holds_floor=self.fsm.holds_floor,
        )
        if fast.is_control:
            self._partial = ""
            self._last_intent = fast
            out.extend(self._apply_control(fast))
            return out

        silence_ms = float(event.get("silence_ms") or self._silence_ms(event.at))
        out.extend(self._evaluate_endpoint(silence_ms, recognizer_final=False))
        return out

    def _on_final_transcript(self, event: Event) -> List[Directive]:
        text = (event.get("text") or "").strip()
        if text:
            self._partial = text
        if not self._partial.strip():
            return []
        silence_ms = float(event.get("silence_ms") or self._silence_ms(event.at))
        confidence = float(event.get("confidence") or 1.0)
        return self._evaluate_endpoint(
            silence_ms, recognizer_final=True, stt_confidence=confidence
        )

    def _evaluate_endpoint(
        self,
        silence_ms: float,
        *,
        recognizer_final: bool,
        stt_confidence: float = 1.0,
    ) -> List[Directive]:
        decision = self.endpointer.evaluate(
            transcript=self._partial,
            silence_ms=silence_ms,
            speech_duration_ms=(
                (time.monotonic() - self._speech_started_at) * 1000.0
                if self._speech_started_at else 0.0
            ),
            prosody=self._prosody,
            pending_question=bool(self.situation.pending_question),
            executing=self.fsm.is_busy_executing,
            holds_floor=self.fsm.holds_floor,
            recognizer_final=recognizer_final,
        )
        self._last_endpoint = decision
        if not decision.should_finalize:
            # This is the "Open my project and..." case: do not submit.
            return [Directive(DirectiveKind.KEEP_LISTENING, {
                "transcript": self._partial,
                "probability": decision.turn_complete_probability,
                "threshold": decision.threshold,
                "recheck_in_ms": decision.recheck_in_ms,
                "reason": decision.reason,
                "signals": decision.signals,
            })]
        transcript = self._partial
        self._partial = ""
        self._speech_started_at = None
        self._prosody = None
        return [
            Directive(DirectiveKind.FINALIZE_TURN, {
                "transcript": transcript,
                "probability": decision.turn_complete_probability,
                "reason": decision.reason,
            }),
            *self._process_turn(transcript, stt_confidence=stt_confidence),
        ]

    # ── §9/§10/§33: what to do with a settled turn ────────────────────────
    def _needs_wake_word(self) -> bool:
        if self.config.require_wake_word is not None:
            return bool(self.config.require_wake_word)
        return self.situation.needs_wake_word

    def _process_turn(self, transcript: str, *, stt_confidence: float = 1.0) -> List[Directive]:
        # §33 — a cold session needs the wake word; a warm one does not.
        #
        # "stop", "cancel", "pause" and "resume" are exempt. The gate used to run
        # first, which made §4 barge-in unreachable in any mode that armed it: the
        # assistant would be mid-sentence, the user would say "stop", and the word
        # would be dropped for lacking a wake word it makes no sense to demand of
        # someone interrupting. Failing to stop is the worse error.
        if self._needs_wake_word() and not _WAKE.search(transcript):
            if detect_control_phrase(transcript) is None:
                self.situation.note_unaddressed(transcript)
                return [Directive(DirectiveKind.KEEP_LISTENING, {
                    "transcript": transcript,
                    "reason": "wake word required and not present",
                    "needs_wake_word": True,
                    "unaddressed_count": self.situation.unaddressed_count,
                })]
        cleaned = _WAKE.sub("", transcript).strip(" ,.!?") or transcript

        self.fsm.try_transition(EventKind.FINAL_TRANSCRIPT)
        intent = self.classifier.classify(
            cleaned,
            stt_confidence=stt_confidence,
            pending_question=bool(self.situation.pending_question),
            pending_confirmation=bool(self.situation.pending_confirmation),
            task_active=self.task_active,
            assistant_holds_floor=self.fsm.holds_floor,
            known_entities=self._known_entities(),
        )
        self._last_intent = intent
        self.situation.note_user_turn(intent=intent.category.value)
        self.context.add_turn(Turn(
            "user", cleaned, stt_confidence=stt_confidence, intent=intent.category.value,
        ))

        if intent.is_control:
            return self._apply_control(intent)

        decision = self.policy.decide(
            intent, self.situation, self.state, task_active=self.task_active
        )
        return self._apply_decision(decision, intent)

    def _known_entities(self) -> List[str]:
        r = self.situation.references
        seen = [r.last_object, r.last_file, r.last_application, r.last_url, r.current_task]
        return [s for s in seen if s]

    def _apply_control(self, intent: Intent) -> List[Directive]:
        """§4/§20 — stop and cancel, from any state, with no confidence gate."""
        pause = intent.action == "pause"
        out: List[Directive] = [Directive(DirectiveKind.STOP_TTS, {"reason": intent.category.value})]
        self.policy.flush_non_critical()
        # Playback is dead as of the directive above; the ack below re-arms it.
        self._tts_active = False

        if pause:
            self.fsm.try_transition(EventKind.USER_PAUSE)
            paused = [
                n.id for n in self.graph.iter_nodes() if n.status is NodeStatus.RUNNING
            ]
            out.append(Directive(DirectiveKind.PAUSE_EXECUTION, {"nodes": paused}))
            out.append(self._speak("Paused.", Priority.P0_EMERGENCY, "pause-ack", force=True))
            return [d for d in out if d is not None]

        cancelled = self.graph.cancel_all(f"user {intent.category.value.lower()}")
        self.fsm.try_transition(EventKind.USER_STOP)
        out.append(Directive(DirectiveKind.CANCEL_EXECUTION, {
            "nodes": cancelled,
            "reason": intent.cleaned_text,
        }))
        word = "Cancelled." if intent.category is IntentCategory.CANCELLATION else "Stopped."
        out.append(self._speak(word, Priority.P0_EMERGENCY, "stop-ack", force=True))
        self.situation.clear_confirmation()
        self.situation.answered()
        return [d for d in out if d is not None]

    def _apply_decision(self, decision: PolicyDecision, intent: Intent) -> List[Directive]:
        """Turn a §9 ResponseAction into transport directives."""
        out: List[Directive] = []
        action = decision.action

        if action is ResponseAction.ASK:
            question = decision.question or "Could you say that again?"
            kind = (
                DirectiveKind.REQUEST_CONFIRMATION
                if decision.requires_confirmation
                else DirectiveKind.ASK_CLARIFICATION
            )
            if decision.requires_confirmation:
                self.situation.request_confirmation({
                    "text": intent.cleaned_text,
                    "category": intent.category.value,
                    "destructive": intent.is_destructive,
                    "entities": intent.entities,
                })
                self.fsm.force(VoiceState.CONFIRMING, EventKind.FINAL_TRANSCRIPT)
            else:
                self.fsm.force(VoiceState.WAITING, EventKind.FINAL_TRANSCRIPT)
            self.situation.ask(question)
            self.context.add_turn(Turn("assistant", question, intent="ask"))
            out.append(Directive(kind, {
                "question": question,
                "reason": decision.reason,
                "intent": intent.as_dict(),
            }))
            spoken = self._speak(
                question,
                Priority.P1_SAFETY if decision.requires_confirmation else Priority.P2_DIRECT_ANSWER,
                "ask",
                force=True,
            )
            if spoken:
                out.append(spoken)
            return out

        if action is ResponseAction.EXECUTE:
            return out + self._begin_execution(intent, decision)

        if action in (ResponseAction.SPEAK, ResponseAction.ACKNOWLEDGE):
            self.situation.answered()
            if decision.utterance:
                self.context.add_turn(Turn("assistant", decision.utterance.text))
                spoken = self._speak(
                    decision.utterance.text,
                    decision.utterance.priority,
                    decision.utterance.dedupe_key,
                    force=True,
                )
                if spoken:
                    out.append(spoken)
                return out
            self.fsm.try_transition(EventKind.RESPONSE_TOKEN)
            out.append(Directive(DirectiveKind.GENERATE_RESPONSE, {
                "intent": intent.as_dict(),
                "reason": decision.reason,
                **self.build_prompt(),
            }))
            return out

        if action is ResponseAction.END_CONVERSATION:
            if decision.utterance:
                spoken = self._speak(
                    decision.utterance.text, decision.utterance.priority, "farewell", force=True
                )
                if spoken:
                    out.append(spoken)
            out.append(Directive(DirectiveKind.STOP_LISTENING, {"reason": "conversation ended"}))
            return out

        if action is ResponseAction.WAIT:
            out.append(Directive(DirectiveKind.KEEP_LISTENING, {
                "reason": decision.reason, "transcript": intent.cleaned_text,
            }))
            return out

        if action is ResponseAction.STAY_SILENT:
            out.append(Directive(DirectiveKind.NOOP, {"reason": decision.reason}))
            return out

        # LISTEN — the default. §7's backchannel path lands here, and the
        # important part is that the floor stays with the user.
        out.append(Directive(DirectiveKind.KEEP_LISTENING, {
            "reason": decision.reason,
            "backchannel": intent.category is IntentCategory.BACKCHANNEL,
        }))
        return out

    # ── §16/§35: execution and re-planning ────────────────────────────────
    def _begin_execution(self, intent: Intent, decision: PolicyDecision) -> List[Directive]:
        """The voice→task-graph seam. §16: one system, not a chatbot beside a runner."""
        self.situation.answered()
        confirmed = self.situation.pending_confirmation
        self.situation.clear_confirmation()

        # A confirmation reply carries no content of its own — execute what was
        # read back, not the word "yes".
        if confirmed and intent.category is IntentCategory.CONFIRMATION:
            description = confirmed.get("text") or intent.cleaned_text
            entities = confirmed.get("entities") or {}
            destructive = bool(confirmed.get("destructive"))
        else:
            description = intent.cleaned_text
            entities = intent.entities
            destructive = intent.is_destructive

        if intent.action == "resume":
            self.fsm.try_transition(EventKind.USER_RESUME)
            return [Directive(DirectiveKind.RESUME_EXECUTION, {
                "task": self.graph.as_dict() if len(self.graph) else None,
            })]

        # §35 — a correction amends the existing plan instead of starting over.
        if intent.category is IntentCategory.CORRECTION and len(self.graph):
            replaced = self._apply_correction(description)
            if replaced:
                return replaced

        self.situation.references.note_action(
            description,
            obj=entities.get("query") or entities.get("site"),
            file=entities.get("file_path"),
            app=entities.get("site"),
            url=entities.get("url"),
            command=description,
        )
        if not self.graph.goal:
            self.graph.goal = description
        node = self.graph.add(
            description,
            depends_on=[n.id for n in self.graph.iter_nodes() if n.status is NodeStatus.RUNNING],
            requires_confirmation=destructive,
            kind="automation" if intent.category is IntentCategory.AUTOMATION else "command",
            args=dict(entities),
        )
        self.situation.references.current_task = self.graph.goal
        self.situation.references.current_step = description
        self.context.working.open_tasks = [
            n.description for n in self.graph.iter_nodes() if not n.is_terminal
        ]
        self.fsm.try_transition(EventKind.TASK_STARTED)
        return [Directive(DirectiveKind.EXECUTE_PLAN, {
            "node_id": node.id,
            "description": description,
            "intent": intent.as_dict(),
            "recurring": intent.is_recurring,
            "task": self.graph.as_dict(),
            **self.build_prompt(),
        })]

    def _apply_correction(self, description: str) -> List[Directive]:
        """"Actually, staging" → cancel the production node, add the staging one."""
        candidates = [
            n for n in self.graph.iter_nodes()
            if n.status in (NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.WAITING)
        ]
        if not candidates:
            return []
        target = candidates[-1]
        new = self.graph.replace(target.id, description, reason="user correction")
        self.context.working.note_decision(f"{description} (replaces: {target.description})")
        self.situation.references.current_step = description
        self.context.working.open_tasks = [
            n.description for n in self.graph.iter_nodes() if not n.is_terminal
        ]
        return [
            Directive(DirectiveKind.CANCEL_EXECUTION, {
                "nodes": [target.id],
                "reason": "replaced by correction",
            }),
            Directive(DirectiveKind.EXECUTE_PLAN, {
                "node_id": new.id,
                "description": description,
                "replaces": target.id,
                "task": self.graph.as_dict(),
                **self.build_prompt(),
            }),
        ]

    def build_prompt(self, **extra: Any) -> Dict[str, Any]:
        """§11 — the assembled context for whoever calls the model."""
        return {
            "context": self.context.build(
                situation_line=self._situation_line(),
                task_line=self.graph.spoken_summary() if len(self.graph) else "",
                **extra,
            )
        }

    def _situation_line(self) -> str:
        env = self.situation.environment
        bits = [
            f"mode={self.situation.mode.value}",
            f"state={self.state.value}",
            f"language={self.situation.language}",
        ]
        if env.active_window:
            bits.append(f"active_window={env.active_window}")
        if env.current_url:
            bits.append(f"url={env.current_url}")
        if env.project:
            bits.append(f"project={env.project}")
        if self.situation.references.last_action:
            bits.append(f"last_action={self.situation.references.last_action}")
        return ", ".join(bits)

    # ── §18/§19: outbound speech ──────────────────────────────────────────
    def _speak(
        self,
        text: str,
        priority: Priority = Priority.P5_PROGRESS,
        dedupe_key: Optional[str] = None,
        *,
        force: bool = False,
    ) -> Optional[Directive]:
        """Build a SPEAK directive if the §18 gate allows it, else None."""
        text = (text or "").strip()
        if not text:
            return None
        utterance = Utterance(text, priority, dedupe_key)
        if not force:
            admitted, reason = self.policy.admit(utterance, self.situation, self.state)
            if not admitted:
                self.policy.enqueue(utterance)
                return None
        self.policy.mark_spoken(utterance)
        self.situation.note_assistant_turn()
        self.fsm.try_transition(EventKind.TTS_STARTED)
        self._tts_active = True
        return Directive(
            DirectiveKind.SPEAK,
            {"text": text, "dedupe_key": dedupe_key, **self.config.tts_options()},
            priority=int(priority),
        )

    def _on_response_token(self, event: Event) -> List[Directive]:
        """§19 — accumulate tokens, emit TTS chunks on sentence boundaries."""
        token = event.get("text") or event.get("token") or ""
        if not token:
            return []
        previous = self.state
        self.fsm.try_transition(EventKind.RESPONSE_TOKEN)
        self._response_buffer += token
        self._response_text += token

        out: List[Directive] = []
        if self.state is not previous:
            out.append(self._state_changed(previous))
        for chunk in self._drain_sentences():
            spoken = self._speak(chunk, Priority.P2_DIRECT_ANSWER, force=True)
            if spoken:
                out.append(spoken)
        return out

    def _drain_sentences(self, *, flush: bool = False) -> List[str]:
        """Split the buffer at sentence boundaries, keeping the tail unsent."""
        chunks: List[str] = []
        buffer = self._response_buffer
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match:
                break
            cut = match.end()
            candidate = buffer[:cut].strip()
            if len(candidate) < _MIN_CHUNK_CHARS and not flush:
                # Too short to sound natural on its own — wait for more.
                nxt = _SENTENCE_END.search(buffer, cut)
                if nxt is None:
                    break
                cut = nxt.end()
                candidate = buffer[:cut].strip()
            buffer = buffer[cut:]
            if candidate:
                chunks.append(candidate)
        if flush and buffer.strip():
            chunks.append(buffer.strip())
            buffer = ""
        self._response_buffer = buffer
        return chunks

    def _on_response_complete(self, event: Event) -> List[Directive]:
        text = event.get("text")
        if text:
            # A non-streaming caller can hand us the whole reply at once.
            self._response_buffer = text if not self._response_text else self._response_buffer
            self._response_text = text
        previous = self.state
        self.fsm.try_transition(EventKind.RESPONSE_COMPLETE)
        out: List[Directive] = []
        for chunk in self._drain_sentences(flush=True):
            spoken = self._speak(chunk, Priority.P2_DIRECT_ANSWER, force=True)
            if spoken:
                out.append(spoken)
        if self._response_text.strip():
            self.context.add_turn(Turn("assistant", self._response_text.strip()))
        self._response_text = ""
        self._response_buffer = ""
        if self.state is not previous and not out:
            out.append(self._state_changed(previous))
        return out

    def _on_tts_started(self, event: Event) -> List[Directive]:
        self.fsm.try_transition(EventKind.TTS_STARTED)
        self._tts_active = True
        return []

    def _on_tts_finished(self, event: Event) -> List[Directive]:
        previous = self.state
        self.fsm.try_transition(EventKind.TTS_FINISHED)
        self._tts_active = False
        out: List[Directive] = []
        if self.state is not previous:
            out.append(self._state_changed(previous))
        # Anything the rate limiter held back is now due.
        for utterance in self.policy.drain(self.situation, self.state):
            spoken = self._speak(
                utterance.text, utterance.priority, utterance.dedupe_key, force=True
            )
            if spoken:
                out.append(spoken)
        if self.fsm.is_user_turn:
            out.append(Directive(DirectiveKind.START_LISTENING, {
                "needs_wake_word": self._needs_wake_word(),
            }))
        return out

    # ── §14/§17: task lifecycle and commentary ────────────────────────────
    def _on_task_started(self, event: Event) -> List[Directive]:
        node_id = event.get("node_id")
        goal = event.get("description") or event.get("goal") or ""
        if node_id and node_id in self.graph:
            self.graph.start(node_id)
        elif goal:
            node = self.graph.add(goal)
            self.graph.start(node.id)
            node_id = node.id
        if goal and not self.graph.goal:
            self.graph.goal = goal
        self.situation.references.current_task = self.graph.goal
        self.fsm.try_transition(EventKind.TASK_STARTED)
        spoken = self._speak(
            f"Starting: {goal or self.graph.goal}.", Priority.P4_KEY_PROGRESS, f"start-{node_id}"
        )
        return [d for d in (spoken,) if d is not None]

    def _on_step_started(self, event: Event) -> List[Directive]:
        node_id = event.get("node_id")
        description = event.get("description") or ""
        if node_id and node_id in self.graph:
            node = self.graph.get(node_id)
            if node.status is NodeStatus.PENDING:
                self.graph.start(node_id)
        elif description:
            node = self.graph.add(description)
            self.graph.start(node.id)
            node_id = node.id
        self.situation.references.current_step = description or None
        self.fsm.try_transition(EventKind.STEP_STARTED)
        # §17/§18 — ordinary progress is P5 and rate-limited, so a chatty
        # executor cannot turn the assistant into a narrator.
        spoken = self._speak(
            description or "Working on the next step.",
            Priority.P5_PROGRESS,
            f"step-{node_id}",
        )
        return [d for d in (spoken,) if d is not None]

    def _on_step_completed(self, event: Event) -> List[Directive]:
        node_id = event.get("node_id")
        if node_id and node_id in self.graph:
            self.graph.complete(node_id, event.get("result"))
        self.fsm.try_transition(EventKind.STEP_COMPLETED)
        self.context.working.open_tasks = [
            n.description for n in self.graph.iter_nodes() if not n.is_terminal
        ]
        out: List[Directive] = []
        summary = event.get("summary")
        if summary:
            spoken = self._speak(summary, Priority.P5_PROGRESS, f"done-{node_id}")
            if spoken:
                out.append(spoken)
        return out

    def _on_step_failed(self, event: Event) -> List[Directive]:
        node_id = event.get("node_id")
        error = event.get("error") or "unknown error"
        node = None
        if node_id and node_id in self.graph:
            node = self.graph.get(node_id)
            self.graph.fail(node_id, error)
        self.fsm.try_transition(EventKind.STEP_FAILED)
        out: List[Directive] = []
        if node is not None and node.can_retry:
            self.graph.retry(node_id)
            out.append(Directive(DirectiveKind.EXECUTE_PLAN, {
                "node_id": node_id,
                "description": node.description,
                "retry": node.retry_count,
                "task": self.graph.as_dict(),
            }))
            return out
        # §18 P3 — a failure is worth interrupting ordinary progress for.
        spoken = self._speak(
            f"{(node.description + ' failed') if node else 'A step failed'}: {error}",
            Priority.P3_FAILURE,
            f"fail-{node_id}",
            force=True,
        )
        if spoken:
            out.append(spoken)
        return out

    def _on_task_completed(self, event: Event) -> List[Directive]:
        node_id = event.get("node_id")
        if node_id and node_id in self.graph:
            self.graph.complete(node_id, event.get("result"))
        previous = self.state
        self.fsm.try_transition(EventKind.TASK_COMPLETED)
        self.context.working.open_tasks = [
            n.description for n in self.graph.iter_nodes() if not n.is_terminal
        ]
        out: List[Directive] = []
        if self.state is not previous:
            out.append(self._state_changed(previous))
        spoken = self._speak(
            event.get("summary") or f"Done{': ' + self.graph.goal if self.graph.goal else ''}.",
            Priority.P4_KEY_PROGRESS,
            "task-done",
            force=True,
        )
        if spoken:
            out.append(spoken)
        return out

    def _on_task_failed(self, event: Event) -> List[Directive]:
        error = event.get("error") or "unknown error"
        node_id = event.get("node_id")
        if node_id and node_id in self.graph:
            self.graph.fail(node_id, error)
        previous = self.state
        self.fsm.try_transition(EventKind.TASK_FAILED)
        out: List[Directive] = []
        if self.state is not previous:
            out.append(self._state_changed(previous))
        spoken = self._speak(
            f"The task failed: {error}", Priority.P3_FAILURE, "task-failed", force=True
        )
        if spoken:
            out.append(spoken)
        return out

    # ── §4/§20: explicit control from the UI (button, hotkey) ──────────────
    def _on_user_stop(self, event: Event) -> List[Directive]:
        previous = self.state
        dropped = self.policy.flush_non_critical()
        cancelled = self.graph.cancel_all(event.get("reason") or "user stop")
        self.fsm.transition(EventKind.USER_STOP)
        self.situation.clear_confirmation()
        self.situation.answered()
        self._partial = ""
        self._response_buffer = ""
        self._response_text = ""
        self._tts_active = False
        out = [
            self._state_changed(previous),
            Directive(DirectiveKind.STOP_TTS, {"reason": "user stop", "dropped_utterances": dropped}),
            Directive(DirectiveKind.CANCEL_EXECUTION, {"nodes": cancelled}),
        ]
        spoken = self._speak("Stopped.", Priority.P0_EMERGENCY, "stop-ack", force=True)
        if spoken:
            out.append(spoken)
        return out

    def _on_user_pause(self, event: Event) -> List[Directive]:
        previous = self.state
        self.fsm.try_transition(EventKind.USER_PAUSE)
        running = [n.id for n in self.graph.iter_nodes() if n.status is NodeStatus.RUNNING]
        out = [
            self._state_changed(previous),
            Directive(DirectiveKind.PAUSE_EXECUTION, {"nodes": running}),
        ]
        spoken = self._speak("Paused.", Priority.P0_EMERGENCY, "pause-ack", force=True)
        if spoken:
            out.append(spoken)
        return out

    def _on_user_resume(self, event: Event) -> List[Directive]:
        previous = self.state
        self.fsm.try_transition(EventKind.USER_RESUME)
        return [
            self._state_changed(previous),
            Directive(DirectiveKind.RESUME_EXECUTION, {
                "task": self.graph.as_dict() if len(self.graph) else None,
            }),
        ]

    def _on_user_confirm(self, event: Event) -> List[Directive]:
        pending = self.situation.pending_confirmation
        if not pending:
            return [Directive(DirectiveKind.NOOP, {"reason": "nothing to confirm"})]
        intent = self.classifier.classify(
            pending.get("text") or "",
            pending_confirmation=True,
            task_active=self.task_active,
        )
        self.fsm.try_transition(EventKind.USER_CONFIRM)
        # The read-back already served as the safety gate; do not ask twice.
        intent.needs_confirmation = False
        intent.needs_clarification = False
        return self._begin_execution(intent, PolicyDecision(
            ResponseAction.EXECUTE, "confirmed via UI"
        ))

    def _on_user_reject(self, event: Event) -> List[Directive]:
        previous = self.state
        self.situation.clear_confirmation()
        self.situation.answered()
        self.fsm.try_transition(EventKind.USER_REJECT)
        out = [self._state_changed(previous)]
        spoken = self._speak(
            "Understood, leaving it.", Priority.P2_DIRECT_ANSWER, "reject-ack", force=True
        )
        if spoken:
            out.append(spoken)
        return out

    def _on_error(self, event: Event) -> List[Directive]:
        message = event.get("message") or event.get("error") or "unknown error"
        previous = self.state
        self.fsm.transition(
            EventKind.STT_ERROR if event.kind is EventKind.STT_ERROR else EventKind.ERROR
        )
        self._partial = ""
        out = [Directive(DirectiveKind.STATE_CHANGED, {
            "state": self.state.value,
            "previous": previous.value,
            "error": message,
        })]
        # An STT failure is recoverable: reopen the mic rather than sitting in ERROR.
        if event.kind is EventKind.STT_ERROR:
            out.append(Directive(DirectiveKind.START_LISTENING, {
                "reason": "recovering from stt error",
                "needs_wake_word": self._needs_wake_word(),
            }))
        return out

    #: Dispatch table. Declared after the methods so the functions exist.
    _HANDLERS: Dict[EventKind, Callable[["VoiceSession", Event], List[Directive]]] = {}

    # ── §12: compression, driven from outside because it needs a model ─────
    def compression_batch(self) -> List[Dict[str, Any]]:
        return [t.as_dict() for t in self.context.take_compression_batch()]

    def apply_summary(self, summary: str) -> Dict[str, Any]:
        block = self.context.apply_summary(summary)
        return block.as_dict()

    def abort_compression(self) -> None:
        self.context.abort_compression()

    # ── §37/§38: checkpoints ──────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        """Everything needed to resume this conversation in a new process."""
        return {
            "version": 1,
            "session_id": self.id,
            "created_at": self.created_at,
            "saved_at": time.time(),
            "state": self.state.value,
            "config": {
                "model": self.config.model,
                "system_prompt": self.config.system_prompt,
                "mode": self.config.mode.value,
                "language": self.config.language,
                "voice": self.config.voice,
                "speech_speed": self.config.speech_speed,
                "pitch": self.config.pitch,
                "volume": self.config.volume,
                "endpointing": dict(self.config.endpointing),
                "require_wake_word": self.config.require_wake_word,
            },
            "situation": self.situation.as_dict(),
            "context": self.context.snapshot(),
            "task": self.graph.snapshot(),
        }

    @classmethod
    def restore(cls, data: Dict[str, Any]) -> "VoiceSession":
        raw = data.get("config") or {}
        config = SessionConfig(
            model=raw.get("model", ""),
            # A snapshot written before the persona existed carries "" here, and a
            # §37 resume must not silently restore the assistant to the state-block
            # behaviour the persona was written to fix.
            system_prompt=raw.get("system_prompt") or VOICE_SYSTEM_PROMPT,
            mode=ConversationMode(raw.get("mode", ConversationMode.VOICE.value)),
            language=raw.get("language", "english"),
            voice=raw.get("voice"),
            speech_speed=float(raw.get("speech_speed") or 1.0),
            pitch=float(raw.get("pitch") or 1.0),
            volume=float(raw.get("volume") or 1.0),
            endpointing=dict(raw.get("endpointing") or {}),
            require_wake_word=raw.get("require_wake_word"),
        )
        session = cls(data.get("session_id"), config=config)
        session.created_at = float(data.get("created_at") or time.time())
        session.context = ContextBudget.restore(data.get("context") or {})
        # `restore` replaces the context built from config, prompt included, so the
        # persona has to be re-asserted or an older snapshot resumes without one.
        if not session.context.system_prompt:
            session.context.system_prompt = config.system_prompt
        session.graph = TaskGraph.restore(data.get("task") or {})
        session.graph.session_id = session.id

        situation = data.get("situation") or {}
        s = session.situation
        s.current_topic = situation.get("current_topic")
        s.user_intent = situation.get("user_intent")
        s.turn_count = int(situation.get("turn_count") or 0)
        s.interruption_count = int(situation.get("interruption_count") or 0)
        s.language = situation.get("language") or config.language
        s.pending_question = situation.get("pending_question")
        s.pending_confirmation = situation.get("pending_confirmation")
        s.login_confirmed_sites = dict(situation.get("login_confirmed_sites") or {})
        references = situation.get("references") or {}
        for key, value in references.items():
            if hasattr(s.references, key):
                setattr(s.references, key, value)
        environment = situation.get("environment") or {}
        s.environment.apply({k: v for k, v in environment.items() if k != "is_stale"})
        return session

    def resume_plan(self) -> Dict[str, Any]:
        """§37 — what to tell the user on restart, and what is safe to continue.

        RUNNING nodes are demoted to PENDING because nothing is executing after
        a restart, and the environment they assumed may have changed.
        """
        reopened = self.graph.reopen_running()
        outstanding = self.graph.resumable_nodes()
        return {
            "session_id": self.id,
            "goal": self.graph.goal,
            "reopened": reopened,
            "outstanding": [n.description for n in outstanding],
            "question": (
                f"Last time we were working on {self.graph.goal or 'a task'} — "
                f"{len(outstanding)} step(s) left. Should I carry on?"
                if outstanding else ""
            ),
            "environment_stale": self.situation.environment.is_stale,
        }


VoiceSession._HANDLERS = {
    EventKind.SESSION_OPEN: VoiceSession._on_session_open,
    EventKind.SESSION_CLOSE: VoiceSession._on_session_close,
    EventKind.SPEECH_START: VoiceSession._on_speech_start,
    EventKind.SPEECH_FRAME: VoiceSession._on_speech_frame,
    EventKind.SPEECH_END: VoiceSession._on_speech_end,
    EventKind.SILENCE_TICK: VoiceSession._on_silence_tick,
    EventKind.PARTIAL_TRANSCRIPT: VoiceSession._on_partial_transcript,
    EventKind.FINAL_TRANSCRIPT: VoiceSession._on_final_transcript,
    EventKind.STT_ERROR: VoiceSession._on_error,
    EventKind.ERROR: VoiceSession._on_error,
    EventKind.RESPONSE_TOKEN: VoiceSession._on_response_token,
    EventKind.RESPONSE_COMPLETE: VoiceSession._on_response_complete,
    EventKind.TTS_STARTED: VoiceSession._on_tts_started,
    EventKind.TTS_FINISHED: VoiceSession._on_tts_finished,
    EventKind.TASK_STARTED: VoiceSession._on_task_started,
    EventKind.STEP_STARTED: VoiceSession._on_step_started,
    EventKind.STEP_COMPLETED: VoiceSession._on_step_completed,
    EventKind.STEP_FAILED: VoiceSession._on_step_failed,
    EventKind.TASK_COMPLETED: VoiceSession._on_task_completed,
    EventKind.TASK_FAILED: VoiceSession._on_task_failed,
    EventKind.USER_STOP: VoiceSession._on_user_stop,
    EventKind.USER_PAUSE: VoiceSession._on_user_pause,
    EventKind.USER_RESUME: VoiceSession._on_user_resume,
    EventKind.USER_CONFIRM: VoiceSession._on_user_confirm,
    EventKind.USER_REJECT: VoiceSession._on_user_reject,
    EventKind.ENVIRONMENT_UPDATE: VoiceSession._on_environment_update,
}


class SessionRegistry:
    """Per-session storage, replacing the process-global `_voice_engine`.

    The old singleton meant one `SessionState` for every user and conversation
    in the process: login confirmations, turn counts and pending questions all
    leaked across sessions. Keying by session id fixes that, and `sweep()`
    stops idle sessions from accumulating forever.
    """

    #: Sessions untouched for this long are collected by `sweep()`.
    idle_ttl_s: float = 3600.0
    #: Hard cap; the oldest idle session is evicted when exceeded.
    max_sessions: int = 200

    def __init__(self) -> None:
        self._sessions: Dict[str, VoiceSession] = {}

    def get(self, session_id: str) -> Optional[VoiceSession]:
        return self._sessions.get(session_id)

    def get_or_create(
        self, session_id: Optional[str] = None, *, config: Optional[SessionConfig] = None
    ) -> VoiceSession:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        session = VoiceSession(session_id, config=config)
        self._sessions[session.id] = session
        self.sweep()
        return session

    def adopt(self, session: VoiceSession) -> VoiceSession:
        """Register a session built elsewhere (e.g. `VoiceSession.restore`)."""
        self._sessions[session.id] = session
        self.sweep()
        return session

    def drop(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def sweep(self) -> List[str]:
        """Evict idle sessions. Returns the ids removed."""
        now = time.time()
        removed = [
            sid for sid, s in self._sessions.items()
            if (now - s.last_activity_at) > self.idle_ttl_s
        ]
        for sid in removed:
            self._sessions.pop(sid, None)
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.last_activity_at)
            self._sessions.pop(oldest.id, None)
            removed.append(oldest.id)
        return removed

    def __len__(self) -> int:
        return len(self._sessions)

    def ids(self) -> List[str]:
        return list(self._sessions)

    def snapshot_all(self) -> Dict[str, Any]:
        return {sid: s.snapshot() for sid, s in self._sessions.items()}


_REGISTRY: Optional[SessionRegistry] = None


def get_registry() -> SessionRegistry:
    """Process-wide registry. Sessions inside it are *not* shared state."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY
