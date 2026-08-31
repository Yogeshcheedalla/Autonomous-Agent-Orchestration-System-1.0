"""
Tests for `backend.voice_kernel` — the behaviours the old voice path got wrong.
=============================================================================

Each test names the spec section it covers. The bias is towards the specific
failures found in the audit rather than broad coverage: the substring
stop-word matches, the "Open my project and..." premature submit, the
process-global session state, and cancellation that took the whole plan down
with it.
"""

from __future__ import annotations

import pytest

from backend.voice_kernel import (
    ConversationMode,
    Directive,
    DirectiveKind,
    EndpointDetector,
    Event,
    EventKind,
    Intent,
    IntentCategory,
    IntentClassifier,
    NodeStatus,
    Priority,
    ResponseAction,
    SituationModel,
    SpeechPolicy,
    TaskGraph,
    VoiceSession,
    VoiceState,
    VoiceStateMachine,
    get_registry,
)
from backend.voice_kernel.session import SessionConfig
from backend.voice_kernel.states import IllegalTransition


def kinds(directives):
    return [d.kind for d in directives]


def payload(directives, kind):
    for d in directives:
        if d.kind is kind:
            return d.payload
    return None


# ── §3: the state machine ──────────────────────────────────────────────────
class TestStateMachine:
    def test_stop_is_legal_from_every_state(self):
        for state in VoiceState:
            fsm = VoiceStateMachine(state)
            assert fsm.can(EventKind.USER_STOP), state
            fsm.transition(EventKind.USER_STOP)
            assert fsm.state is VoiceState.STOPPED

    def test_error_is_legal_from_every_state(self):
        for state in VoiceState:
            fsm = VoiceStateMachine(state)
            fsm.transition(EventKind.ERROR)
            assert fsm.state is VoiceState.ERROR

    def test_illegal_transition_raises_rather_than_silently_passing(self):
        fsm = VoiceStateMachine(VoiceState.IDLE)
        with pytest.raises(IllegalTransition):
            fsm.transition(EventKind.TTS_FINISHED)
        assert fsm.try_transition(EventKind.TTS_FINISHED) is None
        assert fsm.state is VoiceState.IDLE

    def test_barge_in_path_from_the_spec(self):
        # SPEAKING → user speech → INTERRUPTED → transcribe → understand
        fsm = VoiceStateMachine(VoiceState.SPEAKING)
        assert fsm.holds_floor
        fsm.transition(EventKind.SPEECH_START)
        assert fsm.state is VoiceState.INTERRUPTED
        fsm.transition(EventKind.PARTIAL_TRANSCRIPT)
        assert fsm.state is VoiceState.TRANSCRIBING
        fsm.transition(EventKind.FINAL_TRANSCRIPT)
        assert fsm.state is VoiceState.UNDERSTANDING

    def test_conversation_stays_open_during_execution(self):
        # §14 — speech during EXECUTING must not knock us out of EXECUTING.
        fsm = VoiceStateMachine(VoiceState.EXECUTING)
        fsm.transition(EventKind.SPEECH_START)
        assert fsm.state is VoiceState.EXECUTING
        fsm.transition(EventKind.FINAL_TRANSCRIPT)
        assert fsm.state is VoiceState.UNDERSTANDING

    def test_history_is_bounded(self):
        fsm = VoiceStateMachine(VoiceState.LISTENING)
        for _ in range(300):
            fsm.transition(EventKind.SPEECH_START)
            fsm.transition(EventKind.SPEECH_END)
        assert len(fsm.history) <= VoiceStateMachine.HISTORY_LIMIT


