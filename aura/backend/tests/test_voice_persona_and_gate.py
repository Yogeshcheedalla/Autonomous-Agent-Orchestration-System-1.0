"""
Regression tests for audit items 18, 19, 20 and the model cooldown.
==================================================================

These four defects share a shape: each was a *missing* piece rather than a wrong
one, and each was invisible from the inside. Nothing threw, no test failed, and
the code read correctly at every individual site.

  * item 18 — nothing sanitised model scaffolding before it reached TTS, so
    `<|tool_call_start|>` and 1700 characters of chain-of-thought were spoken
    aloud verbatim.
  * item 19 — `SessionConfig.system_prompt` defaulted to `""` and nothing ever
    set it, so the only system content a voice model saw was the `key=value`
    situation line, and it answered in that format. See `voice_kernel/persona.py`.
  * item 20 — the §33 wake-word gate returned before `context.add_turn`, so the
    first utterance of every session was discarded with no reply, no context and
    no trace.
  * the cooldown — both model cascades led with a route that could only 402 on
    this account, paying a dead round trip in front of every first spoken word.

The tests are written against the observable consequence, not the mechanism, so
they keep working if the mechanism is rewritten: "does the persona reach the
model", "is the turn recorded", "does the second call skip the dead route".
"""

from __future__ import annotations

import pytest

from backend.model_output import StreamSanitizer, polish_for_speech, sanitize
from backend.voice_kernel import (
    ConversationMode,
    DirectiveKind,
    Event,
    EventKind,
    VoiceSession,
)
from backend.voice_kernel.persona import CONTEXT_PREAMBLE, VOICE_SYSTEM_PROMPT
from backend.voice_kernel.session import SessionConfig


def settle(session: VoiceSession, text: str, confidence: float = 1.0, silence_ms: float = 900.0):
    """Push one complete utterance through and return every directive it caused.

    `silence_ms` is passed explicitly because the transport normally supplies it
    from the endpointing heartbeat, and a helper that omits it accumulates none —
    which leaves §6 legitimately holding short utterances open. Without this, a
    test about the wake-word gate silently becomes a test about word count: a
    nine-word phrase settles at zero silence and a five-word one does not.
    """
    out = list(session.handle(Event(EventKind.SPEECH_START, {})))
    out += session.handle(Event(EventKind.PARTIAL_TRANSCRIPT, {"text": text}))
    out += session.handle(Event(
        EventKind.FINAL_TRANSCRIPT,
        {"text": text, "confidence": confidence, "is_final": True, "silence_ms": silence_ms},
    ))
    return out


def system_message(session: VoiceSession) -> str:
    return session.build_prompt()["context"]["messages"][0]["content"]