# ── §6: end-of-turn detection ──────────────────────────────────────────────
class TestEndpointing:
    def setup_method(self):
        self.detector = EndpointDetector()

    def test_dangling_conjunction_does_not_finalize(self):
        # The canonical spec example. This is the bug a flat 1200ms timer has.
        decision = self.detector.evaluate(
            transcript="Open my project and", silence_ms=900
        )
        assert not decision.should_finalize
        assert "dangling" in decision.reason

    def test_completed_sentence_finalizes(self):
        decision = self.detector.evaluate(
            transcript="Open my project and find the failing tests", silence_ms=800
        )
        assert decision.should_finalize

    def test_hair_trigger_pause_never_finalizes(self):
        decision = self.detector.evaluate(transcript="open chrome", silence_ms=120)
        assert not decision.should_finalize
        assert decision.reason == "below minimum silence"

    def test_dangling_tail_cannot_hold_the_turn_forever(self):
        decision = self.detector.evaluate(transcript="Open my project and", silence_ms=3000)
        assert decision.should_finalize
        assert "max silence" in decision.reason

    @pytest.mark.parametrize(
        "flags",
        [
            {},
            {"recognizer_final": True},
            {"pending_question": True},
            {"holds_floor": True},
            {"recognizer_final": True, "pending_question": True, "holds_floor": True},
        ],
    )
    def test_an_open_structure_survives_every_threshold_adaptation(self, flags):
        """The canonical example used to pass by 0.0018, and only by luck.

        As one weighted signal among five, "dangling tail" scored 0.6182 against
        a 0.62 threshold — and each of `recognizer_final` (+0.15), the
        pending-question discount (-0.14) and the barge-in discount (-0.18) was
        on its own enough to flip it. `recognizer_final` is the damning one:
        Chrome emits `isFinal` on exactly the pause in "Open my project and…
        find the failing tests", so the browser path finalized mid-sentence
        every time while the default-argument unit test above stayed green.
        """
        decision = self.detector.evaluate(
            transcript="Open my project and", silence_ms=1300, **flags
        )
        assert not decision.should_finalize
        assert decision.turn_complete_probability < decision.threshold

    def test_a_marker_mid_utterance_is_still_only_a_vote(self):
        """The veto is for open *structure*, not for the word "and" appearing."""
        decision = self.detector.evaluate(
            transcript="open chrome and then tell me the time", silence_ms=800
        )
        assert decision.should_finalize

    def test_the_ceiling_outranks_the_veto(self):
        """Ordering, not tuning: whatever else fires, max silence has the last word."""
        decision = self.detector.evaluate(
            transcript="Open my project and",
            silence_ms=2600,
            pending_question=True,
            holds_floor=True,
        )
        assert decision.should_finalize
        assert "max silence" in decision.reason

    def test_short_control_word_finalizes_fast(self):
        decision = self.detector.evaluate(transcript="stop", silence_ms=300)
        assert decision.should_finalize

    def test_pending_question_lowers_the_bar(self):
        neutral = self.detector.evaluate(transcript="staging", silence_ms=300)
        asked = self.detector.evaluate(
            transcript="staging", silence_ms=300, pending_question=True
        )
        assert asked.threshold < neutral.threshold

    def test_terminal_punctuation_is_strong_evidence(self):
        with_punct = self.detector.evaluate(transcript="Deploy it.", silence_ms=400)
        without = self.detector.evaluate(transcript="Deploy it", silence_ms=400)
        assert with_punct.turn_complete_probability > without.turn_complete_probability

    def test_absent_prosody_is_not_a_neutral_guess(self):
        without = self.detector.evaluate(transcript="Deploy it.", silence_ms=500)
        assert "prosody" not in without.signals
        falling = self.detector.evaluate(
            transcript="Deploy it.", silence_ms=500, prosody={"pitch_delta_hz": -20}
        )
        assert "prosody" in falling.signals

    def test_standalone_reply_finalizes_at_the_floor(self):
        # A finished one-word reply cannot be continued, so waiting is pointless.
        for word in ("stop", "wait", "yes", "no", "cancel", "aagu", "रुको"):
            decision = self.detector.evaluate(transcript=word, silence_ms=260)
            assert decision.should_finalize, word
            assert "standalone reply" in decision.reason

    def test_standalone_shortcut_does_not_leak_into_longer_turns(self):
        # "stop the build and" starts with a control word but is unfinished.
        decision = self.detector.evaluate(transcript="stop the build and", silence_ms=300)
        assert not decision.should_finalize
        assert "standalone reply" not in decision.reason


# ── §10: intent classification ─────────────────────────────────────────────
class TestIntentClassifier:
    def setup_method(self):
        self.clf = IntentClassifier()

    # The five phrases the legacy substring matcher misread as CONTROL at 0.98.
    @pytest.mark.parametrize("text", [
        "i am waiting for the build to finish",
        "open my repo and halt nothing",
        "that was quite good",
        "find the paused video",
        "let's compare stop words",
    ])
    def test_substring_stop_words_are_not_interruptions(self, text):
        intent = self.clf.classify(text)
        assert intent.category is not IntentCategory.INTERRUPTION, intent.as_dict()
        assert not intent.is_control

    @pytest.mark.parametrize("text", ["stop", "stop it", "wait", "hold on", "aagu", "रुको"])
    def test_real_stop_words_still_stop(self, text):
        intent = self.clf.classify(text)
        assert intent.category is IntentCategory.INTERRUPTION, intent.as_dict()

    def test_stop_is_never_gated_on_a_shaky_transcript(self):
        # §36 inverted: refusing to stop because STT was unsure is the worse bug.
        intent = self.clf.classify("stop", stt_confidence=0.31)
        assert intent.category is IntentCategory.INTERRUPTION
        assert intent.action_confidence == 1.0
        assert not intent.needs_confirmation
        assert not intent.needs_clarification

    @pytest.mark.parametrize("text", ["yeah", "mm-hmm", "okay", "right", "exactly"])
    def test_backchannels_are_not_new_tasks(self, text):
        intent = self.clf.classify(text)
        assert intent.category is IntentCategory.BACKCHANNEL, intent.as_dict()
        assert not intent.is_actionable

    def test_yes_answers_a_pending_question(self):
        intent = self.clf.classify("yes", pending_question=True)
        assert intent.category is IntentCategory.CONFIRMATION

    def test_correction_beats_confirmation(self):
        # §35 — "actually, staging" must re-plan, not answer yes/no.
        intent = self.clf.classify("actually make it staging", pending_confirmation=True)
        assert intent.category is IntentCategory.CORRECTION

    def test_destructive_low_confidence_demands_a_read_back(self):
        # §36's worked example, verbatim.
        intent = self.clf.classify("delete the production database", stt_confidence=0.62)
        assert intent.is_destructive
        assert intent.needs_confirmation

    def test_clear_local_command_executes_without_a_read_back(self):
        intent = self.clf.classify("open my project and find the failing tests")
        assert intent.category in (IntentCategory.AUTOMATION, IntentCategory.COMMAND)
        assert not intent.needs_confirmation, intent.as_dict()
        assert not intent.needs_clarification

    def test_ambiguous_site_asks_which_one(self):
        intent = self.clf.classify("open code")
        if intent.entities.get("site_alternatives"):
            assert intent.needs_clarification

    def test_telugu_command_is_understood(self):
        intent = self.clf.classify("youtube teruvu")
        assert intent.category in (IntentCategory.AUTOMATION, IntentCategory.COMMAND)
        assert intent.target_site == "youtube"

    def test_empty_input_is_silence(self):
        assert self.clf.classify("   ").category is IntentCategory.SILENCE

    def test_recurrence_routes_to_automation(self):
        intent = self.clf.classify("every morning at 8 open my email")
        assert intent.category is IntentCategory.AUTOMATION
        assert intent.is_recurring

    @pytest.mark.parametrize("text,expected", [
        ("stop", True),                      # bare control word
        ("okay stop", True),                 # control word in head position
        ("stop it now", True),               # short utterance
        ("let's compare stop words", False), # object of a verb, not a command
        ("open my repo and halt nothing", False),
    ])
    def test_control_words_must_be_addressed_to_us(self, text, expected):
        assert self.clf.classify(text).is_control is expected, text

    def test_cancellation_needs_the_same_positional_guard(self):
        assert self.clf.classify("cancel the deployment").category is IntentCategory.CANCELLATION
        # "cancelled" is a different token; and a mid-sentence mention is not a command.
        assert self.clf.classify("delete the cancelled build").category is not \
            IntentCategory.CANCELLATION

    def test_a_plain_question_is_never_read_back(self):
        # §30 gates operations, not conversation. "Just to confirm: what are you
        # doing?" is a worse failure than answering an imperfect transcript.
        intent = self.clf.classify("what are you doing", stt_confidence=0.8)
        assert intent.category is IntentCategory.QUESTION
        assert not intent.needs_confirmation

    def test_a_shaky_destructive_command_is_still_read_back(self):
        # The gate must narrow to conversation only — actions keep their guard.
        intent = self.clf.classify("delete the production database", stt_confidence=0.62)
        assert intent.needs_confirmation