# ── item 19: the persona ─────────────────────────────────────────────────────
class TestVoicePersona:
    """§2/§19/§31 — a voice model must be told it is speaking."""

    def test_default_config_carries_the_persona(self):
        # The whole defect was a default of "". Anything that makes this pass by
        # accident (a caller that happens to set it) misses the point.
        assert SessionConfig().system_prompt == VOICE_SYSTEM_PROMPT

    def test_persona_reaches_the_assembled_system_message(self):
        assert "You are Akansha" in system_message(VoiceSession("p1"))

    def test_persona_leads_the_system_message(self):
        # Position matters: the state block used to be the *only* content, which
        # is why it became the answer's template.
        assert system_message(VoiceSession("p2")).startswith("You are Akansha")

    def test_state_block_is_labelled_as_state(self):
        session = VoiceSession("p3")
        built = session.context.build(
            situation_line=session._situation_line(), task_line="index the repo"
        )
        message = built["messages"][0]["content"]
        assert CONTEXT_PREAMBLE in message
        assert message.index(CONTEXT_PREAMBLE) < message.index("CURRENT SITUATION")

    def test_task_line_still_reaches_the_model(self):
        # §14 depends on this exact location; a probe reading `payload["system"]`
        # instead of the first message once produced a false negative.
        session = VoiceSession("p4")
        built = session.context.build(task_line="reorganise the downloads folder")
        assert "CURRENT TASK: reorganise the downloads folder" in built["messages"][0]["content"]

    def test_generate_response_carries_the_persona(self):
        session = VoiceSession("p5")
        directives = settle(session, "what is the capital of France?")
        generate = [d for d in directives if d.kind is DirectiveKind.GENERATE_RESPONSE]
        assert generate, [d.kind.value for d in directives]
        messages = generate[0].payload["context"]["messages"]
        assert "You are Akansha" in messages[0]["content"]
        assert any(m["role"] == "user" and "capital of France" in m["content"] for m in messages)

    def test_explicitly_empty_prompt_is_still_honoured(self):
        # Defaulting must not become "impossible to opt out of".
        session = VoiceSession("p6", config=SessionConfig(system_prompt=""))
        assert not system_message(session).startswith("You are Akansha")

    def test_resume_does_not_lose_the_persona(self):
        # §37 — a snapshot written before the persona existed carries "", and a
        # restart must not quietly restore the old behaviour.
        snapshot = VoiceSession("p7").snapshot()
        snapshot["config"]["system_prompt"] = ""
        snapshot["context"]["system_prompt"] = ""
        assert "You are Akansha" in system_message(VoiceSession.restore(snapshot))

    def test_persona_forbids_the_formats_tts_cannot_speak(self):
        # Not a style assertion: each of these was measured reaching the voice.
        for banned in ("markdown", "bullet", "emoji", "code block"):
            assert banned in VOICE_SYSTEM_PROMPT.lower(), banned


# ── item 20: the wake-word gate ──────────────────────────────────────────────
class TestWakeWordGate:
    """§33 — the wake word is *optional*, and a gate must never be silent."""

    def test_cold_session_first_utterance_is_answered(self):
        session = VoiceSession("w1")
        directives = settle(session, "what is the capital of France?")
        assert not any(d.payload.get("needs_wake_word") for d in directives)
        assert any(d.kind is DirectiveKind.GENERATE_RESPONSE for d in directives)

    def test_cold_session_first_utterance_reaches_context(self):
        # The early return preceded `add_turn`, so the context meter read 0% after
        # two turns — the symptom that exposed this.
        session = VoiceSession("w2")
        settle(session, "remember my project folder is called nightingale")
        assert len(session.context.turns) >= 1
        assert session.context.build()["fill"] > 0.0

    def test_ambient_mode_still_gates(self):
        # The gate has a real job: an always-on mic hears speech meant for someone
        # else, and turning that into tasks is worse than ignoring it.
        session = VoiceSession("w3", config=SessionConfig(mode=ConversationMode.NORMAL))
        directives = settle(session, "so then I told him the deploy was fine")
        assert any(d.payload.get("needs_wake_word") for d in directives)
        assert not any(d.kind is DirectiveKind.GENERATE_RESPONSE for d in directives)

    def test_gated_speech_stays_out_of_memory(self):
        session = VoiceSession("w4", config=SessionConfig(mode=ConversationMode.NORMAL))
        settle(session, "so then I told him the deploy was fine")
        assert session.context.turns == []

    def test_gated_speech_is_counted_not_discarded(self):
        session = VoiceSession("w5", config=SessionConfig(mode=ConversationMode.NORMAL))
        # Terminal punctuation so §6 settles the turn: the subject here is the
        # gate, and an utterance the endpointer is still holding open has not
        # reached it yet.
        settle(session, "unrelated chatter in the room.")
        assert session.situation.unaddressed_count == 1
        assert session.situation.last_unaddressed == "unrelated chatter in the room."

    def test_wake_word_opens_the_gate(self):
        session = VoiceSession("w6", config=SessionConfig(mode=ConversationMode.NORMAL))
        directives = settle(session, "akansha what time is it")
        assert not any(d.payload.get("needs_wake_word") for d in directives)

    @pytest.mark.parametrize("phrase", ["stop", "cancel that", "stop talking"])
    def test_control_phrases_are_never_gated(self, phrase):
        # §4/§20 — the gate used to run first, so "stop" while the assistant was
        # mid-sentence was dropped for lacking a wake word it makes no sense to
        # demand of someone interrupting.
        session = VoiceSession(f"w7-{phrase}", config=SessionConfig(mode=ConversationMode.NORMAL))
        kinds = [d.kind for d in settle(session, phrase)]
        assert DirectiveKind.STOP_TTS in kinds
        assert DirectiveKind.CANCEL_EXECUTION in kinds

    def test_explicit_wake_word_requirement_is_still_honoured(self):
        session = VoiceSession("w8", config=SessionConfig(require_wake_word=True))
        assert any(d.payload.get("needs_wake_word") for d in settle(session, "what time is it"))

    def test_explicit_opt_out_is_still_honoured(self):
        session = VoiceSession(
            "w9",
            config=SessionConfig(mode=ConversationMode.NORMAL, require_wake_word=False),
        )
        assert not any(d.payload.get("needs_wake_word") for d in settle(session, "what time is it"))

    def test_control_exemption_did_not_reopen_the_substring_bug(self):
        # Item 3 — "i am waiting for the build" must not read as "wait"/"stop".
        session = VoiceSession("w10", config=SessionConfig(mode=ConversationMode.NORMAL))
        kinds = [d.kind for d in settle(session, "i am waiting for the build to finish")]
        assert DirectiveKind.STOP_TTS not in kinds


# ── item 18: the sanitiser ───────────────────────────────────────────────────
class TestModelOutputSanitiser:
    """Verified ad hoc during the audit; pinned here so it stays verified."""

    CHUNK_SIZES = (1, 3, 7, 1000)

    def feed_in_chunks(self, text: str, size: int, *, for_speech: bool = True) -> str:
        sanitizer = StreamSanitizer(for_speech=for_speech)
        out = "".join(sanitizer.feed(text[i:i + size]) for i in range(0, len(text), size))
        return out + sanitizer.finish()

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_tool_call_markers_are_stripped_across_chunk_boundaries(self, size):
        # `<|tool_call_start|>` is 21 characters and arrives split. This is the
        # exact string `liquid` was measured emitting.
        raw = "Sure.<|tool_call_start|>[bash(command='ls -la')]<|tool_call_end|> Done."
        assert "tool_call" not in self.feed_in_chunks(raw, size)

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_reasoning_blocks_are_stripped(self, size):
        raw = "<think>the user wants X, so I should Y</think>The answer is four."
        cleaned = self.feed_in_chunks(raw, size)
        assert "the user wants X" not in cleaned
        assert "four" in cleaned

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_unterminated_reasoning_yields_nothing_speakable(self, size):
        # A model whose entire reply was an unterminated dump has told us nothing,
        # and `_stream_into` depends on this being empty to try the next candidate.
        assert self.feed_in_chunks("<think>an unterminated dump", size).strip() == ""

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_comparison_operators_are_not_mistaken_for_markers(self, size):
        # The false-positive guard: this is ordinary prose, not scaffolding.
        raw = "Check whether A < B and C > D before continuing."
        assert self.feed_in_chunks(raw, size) == raw

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_plain_prose_survives_every_chunking(self, size):
        raw = "Paris. It is in the north-central part of the country."
        assert self.feed_in_chunks(raw, size) == raw

    def test_chunking_is_invariant(self):
        # Streaming must not change *what* is said, only when.
        raw = "Okay.<think>hmm</think> The build passed in about ninety seconds."
        results = {self.feed_in_chunks(raw, size) for size in self.CHUNK_SIZES}
        assert len(results) == 1, results

    def test_markdown_is_flattened_for_speech(self):
        # Asserted against the whole pipeline, because that is what reaches TTS.
        # The two layers are complementary and neither is sufficient alone:
        # `StreamSanitizer` drops token-level markers (`**`, backticks, `#`) as
        # they arrive, while link and bullet syntax needs lookahead over a whole
        # line and so waits for `polish_for_speech`.
        spoken = sanitize("**Important:** see [the docs](https://example.com/a/b)", for_speech=True)
        assert spoken == "Important: see the docs"

    def test_the_two_speech_layers_each_do_their_own_half(self):
        # Pinned because asserting bold-stripping against `polish_for_speech`
        # alone looks like a sanitiser gap and is really a layering mistake.
        raw = "**Bold** and [a link](https://example.com)"
        streamed = StreamSanitizer(for_speech=True)
        stream_only = streamed.feed(raw) + streamed.finish()
        assert "**" not in stream_only and "https://" in stream_only
        polish_only = polish_for_speech(raw)
        assert "https://" not in polish_only and "**" in polish_only

    def test_sanitize_matches_the_streaming_path(self):
        raw = "Hello<think>internal</think> there."
        assert sanitize(raw, for_speech=True).strip() == self.feed_in_chunks(raw, 1000).strip()


    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_harmony_analysis_channel_is_stripped(self, size):
        # gpt-oss routes put reasoning in a `<|channel|>analysis` block closed by
        # `<|message|>` rather than by a matching tag. Covered because it is the
        # one block whose closer is not the mirror of its opener, so an
        # implementation that pairs tags by name silently passes it through.
        raw = "<|channel|>analysis I should check the docs first<|message|>The build passed."
        cleaned = self.feed_in_chunks(raw, size)
        assert "I should check the docs" not in cleaned
        assert "The build passed." in cleaned

    @pytest.mark.parametrize("size", CHUNK_SIZES)
    def test_chat_template_residue_is_dropped(self, size):
        # Bare template tokens some routes forward verbatim. Unlike the blocks
        # above, the text around them must survive -- they are deletions, not
        # boundaries, and treating one as a block opener would eat the reply.
        raw = "<|im_start|>Ready when you are.<|im_end|>"
        cleaned = self.feed_in_chunks(raw, size)
        assert "im_start" not in cleaned and "im_end" not in cleaned
        assert "Ready when you are." in cleaned

    def test_speech_only_drops_leave_the_text_path_alone(self):
        # `for_speech` is a flag rather than the default: the chat UI renders
        # markdown, so stripping it unconditionally would degrade the text path to
        # fix the voice one.
        raw = "**Important:** run `npm test`"
        assert "**" in sanitize(raw, for_speech=False)
        assert "**" not in sanitize(raw, for_speech=True)