# ── §9/§17/§18: speech policy ──────────────────────────────────────────────
class TestSpeechPolicy:
    def setup_method(self):
        self.policy = SpeechPolicy()
        self.clf = IntentClassifier()
        self.situation = SituationModel(session_id="t")

    def decide(self, text, **kw):
        intent = self.clf.classify(text, **kw)
        return self.policy.decide(intent, self.situation, VoiceState.LISTENING,
                                  task_active=kw.get("task_active", False))

    def test_p6_internal_chatter_is_never_spoken(self):
        from backend.voice_kernel import Utterance
        ok, reason = self.policy.admit(
            Utterance("calling tool read_file", Priority.P6_INTERNAL),
            self.situation, VoiceState.EXECUTING,
        )
        assert not ok
        assert "never spoken" in reason

    def test_ordinary_progress_is_rate_limited(self):
        from backend.voice_kernel import Utterance
        first = Utterance("step one done", Priority.P5_PROGRESS)
        assert self.policy.admit(first, self.situation, VoiceState.EXECUTING)[0]
        self.policy.mark_spoken(first)
        second = Utterance("step two done", Priority.P5_PROGRESS)
        ok, reason = self.policy.admit(second, self.situation, VoiceState.EXECUTING)
        assert not ok
        assert "rate limit" in reason

    def test_a_failure_outranks_the_progress_limit(self):
        from backend.voice_kernel import Utterance
        self.policy.mark_spoken(Utterance("progress", Priority.P5_PROGRESS))
        ok, _ = self.policy.admit(
            Utterance("the build failed", Priority.P3_FAILURE),
            self.situation, VoiceState.EXECUTING,
        )
        assert ok

    def test_quiet_mode_suppresses_progress_but_not_failures(self):
        from backend.voice_kernel import Utterance
        self.situation.mode = ConversationMode.QUIET_MODE
        assert not self.policy.admit(
            Utterance("still working", Priority.P4_KEY_PROGRESS),
            self.situation, VoiceState.EXECUTING,
        )[0]
        assert self.policy.admit(
            Utterance("it failed", Priority.P3_FAILURE),
            self.situation, VoiceState.EXECUTING,
        )[0]

    def test_barge_in_flushes_pending_speech_but_keeps_safety(self):
        from backend.voice_kernel import Utterance
        self.policy.enqueue(Utterance("progress a", Priority.P5_PROGRESS))
        self.policy.enqueue(Utterance("progress b", Priority.P4_KEY_PROGRESS))
        self.policy.enqueue(Utterance("confirm delete?", Priority.P1_SAFETY))
        dropped = self.policy.flush_non_critical()
        assert dropped == 2
        remaining = self.policy.drain(self.situation, VoiceState.LISTENING)
        assert [u.priority for u in remaining] == [Priority.P1_SAFETY]

    def test_backchannel_keeps_the_floor_with_the_user(self):
        decision = self.decide("mm-hmm")
        assert decision.action is ResponseAction.LISTEN

    def test_stop_interrupts_self(self):
        decision = self.decide("stop")
        assert decision.action is ResponseAction.INTERRUPT_SELF
        assert decision.utterance.priority is Priority.P0_EMERGENCY

    def test_thanks_while_a_task_runs_does_not_end_the_conversation(self):
        # §34 — conversation_end != task_end.
        decision = self.decide("okay thanks that's all", task_active=True)
        assert decision.action is not ResponseAction.END_CONVERSATION

    def test_closing_phrase_with_nothing_running_ends_the_conversation(self):
        decision = self.decide("okay we're done, bye")
        assert decision.action is ResponseAction.END_CONVERSATION