# ── items 16, 17, 18: the cascade configuration itself ───────────────────────
class TestVoiceModelCascade:
    """The three model-configuration defects, pinned as invariants.

    These were marked "fixed" in the audit ledger with no regression test, and
    they are the kind that come back silently: nothing throws when a cascade
    degenerates, the symptom is just that every spoken reply becomes the canned
    provider-failure fallback.
    """

    def test_voice_cascade_is_not_a_list_of_one(self):
        # Item 17. The defect was literally `[OPENROUTER_MODEL]` -- a one-entry
        # cascade, so a single dead route silenced every spoken reply while the
        # text path fell through to a working model and looked fine.
        from backend import ai_engine

        assert len(ai_engine._voice_model_candidates()) > 1

    def test_voice_cascade_has_more_than_one_free_route(self):
        # Item 16. All five fallbacks were dead at once (3x 402, 2x 404), which is
        # what a cascade of paid routes on a zero-balance key degenerates into. At
        # least two entries must be free, so exhausted credit cannot mute voice.
        from backend import ai_engine

        free = [m for m in ai_engine.OPENROUTER_FALLBACK_MODELS if m.endswith(":free")]
        assert len(free) >= 2, ai_engine.OPENROUTER_FALLBACK_MODELS

    def test_every_voice_model_is_free(self):
        # Voice is the latency-critical path and retries are per-utterance, so a
        # paid route here is both a bill and a 402 the user hears as silence.
        from backend import ai_engine

        paid = [m for m in ai_engine.VOICE_MODELS if not m.endswith(":free")]
        assert paid == [], paid

    def test_markerless_reasoning_dumpers_are_excluded_from_voice(self):
        # Item 18's other half, and the reason it is a config test rather than a
        # sanitiser test: `nemotron-3.5-lightning` was measured emitting 1721
        # characters starting "Here's a thinking process:" with *no* markers at
        # all. `model_output` deliberately does not guess at unmarked prose --
        # there is no way to tell that from a real answer about thinking
        # processes -- so the only defence is keeping the model off the voice
        # list. If it ever reappears there, Akansha reads her notes aloud.
        from backend import ai_engine

        assert "nvidia/nemotron-3.5-lightning:free" not in ai_engine.VOICE_MODELS
        # It stays in the *text* cascade on purpose: unmarked reasoning is ugly
        # on screen, not spoken, and it is a working free route.
        assert "nvidia/nemotron-3.5-lightning:free" in ai_engine.OPENROUTER_FALLBACK_MODELS

    def test_routes_that_never_reach_an_answer_are_excluded_everywhere(self):
        # The sibling of the test above, and the more dangerous shape: these
        # models do not emit reasoning *instead of* an answer where a sanitiser
        # could see it -- they emit nothing at all. Measured at the 200-token
        # ceiling a real reply uses, `liquid/lfm-2.5-2.6b:free` returned HTTP 200
        # with 0 characters of `delta.content` and 934 of `delta.reasoning`; it
        # spends the whole budget thinking and stops. The streaming loop scores
        # an empty reply as a failure and moves on, which is correct, so the cost
        # is a wasted round trip per attempt rather than a wrong answer.
        #
        # It is excluded from *both* lists, not just voice, because unlike the
        # nemotron above there is no context where an empty string is useful.
        # And it cannot be repaired from the request side: `{"enabled": false}`
        # is refused with "400 Reasoning is mandatory for this endpoint",
        # `{"exclude": true}` only hides the thinking (0 content, 0 reasoning),
        # and `{"effort": "low"}` still produced 975 characters of reasoning and
        # no content.
        from backend import ai_engine

        for dead in ("liquid/lfm-2.5-2.6b:free", "inclusionai/ling-3.0-flash-fin:free"):
            assert dead not in ai_engine.VOICE_MODELS, dead
            assert dead not in ai_engine.OPENROUTER_FALLBACK_MODELS, dead

    def test_the_voice_list_is_judged_on_the_real_prompt_not_a_toy_one(self):
        # This test exists because the obvious way to vet a route gives the wrong
        # answer. Asked "what is a compiler?" with no system prompt,
        # `nvidia/nemotron-3-super-120b-a12b:free` looked like the best route
        # available: 1.22 s to first token, 147 characters of clean content, and
        # its 394 characters of thinking properly separated into
        # `delta.reasoning`. On that evidence it was made the default.
        #
        # Run through `generate_chat_stream` -- Akansha's real system prompt and
        # history -- the same route put 1029 characters of "Okay, the user is
        # asking for a one-sentence explanation..." into the *content* channel
        # and was truncated mid-thought at "Ah!". A toy prompt does not measure
        # the thing that ships.
        #
        # Both nemotron endpoints behave this way, so the vendor is pinned rather
        # than the two ids: a new nemotron route is guilty until
        # `scripts/probe_model_routes.py` says otherwise.
        from backend import ai_engine

        leaks_reasoning_as_prose = [m for m in ai_engine.VOICE_MODELS if "nemotron" in m]
        assert leaks_reasoning_as_prose == [], leaks_reasoning_as_prose

    def test_the_retired_free_route_is_gone_rather_than_demoted(self):
        # `nvidia/nemotron-3-nano-30b-a3b:free` was `DEFAULT_OPENROUTER_MODEL`,
        # so it was entry one of both cascades. It now answers "404 - This model
        # is unavailable for free. The paid version is available now" and is
        # absent from the free routes OpenRouter advertises.
        #
        # The cooldown cannot cover this. It demotes a route only *after* it has
        # failed once in the current process, so a retired head-of-cascade costs
        # one guaranteed wasted request per backend start, forever. A route that
        # has been withdrawn has to be deleted, not demoted.
        from backend import ai_engine

        retired = "nvidia/nemotron-3-nano-30b-a3b:free"
        assert retired != ai_engine.DEFAULT_OPENROUTER_MODEL
        assert retired not in ai_engine.VOICE_MODELS
        assert retired not in ai_engine.OPENROUTER_FALLBACK_MODELS

    def test_voice_candidates_are_deduplicated(self):
        # `_voice_model_candidates` merges OPENROUTER_MODEL with VOICE_MODELS, and
        # the common case is that the env names a model already in the list.
        # Retrying the same dead route twice per utterance is the failure this
        # prevents.
        from backend import ai_engine

        candidates = ai_engine._voice_model_candidates()
        assert len(candidates) == len(set(candidates)), candidates

    def test_default_model_is_concrete(self):
        # `openrouter/auto` silently selects paid routes, which is how a
        # zero-balance key ends up spending a 402 per request.
        from backend import ai_engine

        assert ai_engine.DEFAULT_OPENROUTER_MODEL.lower() not in {
            "openrouter/auto",
            "auto",
            "/auto",
            "",
        }
        assert "/" in ai_engine.DEFAULT_OPENROUTER_MODEL