# ── §15/§20/§35: task graph ────────────────────────────────────────────────
class TestTaskGraph:
    def build(self):
        g = TaskGraph("ship the fix")
        fix = g.add("fix failing tests")
        build = g.add("build the image", depends_on=[fix.id])
        deploy = g.add("deploy to production", depends_on=[build.id])
        smoke = g.add("smoke test", depends_on=[deploy.id])
        return g, fix, build, deploy, smoke

    def test_dependencies_gate_readiness(self):
        g, fix, build, _, _ = self.build()
        assert [n.id for n in g.ready_nodes()] == [fix.id]
        g.start(fix.id)
        assert g.ready_nodes() == []
        g.complete(fix.id, "ok")
        assert [n.id for n in g.ready_nodes()] == [build.id]

    def test_unknown_dependency_is_rejected(self):
        g = TaskGraph()
        with pytest.raises(KeyError):
            g.add("bad", depends_on=["nope"])

    def test_cancel_keeps_completed_work(self):
        # §14's "cancel the deployment but keep the fixes", exactly.
        g, fix, build, deploy, smoke = self.build()
        g.start(fix.id); g.complete(fix.id, "ok")
        g.start(build.id); g.complete(build.id, "ok")
        cancelled = g.cancel_node(deploy.id, "user changed their mind")
        assert cancelled == [deploy.id]
        assert g.get(fix.id).status is NodeStatus.COMPLETED
        assert g.get(build.id).status is NodeStatus.COMPLETED
        assert g.get(smoke.id).status is NodeStatus.BLOCKED

    def test_correction_replaces_one_node_and_reopens_downstream(self):
        # §35 — production CANCELLED, staging ACTIVE, rest of plan intact.
        g, fix, build, deploy, smoke = self.build()
        g.start(fix.id); g.complete(fix.id, "ok")
        g.start(build.id); g.complete(build.id, "ok")
        new = g.replace(deploy.id, "deploy to staging")
        assert g.get(deploy.id).status is NodeStatus.CANCELLED
        assert g.get(smoke.id).status is NodeStatus.PENDING
        assert new.id in g.get(smoke.id).dependencies
        assert [n.id for n in g.ready_nodes()] == [new.id]

    def test_cancellation_token_fires_its_stoppers(self):
        g, fix, _, _, _ = self.build()
        seen = []
        g.get(fix.id).token.on_cancel(seen.append)
        g.cancel_node(fix.id, "abort")
        assert seen == ["abort"]

    def test_cancel_all_empties_the_ready_queue(self):
        g, *_ = self.build()
        g.cancel_all("stop everything")
        assert g.ready_nodes() == []
        assert g.is_complete

    def test_starting_a_cancelled_node_raises(self):
        from backend.voice_kernel.task_graph import TaskCancelled
        g, fix, *_ = self.build()
        g.cancel_node(fix.id)
        with pytest.raises(TaskCancelled):
            g.start(fix.id)

    def test_failure_blocks_downstream_and_retry_reopens_it(self):
        g, fix, build, _, _ = self.build()
        g.start(fix.id)
        g.fail(fix.id, "2 tests still red")
        assert g.get(build.id).status is NodeStatus.BLOCKED
        g.retry(fix.id)
        assert g.get(fix.id).status is NodeStatus.PENDING
        assert g.get(build.id).status is NodeStatus.PENDING

    def test_retry_is_capped(self):
        g = TaskGraph()
        node = g.add("flaky step")
        for _ in range(node.max_retries):
            g.start(node.id)
            g.fail(node.id, "boom")
            g.retry(node.id)
        g.start(node.id)
        g.fail(node.id, "boom")
        assert not g.get(node.id).can_retry
        with pytest.raises(ValueError):
            g.retry(node.id)

    def test_snapshot_round_trips(self):
        g, fix, *_ = self.build()
        g.start(fix.id); g.complete(fix.id, {"tests": 12})
        restored = TaskGraph.restore(g.snapshot())
        assert len(restored) == len(g)
        assert restored.get(fix.id).result == {"tests": 12}
        assert restored.progress == g.progress


# ── the whole session, end to end ──────────────────────────────────────────
class TestSession:
    """Event in → directives out, across the seams the unit tests can't see.

    Both bugs found while wiring the transport lived *between* components that
    each passed their own tests: RESPONSE_TOKEN was legal at the FSM but shadowed
    by the inert list, and playback tracking disagreed with the FSM about who was
    making sound. Those only show up when a real sequence is replayed.
    """

    def open(self, **config):
        session = VoiceSession(config=SessionConfig(require_wake_word=False, **config))
        session.handle(Event(EventKind.SESSION_OPEN, {}))
        return session

    def feed(self, session, kind, **payload):
        return session.handle(Event(kind, payload))

    def said(self, session, text, *, confidence=0.97, silence=800.0):
        return self.feed(
            session, EventKind.FINAL_TRANSCRIPT,
            text=text, confidence=confidence, silence_ms=silence,
        )

    def system_message(self, directives):
        ctx = payload(directives, DirectiveKind.GENERATE_RESPONSE)["context"]
        return next(m["content"] for m in ctx["messages"] if m["role"] == "system")

    # ── §3/§4 ─────────────────────────────────────────────────────────────
    def test_a_streamed_token_takes_the_floor(self):
        # The shadowed-transition bug: without this the session answers from
        # UNDERSTANDING forever and nothing downstream knows we are talking.
        session = self.open()
        self.said(session, "what is on my screen")
        # A fragment with no sentence boundary: we are composing, not yet talking.
        self.feed(session, EventKind.RESPONSE_TOKEN, token="Looking at")
        assert session.state is VoiceState.RESPONDING
        assert session.fsm.holds_floor
        # Completing the sentence hands a chunk to TTS, so now we are speaking.
        self.feed(session, EventKind.RESPONSE_TOKEN, token=" it now.")
        assert session.state is VoiceState.SPEAKING
        # And directly at the FSM, where the inert list used to shadow the row.
        fsm = VoiceStateMachine(VoiceState.UNDERSTANDING)
        assert fsm.transition(EventKind.RESPONSE_TOKEN) is VoiceState.RESPONDING
        assert VoiceStateMachine(VoiceState.LISTENING).transition(
            EventKind.RESPONSE_TOKEN
        ) is None  # still inert where no row exists

    def test_barge_in_mid_response_stops_playback(self):
        session = self.open()
        self.said(session, "what is on my screen")
        self.feed(session, EventKind.RESPONSE_TOKEN, token="Here is a long answer. ")
        self.feed(session, EventKind.TTS_STARTED, text="Here is a long answer.")
        out = self.feed(session, EventKind.SPEECH_START, source="user")
        stop = payload(out, DirectiveKind.STOP_TTS)
        assert stop is not None, kinds(out)
        assert stop["reason"] == "user barge-in"
        assert stop["spoken_so_far"]
        assert payload(out, DirectiveKind.START_TRANSCRIBING)["barge_in"] is True

    def test_barge_in_over_a_read_back_stops_playback(self):
        # §4 + §36: the safety question is the utterance a user most needs to cut
        # into ("no, staging!"). The FSM stays in CONFIRMING while it plays, so
        # this only works because playback is tracked separately from state.
        session = self.open()
        out = self.said(session, "delete the production database", confidence=0.62)
        assert payload(out, DirectiveKind.REQUEST_CONFIRMATION) is not None
        assert session.state is VoiceState.CONFIRMING
        interrupted = self.feed(session, EventKind.SPEECH_START, source="user")
        assert payload(interrupted, DirectiveKind.STOP_TTS) is not None, kinds(interrupted)

    def test_our_own_echo_is_never_a_barge_in(self):
        # §5 — the mic hears the speaker. That is not the user cutting in.
        session = self.open()
        self.said(session, "what is on my screen")
        self.feed(session, EventKind.RESPONSE_TOKEN, token="Answering. ")
        out = self.feed(session, EventKind.SPEECH_START, source="assistant")
        assert kinds(out) == [DirectiveKind.NOOP]
        assert session.fsm.holds_floor

    # ── §36/§30 ───────────────────────────────────────────────────────────
    def test_a_shaky_destructive_command_executes_the_read_back_not_the_yes(self):
        session = self.open()
        self.said(session, "delete the production database", confidence=0.62)
        out = self.said(session, "yes")
        plan = payload(out, DirectiveKind.EXECUTE_PLAN)
        assert plan is not None, kinds(out)
        assert plan["description"] == "delete the production database"
        assert session.state is VoiceState.EXECUTING

    # ── §35 ───────────────────────────────────────────────────────────────
    def test_a_correction_amends_the_plan_instead_of_restarting(self):
        session = self.open()
        self.said(session, "open my project and run the failing tests")
        original = next(iter(session.graph.iter_nodes())).id
        out = self.said(session, "actually run the linter instead")
        assert payload(out, DirectiveKind.CANCEL_EXECUTION)["nodes"] == [original]
        assert payload(out, DirectiveKind.EXECUTE_PLAN)["replaces"] == original
        assert session.graph.get(original).status is NodeStatus.CANCELLED
        assert [n.status for n in session.graph.iter_nodes()].count(NodeStatus.PENDING) == 1

    # ── §20 ───────────────────────────────────────────────────────────────
    def test_stop_arriving_as_a_partial_still_cancels_the_work(self):
        # The user says "stop" and stops talking; no recogniser-final arrives.
        session = self.open()
        self.said(session, "open my project and run the failing tests")
        running = [n.id for n in session.graph.iter_nodes()]
        out = self.feed(session, EventKind.PARTIAL_TRANSCRIPT, text="stop", silence_ms=300)
        assert payload(out, DirectiveKind.STOP_TTS) is not None, kinds(out)
        assert payload(out, DirectiveKind.CANCEL_EXECUTION)["nodes"] == running
        ack = payload(out, DirectiveKind.SPEAK)
        assert ack["text"] == "Stopped."
        assert [d for d in out if d.kind is DirectiveKind.SPEAK][0].priority == int(
            Priority.P0_EMERGENCY
        )

    # ── §14 ───────────────────────────────────────────────────────────────
    def test_asking_what_are_you_doing_does_not_disturb_the_task(self):
        session = self.open()
        self.said(session, "open my project and run the failing tests")
        node = next(iter(session.graph.iter_nodes()))
        out = self.said(session, "what are you doing", confidence=0.8)
        assert payload(out, DirectiveKind.GENERATE_RESPONSE) is not None, kinds(out)
        assert "CURRENT TASK" in self.system_message(out)
        assert node.description in self.system_message(out)
        # The question is a question, not a re-plan and not a read-back.
        assert payload(out, DirectiveKind.REQUEST_CONFIRMATION) is None
        assert session.graph.get(node.id).status is NodeStatus.PENDING

    # ── §11/§29 ───────────────────────────────────────────────────────────
    def test_context_indicator_is_rendered_from_the_real_window(self):
        session = self.open()
        out = self.said(session, "what is on my screen")
        ctx = payload(out, DirectiveKind.GENERATE_RESPONSE)["context"]
        assert 0.0 <= ctx["fill"] <= 1.0
        assert ctx["window"] > 0
        assert len(ctx["indicator"]) > 0

    # ── §12/§37: the session is the unit of continuity ────────────────────
    def test_the_registry_hands_back_the_same_session(self):
        registry = get_registry()
        for session_id in list(registry.ids()):
            registry.drop(session_id)
        first = registry.get_or_create(
            "continuity", config=SessionConfig(require_wake_word=False)
        )
        first.handle(Event(EventKind.SESSION_OPEN, {}))
        first.handle(Event(EventKind.FINAL_TRANSCRIPT, {
            "text": "open my project and run the failing tests",
            "confidence": 0.97, "silence_ms": 800,
        }))
        again = registry.get_or_create("continuity")
        assert again is first
        assert again.resume_plan()["outstanding"]
        registry.drop("continuity")