# ── the model cooldown ───────────────────────────────────────────────────────
class TestModelCooldown:
    """A dead route must cost one round trip, not one per utterance."""

    @pytest.fixture(autouse=True)
    def clean_cooldown(self):
        from backend import ai_engine

        saved = dict(ai_engine._MODEL_COOLDOWN)
        ai_engine._MODEL_COOLDOWN.clear()
        yield ai_engine
        ai_engine._MODEL_COOLDOWN.clear()
        ai_engine._MODEL_COOLDOWN.update(saved)

    @pytest.mark.parametrize("message,kind", [
        ("Error code: 402 - This request requires more credits", "capacity"),
        ("Error code: 404 - No endpoints found matching your data policy", "unavailable"),
        ("model is unavailable for free", "unavailable"),
        ("Error code: 401 - authentication failed", "auth"),
        ("request timed out", "timeout"),
        ("rate limit exceeded", "rate_limit"),
        ("502 bad gateway", "provider"),
    ])
    def test_failure_kinds(self, clean_cooldown, message, kind):
        assert clean_cooldown._provider_failure_kind(RuntimeError(message)) == kind

    def test_structural_failure_demotes(self, clean_cooldown):
        first = clean_cooldown._voice_model_candidates()[0]
        assert clean_cooldown.note_model_failure(first, RuntimeError("402 insufficient credits"))
        assert first not in clean_cooldown._voice_model_candidates()

    def test_transient_failure_does_not_demote(self, clean_cooldown):
        # Demoting on network noise would reshuffle the cascade for no reason.
        before = clean_cooldown._voice_model_candidates()
        assert not clean_cooldown.note_model_failure(before[0], RuntimeError("request timed out"))
        assert clean_cooldown._voice_model_candidates() == before

    def test_text_path_keeps_dead_routes_at_the_back(self, clean_cooldown):
        # An extra attempt is cheap when nobody is listening to silence, and a
        # stale cooldown must never be why an answer failed.
        first = clean_cooldown._openrouter_model_candidates()[0]
        clean_cooldown.note_model_failure(first, RuntimeError("402 insufficient credits"))
        after = clean_cooldown._openrouter_model_candidates()
        assert after[-1] == first
        assert set(after) == set(clean_cooldown._openrouter_model_candidates())

    def test_nothing_is_dropped_when_everything_is_cooling(self, clean_cooldown):
        before = clean_cooldown._voice_model_candidates()
        for model in before:
            clean_cooldown.note_model_failure(model, RuntimeError("402 insufficient credits"))
        assert clean_cooldown._voice_model_candidates() == before

    def test_cooldown_expires(self, clean_cooldown):
        first = clean_cooldown._voice_model_candidates()[0]
        clean_cooldown.note_model_failure(first, RuntimeError("402 insufficient credits"))
        assert first not in clean_cooldown._voice_model_candidates()
        # A top-up must heal without a code change, so expiry is what restores it.
        clean_cooldown._MODEL_COOLDOWN[first] = 0.0
        assert clean_cooldown._voice_model_candidates()[0] == first

    def test_blank_model_is_ignored(self, clean_cooldown):
        assert not clean_cooldown.note_model_failure("", RuntimeError("402"))
        assert not clean_cooldown.note_model_failure(None, RuntimeError("402"))
