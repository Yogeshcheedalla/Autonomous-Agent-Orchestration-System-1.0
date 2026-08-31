import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import automation
from backend import ai_engine as ai_engine_module
from backend import voice_dialog
from backend.ai_engine import (
    _build_direct_live_data_context,
    _build_live_answer_hint,
    _build_multi_question_live_context,
    _build_output_intent_context,
    _build_live_search_query,
    _CASCADE_BUDGET_VOICE_S,
    _direct_source_backed_answer,
    _detect_output_formats,
    _detect_emotional_state,
    _detect_user_language_preference,
    _extract_relationship_name_fact,
    _extract_matchups_from_live_context,
    _FALLBACK_LIVE_BUDGET_S,
    _fetch_ipl_standings_context,
    _fetch_news_direct_context,
    _humor_policy,
    _language_instruction,
    _live_lookup_acknowledgement,
    _local_attachment_question_answer,
    _needs_structured_table,
    _parse_ipl_standings_rows,
    _preferred_live_source_profile,
    _PRE_MODEL_LIVE_BUDGET_TEXT_S,
    _PRE_MODEL_LIVE_BUDGET_VOICE_S,
    _fast_local_reply_for_provider_failure,
    _provider_failure_fallback,
    _split_live_questions,
    _needs_live_web_context,
    _response_token_limit,
    _should_skip_ai_memory_analysis,
    _should_use_fast_local_reply,
    build_social_intelligence_context,
)
from backend.artifact_engine import (
    artifact_markdown,
    create_requested_artifacts,
    requested_artifact_formats,
    sanitize_model_artifact_placeholders,
)
from backend.main import (
    _normalize_whatsapp_allowed_contact,
    _speaker_access_level,
    build_browser_prompt_plan,
    extract_send_message_details,
    hash_password,
    is_broken_assistant_response,
    normalize_auth_email,
    verify_password,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def compact_source(text: str) -> str:
    """Collapse whitespace runs so an assertion survives formatter reflow.

    Several assertions here pin a multi-token expression as one exact line. That
    made them fail the moment prettier re-wrapped the expression across lines,
    reporting a regression where the behaviour was unchanged -- a false alarm
    that costs more than the coverage is worth. Comparing against the compacted
    text keeps the assertion about the *code* instead of about its line breaks.
    """

    return re.sub(r"\s+", " ", text)


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeElement:
    def __init__(self, text: str, rect: FakeRect, control_type: str = "Text"):
        self._text = text
        self._rect = rect
        self.element_info = SimpleNamespace(control_type=control_type, name=text)

    def window_text(self):
        return self._text

    def rectangle(self):
        return self._rect


class AuditRegressionTests(unittest.TestCase):
    def test_whatsapp_parser_allows_only_amma_aliases(self):
        self.assertEqual(
            extract_send_message_details("open whatsapp desktop and send hi to Amma"),
            ("hi", "Amma"),
        )
        self.assertEqual(
            extract_send_message_details("send good morning message to mummy on whatsapp"),
            ("good morning", "mummy"),
        )
        self.assertEqual(_normalize_whatsapp_allowed_contact("mummy on whatsapp desktop"), "Amma")
        self.assertIsNone(_normalize_whatsapp_allowed_contact("Amma Home"))

    def test_whatsapp_plan_rejects_unsafe_contact(self):
        allowed = build_browser_prompt_plan("open whatsapp desktop and send hi to Amma")
        self.assertEqual(
            allowed["steps"][-1]["payload"],
            {"contact": "Amma", "message": "hi"},
        )

        rejected = build_browser_prompt_plan("open whatsapp desktop and send hi to Amma Home")
        self.assertTrue(rejected["needs_clarification"])
        self.assertEqual(rejected["steps"], [])

    def test_whatsapp_ui_matching_requires_exact_visible_contact(self):
        window = object()
        elements = [
            FakeElement("Amma Home", FakeRect(30, 260, 260, 305), "ListItem"),
            FakeElement("Amma", FakeRect(30, 320, 260, 365), "ListItem"),
        ]

        with (
            patch.object(automation, "_iter_descendants", return_value=elements),
            patch.object(automation, "_get_window_bounds", return_value=(0, 0, 1000, 800)),
        ):
            match = automation._find_whatsapp_contact_element(window, ["Amma"])
            self.assertIsNotNone(match)
            self.assertEqual(match.window_text(), "Amma")

    def test_whatsapp_chat_header_requires_exact_contact(self):
        window = object()
        wrong_header = [FakeElement("Amma Home", FakeRect(360, 35, 520, 70), "Text")]
        right_header = [FakeElement("Amma", FakeRect(360, 35, 520, 70), "Text")]

        with (
            patch.object(automation, "_iter_descendants", return_value=wrong_header),
            patch.object(automation, "_get_window_bounds", return_value=(0, 0, 1000, 800)),
        ):
            self.assertFalse(automation._is_whatsapp_chat_open(window, ["Amma"]))

        with (
            patch.object(automation, "_iter_descendants", return_value=right_header),
            patch.object(automation, "_get_window_bounds", return_value=(0, 0, 1000, 800)),
        ):
            self.assertTrue(automation._is_whatsapp_chat_open(window, ["Amma"]))

    def test_speaker_relationship_access_levels(self):
        self.assertEqual(_speaker_access_level("amma"), "trusted")
        self.assertEqual(_speaker_access_level("mother"), "trusted")
        self.assertEqual(_speaker_access_level("owner"), "owner")
        self.assertEqual(_speaker_access_level("guest"), "guest")
        self.assertEqual(_speaker_access_level("unknown"), "guest")

    def test_social_intelligence_context_adapts_mother_relationship(self):
        context = build_social_intelligence_context(
            {
                "display_name": "Amma",
                "relationship_to_owner": "mother",
                "access_level": "trusted",
                "closeness_level": "close",
                "language_preference": "telugu_english",
                "notes": "Yogesh's mother",
            },
            "I am worried about his food",
            "stressed",
        )

        self.assertIn("Active speaker: Amma", context)
        self.assertIn("Relationship to owner the owner: mother", context)
        self.assertNotIn("Relationship to owner Yogesh", context)
        self.assertIn("Closeness level: close", context)
        self.assertIn("food, health, rest", context)
        self.assertIn("Telugu + English", context)
        self.assertIn("Mood state: stressed", context)
        self.assertIn("protected actions need owner approval", context)

    def test_social_intelligence_context_defaults_to_owner_for_chat_session(self):
        context = build_social_intelligence_context(None, "continue my project work", None)

        self.assertIn("Active speaker: the owner", context)
        self.assertIn("Relationship to owner the owner: owner", context)
        self.assertNotIn("Active speaker: Yogesh", context)
        self.assertIn("Closeness level: close", context)
        self.assertIn("personal assistant plus close companion", context)

    def test_relationship_name_updates_are_memory_facts_not_static_replies(self):
        text = "my mother name is usha rani"

        self.assertFalse(_should_use_fast_local_reply(text))
        self.assertEqual(_extract_relationship_name_fact(text), ("mother", "Usha Rani"))
        self.assertTrue(
            _should_skip_ai_memory_analysis(
                text,
                "Got it. I'll remember your mother's name is Usha Rani.",
            )
        )

    def test_emotional_state_detection_from_text(self):
        self.assertEqual(_detect_emotional_state("I am very tired today"), "tired")
        self.assertEqual(_detect_emotional_state("this exam pressure is stressful"), "stressed")
        self.assertEqual(_detect_emotional_state("awesome super excited"), "excited")

    def test_friend_humor_depends_on_closeness_and_mood(self):
        close_friend = build_social_intelligence_context(
            {
                "display_name": "Rahul",
                "relationship_to_owner": "friend",
                "access_level": "trusted",
                "closeness_level": "close",
                "communication_style": "college banter",
                "language_preference": "hinglish",
                "interaction_count": 31,
            },
            "bro I finally finished the assignment",
            "happy",
        )

        self.assertIn("playful teasing is allowed", close_friend)
        self.assertIn("Hinglish", close_friend)
        self.assertIn("college-style banter", close_friend)

        stressed_friend_policy = _humor_policy("friend", "close", "stressed")
        self.assertIn("Humor: off", stressed_friend_policy)

    def test_social_context_includes_recent_speaker_history_subtly(self):
        context = build_social_intelligence_context(
            {
                "display_name": "Rahul",
                "relationship_to_owner": "friend",
                "access_level": "trusted",
                "closeness_level": "normal",
                "recent_interactions": [
                    {"role": "user", "content": "My lab exam is tomorrow", "mood_state": "stressed"},
                    {"role": "assistant", "content": "Let's revise the key parts calmly.", "mood_state": "stressed"},
                ],
            },
            "hey",
            "neutral",
        )

        self.assertIn("Recent per-speaker interaction history", context)
        self.assertIn("My lab exam is tomorrow", context)
        self.assertIn("Use recent per-speaker history subtly", context)

    def test_language_detection_handles_ten_telugu_inputs(self):
        telugu_cases = [
            "ఏమైంది రా ఈరోజు silent గా ఉన్నావ్",
            "నాకు ఈ topic explain cheppu",
            "ఇప్పుడు class lo emi jarigindi",
            "సరే anna project chudu",
            "ఎక్కడ issue undi cheppandi",
            "em chestunnav ra",
            "naku exam tension undi cheppu",
            "ippudu ela prepare avvali",
            "sare inka next task cheppu",
            "assignment lo emi mistake undi",
        ]

        for text in telugu_cases:
            with self.subTest(text=text):
                self.assertEqual(_detect_user_language_preference(text, "english"), "telugu_english")

    def test_language_detection_handles_ten_hindi_inputs(self):
        hindi_cases = [
            "क्या हुआ आज थोड़ा tired लग रहे हो",
            "मुझे यह topic समझाओ",
            "आज class में क्या हुआ",
            "ठीक है अब next task बताओ",
            "कहाँ issue आ रहा है",
            "kya hua aaj thoda tired ho",
            "mujhe ye topic samjhao",
            "kaise prepare karna hai batao",
            "aap theek ho kya",
            "bas ab ruk jao",
        ]

        for text in hindi_cases:
            with self.subTest(text=text):
                self.assertEqual(_detect_user_language_preference(text, "english"), "hindi")

    def test_language_detection_handles_ten_mixed_language_inputs(self):
        mixed_cases = [
            ("Bro today class lo sir Hindi lo explain chesadu", "telugu_english"),
            ("Exam ki vellali but mood ledu", "telugu_english"),
            ("Project lo issue undi can you check", "telugu_english"),
            ("Sare now open browser and chudu", "telugu_english"),
            ("Naku output ravatledu please debug", "telugu_english"),
            ("Bro kya scene hai today class lo", "hindi"),
            ("Aaj assignment submit karna hai okay", "hindi"),
            ("Mujhe code samjhao but simple English lo", "hindi"),
            ("Ruko bas one minute I will tell", "hindi"),
            ("Kya bro college life chal raha hai", "hindi"),
        ]

        for text, expected in mixed_cases:
            with self.subTest(text=text):
                self.assertEqual(_detect_user_language_preference(text, "english"), expected)

    def test_language_instruction_prevents_english_fallback_for_indian_languages(self):
        hindi_instruction = _language_instruction("hindi", "hindi")
        telugu_instruction = _language_instruction("telugu_english", "telugu_english")

        self.assertIn("Devanagari", hindi_instruction)
        self.assertIn("Indian Hindi", hindi_instruction)
        self.assertIn("Do not answer only in English", hindi_instruction)
        self.assertIn("Telugu + English", telugu_instruction)
        self.assertIn("Indian Telugu speaker", telugu_instruction)
        self.assertIn("do not answer only in English", telugu_instruction)

    def test_voice_recognition_language_buttons_bias_asr_before_english(self):
        voice_hook = (PROJECT_ROOT / "src/hooks/useVoice.ts").read_text(encoding="utf-8")

        self.assertIn("if (preference === 'hindi') return ['hi-IN', 'en-IN'];", voice_hook)
        self.assertIn(
            "if (preference === 'telugu_english') return ['te-IN', 'en-IN', 'hi-IN'];",
            voice_hook,
        )
        self.assertIn("return ['en-IN', 'en-US'];", voice_hook)
        self.assertNotIn("return ['en-IN', 'te-IN', 'hi-IN'];", voice_hook)

    def test_mixed_voice_recognition_keeps_selected_indian_language_active(self):
        voice_hook = compact_source(
            (PROJECT_ROOT / "src/hooks/useVoice.ts").read_text(encoding="utf-8")
        )

        # The guarantee: a mixed-language utterance keeps the *selected* Indian
        # language listening, instead of collapsing every mixed turn to English
        # and losing the Telugu/Hindi half of the sentence. Asserted as one
        # compacted expression so the whole ternary chain is checked at once --
        # the local was renamed `voiceLanguage` -> `preference`, which broke the
        # old token-by-token version while the behaviour stayed identical.
        self.assertIn(
            "languageMode === 'mixed' "
            "? preference === 'hindi' "
            "? 'hi-IN' "
            ": preference === 'telugu_english' "
            "? 'te-IN' "
            ": 'en-IN'",
            voice_hook,
        )
        self.assertNotIn("languageMode === 'mixed' ? 'en-IN'", voice_hook)


    def test_frontend_speech_paths_use_one_shared_audio_guard(self):
        guard = (PROJECT_ROOT / "src/lib/audioPlaybackGuard.ts").read_text(encoding="utf-8")
        voice_hook = (PROJECT_ROOT / "src/hooks/useVoice.ts").read_text(encoding="utf-8")
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("claimAkanshaAudio", guard)
        self.assertIn("hardCancelBrowserSpeech", guard)
        self.assertIn("settleBrowserSpeechCancel", guard)
        self.assertIn("@/lib/audioPlaybackGuard", voice_hook)
        self.assertIn("@/lib/audioPlaybackGuard", chat_thread)
        self.assertNotIn("window.__akanshaAudioOwner", voice_hook)
        self.assertNotIn("window.__akanshaAudioOwner", chat_thread)

    def test_chat_voice_flushes_pending_transcript_and_cleans_timers(self):
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("submitVoiceTranscript(pendingTranscriptRef.current)", chat_thread)
        self.assertIn("clearVoiceFinalFlushTimer();", chat_thread)
        self.assertIn("clearTimeout(voiceRestartTimerRef.current)", chat_thread)
        self.assertIn("now - previous.at < 2500", chat_thread)

    def test_chat_voice_button_aborts_stale_recognition_and_handles_missing_mic(self):
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        # The load-bearing part is `.abort?.()`, not the cast that used to precede
        # it. `abort()` is optional on older Chromium builds, so an unguarded
        # `.abort()` throws and leaves the stale recogniser running; the optional
        # call is what this assertion is protecting. The `as any` these lines
        # carried is gone now that src/types/speech-recognition.d.ts declares the
        # interface -- asserting on the cast would pin the workaround rather than
        # the behaviour, and would fail the moment the workaround stopped being
        # needed. Which is exactly what happened.
        self.assertIn("activeRecognition?.abort?.()", chat_thread)
        self.assertIn("previousRecognition.abort?.()", chat_thread)
        self.assertIn("errorCode === 'audio-capture'", chat_thread)
        self.assertIn("No microphone input was detected", chat_thread)

    def test_chat_thread_uses_client_fast_lane_for_simple_replies(self):
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")
        compact_thread = compact_source(chat_thread)

        self.assertIn("function fastLocalChatReply", chat_thread)
        self.assertIn("const isQuickMode = chatWorkMode === 'quick'", chat_thread)
        # Compacted: prettier wraps this across four lines. The guard that
        # matters is `!hasAttachments` -- an attachment must never be answered
        # from the local fast lane, which cannot see the file.
        self.assertIn(
            "const fastReply = isQuickMode && !hasAttachments "
            "? fastLocalChatReply(content, chatLanguagePreference()) "
            ": null;",
            compact_thread,
        )
        self.assertIn("persistPlannerSideMessage('user', content)", chat_thread)
        self.assertIn("addAssistantMessage(fastReply, 'happy')", chat_thread)
        self.assertIn("if (shouldSpeakReply) {", chat_thread)


    def test_chat_composer_pastes_one_clipboard_image_only(self):
        composer = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatComposer.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("stableFileSignature", composer)
        self.assertIn("stableClipboardImageSignature", composer)
        self.assertIn("seenClipboardImages", composer)
        self.assertIn("e.stopPropagation()", composer)
        self.assertIn("e.nativeEvent.stopImmediatePropagation()", composer)
        self.assertIn('data-akansha-chat-composer="true"', composer)
        self.assertIn("onPaste={handlePaste}", composer)
        self.assertNotIn("window.addEventListener('paste'", composer)
        self.assertNotIn('window.addEventListener("paste"', composer)

    def test_sidebar_uses_client_navigation_without_full_reload(self):
        sidebar = (PROJECT_ROOT / "src/components/Sidebar.tsx").read_text(encoding="utf-8")
        compact_sidebar = compact_source(sidebar)

        self.assertIn("import Link from 'next/link'", sidebar)
        self.assertIn("<Link", sidebar)
        self.assertIn("href={item.href}", sidebar)
        self.assertIn("prefetch={false}", sidebar)
        self.assertIn("reliableNavigate", sidebar)
        self.assertIn('type="button"', sidebar)

        # This test used to require `window.setTimeout` + `window.location.href =
        # href`: a "if the router push did not land, hard-reload the page"
        # fallback. That contradicted the test's own name -- a hard reload *is*
        # the full reload it exists to prevent -- and it only existed because
        # every click called preventDefault(), making an imperative push the sole
        # way out of the route. `<Link href>` navigates on its own, so there is
        # nothing left to time out and nothing to fall back to.
        self.assertNotIn("window.location.href = href", sidebar)
        self.assertNotIn("window.location.assign(href)", sidebar)
        self.assertNotIn("window.location.reload", sidebar)

        # Modified clicks belong to the browser. Unconditional preventDefault()
        # silently broke ctrl/cmd-click-to-open-in-new-tab on every sidebar link.
        self.assertIn(
            "if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey "
            "|| event.button !== 0) { return; }",
            compact_sidebar,
        )
        # Still intercepted, deliberately: the three controls that reset the
        # thread or open a panel rather than following their own href.
        self.assertIn("event.preventDefault()", sidebar)


    def test_sidebar_delete_is_optimistic_and_restores_on_failure(self):
        sidebar = (PROJECT_ROOT / "src/components/Sidebar.tsx").read_text(encoding="utf-8")

        self.assertIn("const previousConversations = recentConversations", sidebar)
        self.assertIn("setRecentConversations((items) => items.filter((item) => item.id !== conversation.id))", sidebar)
        self.assertIn("setRecentConversations(previousConversations)", sidebar)
        self.assertIn("event.preventDefault()", sidebar)

    def test_layout_does_not_load_rocket_scripts_that_break_navigation(self):
        layout = (PROJECT_ROOT / "src/app/layout.tsx").read_text(encoding="utf-8")

        self.assertNotIn("static.rocket.new", layout)
        self.assertNotIn("rocket-web.js", layout)
        self.assertNotIn("rocket-shot.js", layout)

    def test_chat_workspace_stats_callback_does_not_create_render_loop(self):
        workspace = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatWorkspace.tsx"
        ).read_text(encoding="utf-8")
        thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("const handleStatsChange = React.useCallback", workspace)
        self.assertIn("previous.messages === messages && previous.contextUnits === contextUnits", workspace)
        self.assertIn("onStatsChange={handleStatsChange}", workspace)
        self.assertIn("lastReportedStatsRef", thread)
        self.assertIn("lastReportedStatsRef.current.messages === messageCount", thread)
        self.assertNotIn("onStatsChange={(messages, tokens) => setChatStats({ messages, tokens })}", workspace)

    def test_image_provider_error_does_not_repeat_budget_copy(self):
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("readImageForVision", chat_thread)
        self.assertIn("canvas.toDataURL('image/jpeg', 0.82)", chat_thread)
        self.assertIn("detailed image analysis did not return a usable result", chat_thread)
        self.assertIn("lower.includes('budget')", chat_thread)
        self.assertIn("lower.includes('insufficient')", chat_thread)
        self.assertNotIn("Quick mode is active", chat_thread)
        self.assertNotIn("reduced the output token limit", chat_thread)
        self.assertNotIn("token or credit budget is too low", chat_thread)

    def test_chat_renderer_hides_zero_artifacts_and_token_cost_copy(self):
        message_bubble = (
            PROJECT_ROOT / "src/app/chat-interface/components/MessageBubble.tsx"
        ).read_text(encoding="utf-8")
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")
        topbar = (PROJECT_ROOT / "src/components/Topbar.tsx").read_text(encoding="utf-8")

        self.assertNotIn("{message.tokenCount} tokens", message_bubble)
        self.assertNotIn("{totalTokens.toLocaleString()} tokens", chat_thread)
        self.assertNotIn("Token usage indicator", topbar)
        self.assertNotIn("akansha-token-usage", topbar)
        self.assertNotIn("akansha-token-usage", chat_thread)
        self.assertIn("compact === '0'", chat_thread)
        # The guard is that broken assistant rows are filtered out of restored
        # history at all. The parameter annotation is not part of that -- it used to
        # read `(m: any)` and is now inferred from ChatHistoryResponse, so pinning
        # the annotation made this test fail on a change that strengthened the very
        # thing it checks.
        self.assertIn("!isBrokenAssistantHistoryMessage(m)", chat_thread)

    def test_planner_delete_writes_through_to_local_storage(self):
        planner = (
            PROJECT_ROOT / "src/app/chat-interface/components/TaskCalendarPanel.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("writeStorage(TASKS_STORAGE_KEY, next)", planner)
        self.assertIn("writeStorage(EVENTS_STORAGE_KEY, next)", planner)
        self.assertIn("const next = previous.filter((item) => item.id !== task.id)", planner)
        self.assertIn("const next = previous.filter((item) => item.id !== event.id)", planner)
        # Sorting a copy, not `events` itself: an in-place `events.sort()` mutates
        # the state array, so React sees the same reference and the deletion never
        # repaints. Compacted -- the arrow body is wrapped onto its own line.
        self.assertIn(
            "() => [...events].sort((a, b) => getEventStartDate(a)",
            compact_source(planner),
        )


    def test_digital_twin_prompts_auto_route_without_manual_slash(self):
        slash_commands = (PROJECT_ROOT / "src/lib/slashCommands.ts").read_text(encoding="utf-8")
        composer = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatComposer.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("name: 'twin'", slash_commands)
        self.assertIn("name: 'simulate'", slash_commands)
        self.assertIn("name: 'goal'", slash_commands)
        self.assertIn("autoRouteCognitivePrompt", slash_commands)
        self.assertIn("Use Akansha Cognitive Digital Twin and Goal Engine routing", slash_commands)
        self.assertIn("autoRouteCognitivePrompt(expandSlashCommand(content))", composer)

    # Two tests were deleted here along with the architecture they described:
    # they pinned exact shader source (`p.z += mouth * 0.0`) in
    # `AssistantAvatarStage.tsx` and exact blendshape targets
    # (`target.jawOpen = 0.65;`) in `Akansha3DInterface.tsx`. Neither file exists
    # any more -- `src/components/assistant/` now holds ContinuousJarvisOverlay,
    # HumanPresence and NeuralPresence -- so both could only ever raise
    # FileNotFoundError. They asserted nothing about current behaviour, and a
    # test that cannot pass is worse than no test: it trains you to read a red
    # suite as normal. The mouth-bulge lesson they encoded survives as a comment
    # in HumanPresence.tsx.

    def test_media_followup_sets_volume_and_clicks_requested_result(self):
        plan = build_browser_prompt_plan("play third song and play at 60 volume")

        self.assertEqual(plan["steps"][0], {"action": "set_volume", "payload": {"amount": 60}})
        self.assertEqual(
            plan["steps"][1],
            {"action": "click_youtube_result", "target": "3", "payload": {"amount": 3}},
        )

    def test_youtube_search_play_uses_first_result_by_default(self):
        plan = build_browser_prompt_plan("open youtube and play bahubali 2 songs")

        self.assertEqual(plan["steps"][0]["action"], "open_youtube_song")
        self.assertEqual(plan["steps"][0]["target"], "bahubali 2 songs")
        self.assertEqual(plan["steps"][0]["payload"]["index"], 1)
        self.assertTrue(plan["steps"][0]["payload"]["play"])

    def test_active_window_shortcuts_are_planned(self):
        save_plan = build_browser_prompt_plan("save this")
        self.assertEqual(save_plan["steps"], [{"action": "hotkey", "payload": {"keys": ["ctrl", "s"]}}])

        scroll_plan = build_browser_prompt_plan("scroll down")
        self.assertEqual(
            scroll_plan["steps"],
            [{"action": "scroll", "target": "down", "payload": {"direction": "down", "amount": 6}}],
        )

    def test_form_fill_waits_for_submit_confirmation(self):
        plan = build_browser_prompt_plan(
            "open example.com and fill name Yogesh email yogesh@example.com phone 9999999999 before submitting popup notification"
        )

        self.assertEqual(plan["steps"][0], {"action": "open_url", "target": "example.com"})
        self.assertEqual(plan["steps"][2]["action"], "type_sequence")
        self.assertEqual(
            plan["steps"][2]["payload"]["values"],
            ["Yogesh", "yogesh@example.com", "9999999999"],
        )
        self.assertFalse(plan["steps"][2]["payload"]["submit"])
        self.assertEqual(plan["steps"][3]["action"], "notify_user")

    def test_submit_followup_presses_enter(self):
        plan = build_browser_prompt_plan("okay all okay submit")

        self.assertEqual(
            plan["steps"],
            [{"action": "press_key", "target": "enter", "payload": {"key": "enter"}}],
        )

    def test_compound_youtube_scroll_and_close_present_tab(self):
        scroll_plan = build_browser_prompt_plan("open youtube website and scroll one by one")
        self.assertEqual(scroll_plan["steps"][0], {"action": "open_url", "target": "https://youtube.com"})
        self.assertEqual(scroll_plan["steps"][1]["action"], "wait")
        self.assertEqual(
            scroll_plan["steps"][2],
            {"action": "scroll", "target": "down", "payload": {"direction": "down", "amount": 3}},
        )

        close_plan = build_browser_prompt_plan("close the present YouTube tab")
        self.assertEqual(close_plan["steps"], [{"action": "close_tab"}])

    def test_live_web_context_detection_for_current_questions(self):
        self.assertTrue(_needs_live_web_context("what are the latest AI news today"))
        self.assertTrue(_needs_live_web_context("search the internet for current weather"))
        self.assertTrue(_needs_live_web_context("who won the match"))
        self.assertTrue(_needs_live_web_context("what is the highest score"))
        self.assertTrue(_needs_live_web_context("silver and gold per gram"))
        self.assertTrue(_needs_live_web_context("ebullion silver rate"))
        self.assertFalse(_needs_live_web_context("explain recursion simply"))

    def test_live_ipl_search_query_is_date_anchored(self):
        query = _build_live_search_query("what are the teams having an IPL match today")

        self.assertIn("IPL", query)
        self.assertIn("match teams", query)
        self.assertRegex(query, r"\b20\d{2}\b")

    def test_ipl_points_table_prompts_force_live_structured_table_answering(self):
        prompt = "Give me the complete points table 2026 IPL"
        query = _build_live_search_query(prompt)
        context = _build_output_intent_context(prompt)

        self.assertTrue(_needs_live_web_context(prompt))
        self.assertTrue(_needs_structured_table(prompt))
        self.assertIn("points table", query)
        self.assertIn("standings", query)
        self.assertIn("clean Markdown table", context)
        self.assertIn("Never tell the user to visit a website", context)
        self.assertIn("Do not use confident words", context)
        self.assertIn("Confidence score means source coverage only", context)

    def test_ipl_standings_parser_extracts_verified_rows_without_guessing(self):
        sample = (
            "1 Royal Challengers Bengaluru 13 9 4 18+1.065 "
            "2 Gujarat Titans 13 8 5 16+0.400 "
            "3 Sunrisers Hyderabad 13 8 5 16+0.350"
        )
        rows = _parse_ipl_standings_rows(sample)

        self.assertEqual(rows[0]["team"], "Royal Challengers Bengaluru")
        self.assertEqual(rows[0]["points"], "18")
        self.assertEqual(rows[0]["nrr"], "+1.065")
        self.assertEqual(rows[1]["team"], "Gujarat Titans")

    @patch(
        "backend.ai_engine._read_url",
        return_value=(
            "1 Royal Challengers Bengaluru 13 9 4 18+1.065 "
            "2 Gujarat Titans 13 8 5 16+0.400 "
            "3 Sunrisers Hyderabad 13 8 5 16+0.350 "
            "4 Punjab Kings 13 6 6 13+0.227 "
            "5 Rajasthan Royals 12 6 6 12+0.027 "
            "6 Chennai Super Kings 13 6 7 12-0.016 "
            "7 Delhi Capitals 13 6 7 12-0.871 "
            "8 Kolkata Knight Riders 12 5 6 11-0.038 "
            "9 Mumbai Indians 12 4 8 8-0.504 "
            "10 Lucknow Super Giants 12 4 8 8-0.701"
        ),
    )
    def test_ipl_points_table_context_contains_source_verified_table(self, _mock_read):
        context = _fetch_ipl_standings_context("generate points table ipl 2026")

        self.assertIn("DIRECT LIVE DATA: IPL 2026 points table extracted", context)
        self.assertIn("| Royal Challengers Bengaluru | 13 | 9 | 4 | 0 | 18 | +1.065 | Verified from source |", context)
        self.assertIn("Do not invent missing teams", context)

    def test_source_backed_ipl_table_answer_uses_only_parsed_rows(self):
        live_context = (
            "DIRECT LIVE DATA: IPL 2026 points table extracted from Test Source (https://example.com/table). "
            "Fetched at Tuesday, May 19, 2026, 9:08 PM IST. Use exactly these rows.\n"
            "| Pos | Team | P | W | L | NR | Pts | NRR | Source status |\n"
            "|---:|---|---:|---:|---:|---:|---:|---:|---|\n"
            "| 1 | Royal Challengers Bengaluru | 13 | 9 | 4 | 0 | 18 | +1.065 | Verified from source |\n"
            "| 2 | Gujarat Titans | 13 | 8 | 5 | 0 | 16 | +0.400 | Verified from source |"
        )

        answer = _direct_source_backed_answer("generate points table ipl 2026", live_context)

        self.assertIn("Royal Challengers Bengaluru", answer)
        self.assertIn("Gujarat Titans", answer)
        self.assertIn("I did not add confidence", answer)
        self.assertNotIn("Mumbai Indians | 10", answer)

    def test_source_backed_news_answer_labels_items_as_source_reported(self):
        live_context = (
            "DIRECT LIVE DATA: India current news RSS/news feeds fetched at Tuesday, May 19, 2026, 9:10 PM IST. "
            "Use these source-attributed headlines.\n"
            "- The Hindu National: Parliament passes example bill [Tue, 19 May 2026 10:00:00 +0530] - Short summary.\n"
            "- Indian Express India: State update headline [Tue, 19 May 2026 09:00:00 +0530]"
        )

        answer = _direct_source_backed_answer("latest India news today", live_context)

        self.assertIn("| Source | Headline | Published | Status |", answer)
        self.assertIn("Source-reported, not independently confirmed", answer)
        self.assertNotIn("confidence", answer.lower())

    def test_output_format_detection_for_generated_files(self):
        prompt = "Generate Excel, PDF, PNG, JPG, CSV, JSON and invoice report for IPL stats"

        self.assertEqual(
            set(_detect_output_formats(prompt)),
            {"xlsx", "pdf", "png", "jpg", "csv", "json"},
        )
        self.assertEqual(
            set(requested_artifact_formats(prompt)),
            {"xlsx", "pdf", "png", "jpg", "csv", "json"},
        )
        self.assertIn("pdf", requested_artifact_formats("Create invoice for web design service"))
        self.assertIn("pdf", requested_artifact_formats("Make formula sheet notes for Java"))
        self.assertIn("pptx", requested_artifact_formats("Generate 10 PowerPoints about Java"))
        self.assertIn("jpg", requested_artifact_formats("Create a jpc image for the workflow"))
        # Subset, not equality: "all file formats" is an open-ended request, so
        # the set is expected to grow as new writers land -- it currently also
        # returns `html`. Pinning it exactly failed the moment a *new capability*
        # was added, which is the opposite of what this test should defend. What
        # matters is that none of the ten below ever silently disappears.
        self.assertLessEqual(
            {"pdf", "docx", "pptx", "xlsx", "csv", "json", "png", "jpg", "md", "zip"},
            set(requested_artifact_formats("Generate all file formats for this report")),
        )


    def test_sandbox_download_links_are_removed_before_real_artifacts(self):
        cleaned = sanitize_model_artifact_placeholders(
            "I made it: [Download PDF](sandbox:/fake.pdf)\n\nGenerated summary stays here."
        )

        self.assertNotIn("sandbox:", cleaned)
        self.assertNotIn("Download PDF", cleaned)
        self.assertIn("Generated summary stays here.", cleaned)

    def test_openrouter_model_uses_concrete_fast_default(self):
        ai_engine = (PROJECT_ROOT / "backend/ai_engine.py").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

        # Read the value instead of pinning the string. This used to require
        # `DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-001"` verbatim, so
        # every routine model bump broke it. The requirement was never "be this
        # model", it was "resolve to one concrete model rather than the `auto`
        # router", because `auto` silently picks paid routes.
        default_model = ai_engine_module.DEFAULT_OPENROUTER_MODEL
        self.assertNotIn(default_model.lower(), {"openrouter/auto", "auto", "/auto", ""})
        self.assertIn("/", default_model)
        self.assertIn('configured.lower() in {"openrouter/auto", "auto", "/auto"}', ai_engine)
        # This used to assert `model=OPENROUTER_MODEL` appeared verbatim, back
        # when one constant named the only reachable route. Requests now carry
        # `model_routes.wire_name(...)` so a local `ollama/` id loses its
        # provider prefix on the wire, and the constant is only the *head* of a
        # cascade. Pinning the old spelling would forbid the fix rather than
        # protect it; what still matters is that nothing hardcodes a model at a
        # call site, which is what these two assertions cover.
        self.assertIn("model=model_routes.wire_name(", ai_engine)
        self.assertNotIn('OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/auto")', ai_engine)
        self.assertNotIn('model="openai/gpt-4o-mini"', ai_engine)

        # The example env must not ship an *active* paid override. Doing so cost a
        # fresh clone one guaranteed 402 per process before the cooldown demoted
        # the dead route -- a real request spent to learn nothing.
        active_env_lines = [
            line.strip()
            for line in env_example.splitlines()
            if line.strip().startswith("OPENROUTER_MODEL=")
        ]
        self.assertEqual([], active_env_lines)
        self.assertNotIn("OPENROUTER_MODEL=openrouter/auto", env_example)


    def test_openrouter_client_loads_key_lazily_from_app_env(self):
        ai_engine = (PROJECT_ROOT / "backend/ai_engine.py").read_text(encoding="utf-8")

        self.assertIn('ENV_PATH = PROJECT_ROOT / ".env"', ai_engine)
        self.assertIn("load_dotenv(ENV_PATH, override=True)", ai_engine)
        self.assertIn("def _openrouter_client()", ai_engine)
        self.assertIn("api_key=_openrouter_api_key()", ai_engine)
        self.assertNotIn("client = OpenAI(", ai_engine)

    def test_quick_chat_only_bypasses_model_for_direct_time_utilities(self):
        dynamic_prompts = [
            "hi",
            "namaskar",
            "how is the world going on",
            "hows the world is going on",
            "What's going on? Is all ok now",
            "how are you?",
            "ok now tell me joke",
            "yes",
            "aha aha",
            "ohh",
            "enti inka",
            "what is the present dollar price",
            "Nandamuri Taraka Rama Rao, date of birth",
            "Nandamuritha Raka Ram Rao, date of birth",
            "umma",
            "u mma",
            "aku paku",
            "itit is the some thing else",
            "completed well i am now in the vacation holidays cam to my home town anatapur",
        ]

        for prompt in dynamic_prompts:
            self.assertFalse(_should_use_fast_local_reply(prompt), prompt)

        self.assertTrue(_should_use_fast_local_reply("what's the present time"))
        self.assertTrue(_should_use_fast_local_reply("present ist time"))
        self.assertTrue(_should_use_fast_local_reply("what is exact london time"))
        self.assertTrue(_should_skip_ai_memory_analysis("hi", "Hi Yogesh, I'm ready."))
        self.assertTrue(_should_skip_ai_memory_analysis("ok now tell me joke", "One quick joke."))
        self.assertTrue(_should_skip_ai_memory_analysis("what is the present dollar price", "Right now, 1 US dollar is about Rs. 83.00 INR."))
        self.assertTrue(_should_skip_ai_memory_analysis("present ist time", "IST time is 3:00 PM."))
        self.assertFalse(_should_use_fast_local_reply("what is the latest IPL score today"))
        self.assertFalse(_should_use_fast_local_reply("generate a PDF report with 10 pages"))

    def test_provider_failure_fallback_keeps_static_chat_out_of_quick_path(self):
        with patch("backend.ai_engine._fetch_wikipedia_summary", return_value=(
            "N. T. Rama Rao",
            "N. T. Rama Rao was an Indian actor and politician.",
            "https://en.wikipedia.org/wiki/N._T._Rama_Rao",
        )), patch("backend.ai_engine._fetch_wikidata_birth_date", return_value="May 28, 1923"):
            namaskar = _fast_local_reply_for_provider_failure("namaskar", "hindi")
            joke = _fast_local_reply_for_provider_failure("ok now tell me joke", "english")
            london = _fast_local_reply_for_provider_failure("what is exact london time", "english")
            ist = _fast_local_reply_for_provider_failure("present ist time", "english")
            correction = _fast_local_reply_for_provider_failure("itit is the some thing else", "english")
            fragment = _fast_local_reply_for_provider_failure("aku paku", "english")
            acknowledgement = _fast_local_reply_for_provider_failure("aha aha", "english")
            ntr = _fast_local_reply_for_provider_failure("Nandamuri Taraka Rama Rao, date of birth", "english")
            ntr_misspelled = _fast_local_reply_for_provider_failure("Nandamuritha Raka Ram Rao, date of birth", "english")

        dynamic_fallbacks = namaskar + joke + correction + fragment + acknowledgement + ntr + ntr_misspelled
        self.assertIn("May 28, 1923", dynamic_fallbacks)
        self.assertNotIn("answer engine did not answer", dynamic_fallbacks.lower())
        self.assertIn("London time is", london)
        self.assertIn("IST time is", ist)
        combined = dynamic_fallbacks + london + ist
        self.assertNotIn("Quick mode is active", combined)
        self.assertNotIn("I caught that, Yogesh", combined)
        self.assertNotIn("Tell me what you want me to do with it", combined)
        self.assertNotIn("token/credit", combined.lower())
        self.assertNotIn("budget", combined.lower())

    def test_numbered_json_attachment_is_answered_locally_without_provider(self):
        data = [
            {"id": 1, "question": "Warmup"},
            {"question_number": 200, "question": "What is polymorphism in Java?", "topic": "OOP", "answer": "One interface, many implementations."},
            {"id": 201, "question": "Inheritance"},
        ]
        answer = _local_attachment_question_answer(
            "what is 200 question is about in the file",
            [{"name": "a5.json", "type": "application/json", "text": json.dumps(data)}],
        )

        self.assertIsNotNone(answer)
        self.assertIn("Question 200 in a5.json", answer or "")
        self.assertIn("polymorphism", (answer or "").lower())
        self.assertIn("OOP", answer or "")

    def test_chat_response_limit_is_scaled_by_work_mode(self):
        # The name is the contract: research mode must buy a bigger budget than
        # the default for the *same* prompt, and a throwaway question must stay
        # cheaper than a document build. The old version pinned absolute ceilings
        # (180 and 220) that a deliberate tuning pass raised to 260 and 420, so it
        # failed for a change that preserved every ordering below. Loose sanity
        # bounds keep it honest without re-breaking on the next tuning.
        quick = _response_token_limit("tell me one quick idea")
        score = _response_token_limit("what is the latest cricket score today")
        score_research = _response_token_limit(
            "what is the latest cricket score today", conversation_mode="research"
        )
        report = _response_token_limit("generate pdf report with tables")
        report_research = _response_token_limit(
            "generate pdf report with tables", conversation_mode="research"
        )

        self.assertGreater(score_research, score)
        self.assertGreater(report_research, report)
        self.assertGreater(report, quick)
        self.assertLessEqual(quick, 300)
        self.assertLessEqual(score, 500)
        self.assertGreaterEqual(score_research, 800)
        self.assertLessEqual(report, 900)
        self.assertGreaterEqual(report_research, 1200)


    def test_provider_capacity_failure_returns_safe_non_repeating_fallback(self):
        fallback = _provider_failure_fallback(
            "how is the world going on",
            None,
            RuntimeError("402 provider capacity refused"),
            "english",
        )

        self.assertIn("world is mixed", fallback.lower())
        self.assertNotIn("quick mode is active", fallback.lower())
        self.assertNotIn("live reasoning path", fallback.lower())
        self.assertNotIn("token/credit budget", fallback.lower())
        self.assertNotIn("credit", fallback.lower())
        self.assertNotIn("budget", fallback.lower())
        self.assertNotIn("reduced the output token limit", fallback.lower())

    def test_provider_capacity_failure_keeps_work_modes_separate(self):
        fallback = _provider_failure_fallback(
            "what is the latest IPL score today",
            None,
            RuntimeError("402 provider capacity refused"),
            "english",
        )
        lowered = fallback.lower()

        # Two legitimate outcomes, and this test must accept both: with network
        # reachable the live-source path now answers the question from real
        # sources, and offline it returns the safe placeholder. The old version
        # required the placeholder wording ("could not verify" /
        # "Research/Agent/Skill"), so it failed precisely *because* the answer got
        # better -- a sourced table instead of an apology.
        answered_from_sources = "source-reported" in lowered or "fetched at" in lowered
        declined_safely = "could not verify" in lowered and "Research/Agent/Skill" in fallback
        self.assertTrue(
            answered_from_sources or declined_safely,
            f"neither sourced nor safely declined: {fallback[:200]!r}",
        )

        # The actual guarantee, and it holds either way: a provider billing
        # failure is our problem, not the user's, so none of it may surface in the
        # reply. Verified against the live sourced answer as well as the
        # placeholder.
        self.assertNotIn("live reasoning path", lowered)
        self.assertNotIn("quick mode is active", lowered)
        self.assertNotIn("token/credit budget", lowered)
        self.assertNotIn("credit", lowered)
        self.assertNotIn("budget", lowered)
        self.assertNotIn("402", lowered)
        self.assertNotIn("openrouter", lowered)
        self.assertNotIn("reduced the output token limit", lowered)


    def test_provider_image_failure_is_graceful_without_false_pixel_analysis(self):
        fallback = _provider_failure_fallback(
            "analyze the image",
            [{"type": "image/png", "name": "screenshot.png"}],
            RuntimeError("402 provider capacity refused"),
            "english",
        )

        self.assertIn("received the image", fallback)
        self.assertIn("without guessing", fallback)
        self.assertNotIn("token/credit budget", fallback.lower())
        self.assertNotIn("credit", fallback.lower())
        self.assertNotIn("budget", fallback.lower())

    def test_provider_image_failure_uses_local_pixel_pass_when_data_url_exists(self):
        transparent_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
        fallback = _provider_failure_fallback(
            "analyze the image",
            [{"type": "image/png", "name": "screenshot.png", "data_url": transparent_png, "size": 68}],
            RuntimeError("402 provider capacity refused"),
            "english",
        )

        self.assertIn("local pixel pass", fallback)
        self.assertIn("screenshot.png", fallback)
        self.assertIn("1x1px", fallback)
        self.assertNotIn("token/credit budget", fallback.lower())
        self.assertNotIn("credit", fallback.lower())
        self.assertNotIn("budget", fallback.lower())

    def test_background_memory_analysis_has_bounded_length_and_clean_errors(self):
        ai_engine = (PROJECT_ROOT / "backend/ai_engine.py").read_text(encoding="utf-8")

        self.assertIn("max_tokens=220", ai_engine)
        self.assertIn("Analysis skipped: provider unavailable for background memory extraction.", ai_engine)
        self.assertNotIn("Analysis failed:", ai_engine)
        self.assertNotIn("8192", ai_engine)

    def test_chat_work_modes_are_explicitly_routed(self):
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("CHAT_WORK_MODES", chat_thread)
        self.assertIn("label: 'Quick'", chat_thread)
        self.assertIn("label: 'Research'", chat_thread)
        self.assertIn("label: 'Agent'", chat_thread)
        self.assertIn("label: 'Skill'", chat_thread)
        self.assertIn("chatWorkMode === 'quick'", chat_thread)
        self.assertIn("conversation_mode: shouldSpeakReply ? 'voice' : chatWorkMode", chat_thread)

    def test_empty_or_spurious_zero_chat_response_is_rejected(self):
        self.assertTrue(is_broken_assistant_response("hello", ""))
        self.assertTrue(is_broken_assistant_response("hello", "0"))
        self.assertFalse(is_broken_assistant_response("what is zero plus zero", "0"))
        self.assertFalse(is_broken_assistant_response("hello", "Hi Yogesh"))

    def test_java_pdf_prompt_generates_requested_pages_and_complete_code(self):
        prompt = (
            "generate pdf containing code of java all top questions required for tcs placements "
            "10 pages atleast along with complete code in Java, along with comments for each question, "
            "give at least 30 coding questions"
        )
        artifacts = create_requested_artifacts(prompt, "Short outline only.")
        pdf_artifact = next(artifact for artifact in artifacts if artifact["format"] == "pdf")
        pdf_path = PROJECT_ROOT / "generated_artifacts" / pdf_artifact["name"]

        self.assertTrue(pdf_path.exists())
        if fitz := __import__("fitz"):
            document = fitz.open(pdf_path)
            text = "\n".join(page.get_text() for page in document)
            self.assertGreaterEqual(document.page_count, 10)
            document.close()
            self.assertIn("Question 30", text)
            self.assertIn("Complete Java code with comments", text)
            self.assertIn("public class Solution", text)

    def test_document_generation_prompt_is_not_treated_as_browser_automation(self):
        automation_commands = (PROJECT_ROOT / "src/lib/automationCommands.ts").read_text(encoding="utf-8")

        self.assertIn("ARTIFACT_GENERATION_PATTERNS", automation_commands)
        self.assertIn("return false", automation_commands)
        self.assertIn("pdf|pptx?", automation_commands)

    def test_artifact_engine_creates_downloadable_structured_outputs(self):
        response = (
            "| Team | Pts | Status |\n"
            "|---|---:|---|\n"
            "| Punjab Kings | 17 | Verified |\n"
            "| Mumbai Indians | 16 | Verified |\n"
        )

        artifacts = create_requested_artifacts("Generate Excel and CSV of IPL stats", response)
        formats = {artifact["format"] for artifact in artifacts}
        markdown = artifact_markdown(artifacts)

        self.assertEqual(formats, {"xlsx", "csv"})
        self.assertIn("| Format | Download |", markdown)
        for artifact in artifacts:
            self.assertTrue((PROJECT_ROOT / "generated_artifacts" / artifact["name"]).exists())

    def test_chat_message_renderer_supports_markdown_tables_and_modifier_links(self):
        message_bubble = (
            PROJECT_ROOT / "src/app/chat-interface/components/MessageBubble.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function renderMarkdownTable", message_bubble)
        self.assertIn("isMarkdownTableStart", message_bubble)
        self.assertIn("/generated", message_bubble)
        self.assertIn("window.open(url, '_blank'", message_bubble)

    def test_frontend_generated_route_serves_backend_artifacts(self):
        route = (PROJECT_ROOT / "src/app/generated/[...path]/route.ts").read_text(encoding="utf-8")

        self.assertIn("generated_artifacts", route)
        self.assertIn("application/pdf", route)
        self.assertIn("application/vnd.openxmlformats-officedocument.presentationml.presentation", route)
        self.assertIn("path.relative", route)
        self.assertIn("Generated file not found", route)

    def test_yesterday_ipl_search_query_uses_requested_temporal_word(self):
        query = _build_live_search_query("yesterday IPL match teams and highest score")

        self.assertIn("IPL", query)
        self.assertIn("highest scorer", query)
        self.assertIn("match teams score", query)

    def test_openai_latest_model_query_prefers_official_source(self):
        query = _build_live_search_query("what is the latest GPT model today")

        self.assertIn("site:openai.com", query)
        self.assertIn("official", query)

    def test_live_silver_price_query_prefers_bullion_and_exchange_sources(self):
        query = _build_live_search_query("what is current silver price in India today")
        profile = _preferred_live_source_profile("what is current silver price in India today")

        self.assertIn("site:ebullion.in", query)
        self.assertIn("site:ibjarates.com", query)
        self.assertIn("site:mcxindia.com", query)
        self.assertIn("eBullion", profile["policy"])
        self.assertIn("unit", profile["policy"])

    def test_live_per_gram_metal_query_prefers_ebullion_even_without_price_word(self):
        query = _build_live_search_query("silver and gold per gram")

        self.assertIn("site:ebullion.in", query)
        self.assertIn("silver gold rate", query)

    @patch(
        "backend.ai_engine._read_url",
        return_value='{"data":{"gold":{"sellRate":16216.25,"buyRate":15511.6,"variationType":"up","variation":"132.45"},"silver":{"sellRate":290.92,"buyRate":272.7,"variationType":"up","variation":"3.21"}}}',
    )
    def test_direct_metal_context_uses_ebullion_per_gram_first(self, _mock_read):
        context = _build_direct_live_data_context("silver and gold per gram")

        self.assertIn("eBullion live metal ticker", context)
        self.assertIn("Silver sell INR 290.92/g", context)
        self.assertIn("Gold sell INR 16216.25/g", context)

    def test_live_weather_query_prefers_imd_sources(self):
        query = _build_live_search_query("current weather in Hyderabad today")

        self.assertIn("site:mausam.imd.gov.in", query)
        self.assertIn("official IMD", query)

    def test_live_stock_query_prefers_exchange_sources(self):
        query = _build_live_search_query("current TCS stock price")

        self.assertIn("site:nseindia.com", query)
        self.assertIn("site:bseindia.com", query)

    def test_live_india_news_query_avoids_generic_search_noise(self):
        query = _build_live_search_query("what happened in India latest news today")

        self.assertIn("site:thehindu.com", query)
        self.assertIn("site:indianexpress.com", query)
        self.assertIn("India latest news today", query)

    @patch(
        "backend.ai_engine._read_url",
        return_value=(
            "<?xml version='1.0'?><rss><channel><item>"
            "<title>Verified headline from source</title><link>https://example.com/one</link>"
            "<pubDate>Tue, 19 May 2026 16:30:00 +0530</pubDate>"
            "<description>Source-reported description.</description></item></channel></rss>"
        ),
    )
    def test_news_context_forbids_extra_confident_claims(self, _mock_read):
        context = _fetch_news_direct_context("latest India news today")

        self.assertIn("source-attributed headlines", context)
        self.assertIn("Do not create extra news claims", context)
        self.assertIn("Source-reported", context)

    @patch("backend.ai_engine._read_url")
    def test_direct_news_context_uses_rss_before_generic_search(self, mock_read):
        mock_read.return_value = (
            "<?xml version='1.0'?><rss><channel><item>"
            "<title>India headline one</title><link>https://example.com/one</link>"
            "<pubDate>Thu, 14 May 2026 01:00:00 +0530</pubDate>"
            "<description>Verified current item.</description></item></channel></rss>"
        )

        context = _build_direct_live_data_context("what happened in India latest news today")

        self.assertIn("India current news RSS/news feeds", context)
        self.assertIn("India headline one", context)

    def test_live_question_split_handles_compound_query(self):
        questions = _split_live_questions(
            "yesterday IPL match what are the teams and who won the match and what is the highest score"
        )

        self.assertGreaterEqual(len(questions), 2)
        self.assertTrue(any("who won" in question for question in questions))

    def test_live_ipl_context_builds_direct_answer_hint(self):
        live_context = (
            "LIVE WEB CONTEXT:\n"
            "1. Aaj Kiska Match Hai IPL Today Match Schedule 13 May 2026 RCB vs KKR\n"
            "   URL: https://www.timesnowhindi.com/sports/cricket/example\n"
            "2. Royal Challengers Bengaluru vs Kolkata Knight Riders full schedule\n"
            "   URL: https://newsable.asianetnews.com/example"
        )

        self.assertEqual(
            _extract_matchups_from_live_context(live_context)[0],
            "Royal Challengers Bengaluru (RCB) vs Kolkata Knight Riders (KKR)",
        )

        hint = _build_live_answer_hint("what are the teams having an IPL match today", live_context)

        self.assertIn("Royal Challengers Bengaluru (RCB) vs Kolkata Knight Riders (KKR)", hint)
        self.assertIn("Do not tell the user to check a website", hint)
        self.assertIn("IST", hint)

    def test_live_matchup_extraction_understands_face_wording(self):
        live_context = "Page details: Royal Challengers Bengaluru face Kolkata Knight Riders at Raipur."

        self.assertEqual(
            _extract_matchups_from_live_context(live_context),
            ["Royal Challengers Bengaluru vs Kolkata Knight Riders"],
        )

    def test_live_matchup_extraction_filters_non_ipl_noise(self):
        live_context = (
            "Page details: Royal Challengers Bengaluru face Kolkata Knight Riders. "
            "Unrelated card: Roman Reigns vs Jacob Fatu."
        )

        self.assertEqual(
            _extract_matchups_from_live_context(live_context),
            ["Royal Challengers Bengaluru vs Kolkata Knight Riders"],
        )

    @patch("backend.ai_engine._fetch_live_web_context", return_value="LIVE WEB CONTEXT:\n1. verified result")
    def test_multi_question_live_context_keeps_each_question(self, _mock_fetch):
        context = _build_multi_question_live_context(
            "what is the latest GPT model and what is today's IPL match score"
        )

        self.assertIn("LIVE QUESTION 1", context)
        self.assertIn("LIVE QUESTION 2", context)
        self.assertIn("SEARCH QUERY:", context)

    @patch("backend.ai_engine._fetch_live_web_context", return_value="LIVE WEB CONTEXT:\n1. verified result")
    def test_multi_question_live_context_inherits_ipl_and_date(self, _mock_fetch):
        context = _build_multi_question_live_context(
            "yesterday IPL match what are the teams and who won the match and what is the highest score"
        )

        self.assertIn("LIVE QUESTION 1", context)
        self.assertIn("LIVE QUESTION 2: yesterday who won the match IPL", context)
        self.assertIn("LIVE QUESTION 3: yesterday what is the highest score IPL", context)

    def test_auth_password_hash_verification(self):
        hashed = hash_password("correct-password")

        self.assertTrue(verify_password("correct-password", hashed))
        self.assertFalse(verify_password("wrong-password", hashed))
        self.assertNotIn("correct-password", hashed)

    def test_auth_email_normalization(self):
        self.assertEqual(normalize_auth_email("  USER@Example.COM "), "user@example.com")

    def test_desktop_control_verbs_reach_the_automation_router(self):
        """Every verb the owner listed as "control" must be routable.

        `isAutomationIntent` decides, client-side, whether a chat turn goes to the
        automation endpoint or to the language model. It is an AND of two lists --
        a trigger verb and a target noun -- and nine of twenty measured command
        forms satisfied only one half, so they fell through and the model described
        the action instead of performing it. `minimize chrome` had the target and no
        trigger; `click on the login link` had the trigger and no target.

        Asserted against the source because there is no JS test runner in this
        project. The pairs below are the ones that actually failed, not a sample.
        """
        commands = (PROJECT_ROOT / "src/lib/automationCommands.ts").read_text(
            encoding="utf-8"
        )

        for trigger in (
            r"\bminimi[sz]e\b",
            r"\bmaximi[sz]e\b",
            r"\bswitch\b",
            r"\bpress\b",
            r"\bhover\b",
            r"\brefresh\b",
            r"\bscreenshot\b",
        ):
            self.assertIn(trigger, commands, f"missing automation trigger: {trigger}")

        for target in (
            r"\blinks?\b",
            r"\bbuttons?\b",
            r"\bicons?\b",
            r"\bvs\s?code\b",
            r"\bsearch\s?(?:bar|box)\b",
        ):
            self.assertIn(target, commands, f"missing automation target: {target}")

        # `press enter` cannot be satisfied by the pair test at all: `press` and
        # `enter` are both triggers and neither is a target, so it needs an exact
        # match. Same for a bare `right click`.
        self.assertIn(r"^\s*press\s+(?:and\s+hold\s+)?", commands)
        self.assertIn(r"^\s*(?:right|middle)[\s-]?click\b", commands)

        # Guard the other direction. `write` is a trigger, so adding a bare `code`
        # target would route "write code for me" to the desktop instead of
        # answering it. These three must stay out of the target list.
        for forbidden in (r"/\bcode\b/i,", r"/\bemail\b/i,", r"/\bmessages?\b/i,"):
            self.assertNotIn(
                forbidden,
                commands,
                f"{forbidden} as a target pairs with `write` and misroutes chat",
            )


class VoiceTurnParityTests(unittest.TestCase):
    """`/api/voice/chat` must record a turn the same way `/api/chat/stream` does.

    It used to stream a reply and persist nothing, so a question asked out loud
    never entered conversation history, never reached the knowledge graph and
    never triggered the memory analyser -- while the identical question typed did
    all three. The assistant genuinely could not learn from speech.
    """

    def _voice_endpoint_source(self) -> str:
        source = (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/api/voice/chat")')
        # Up to the next top-level decorator, so these assertions cannot be
        # satisfied by code belonging to a neighbouring endpoint.
        end = source.index("\n@app.", start + 10)
        return source[start:end]

    def test_voice_turn_is_persisted_and_learned_from(self):
        body = self._voice_endpoint_source()

        for required, why in (
            ('role="user"', "the spoken turn is never stored"),
            ('role="assistant"', "her reply is never stored"),
            ("record_chat_message_in_graph", "voice turns never reach the graph"),
            ("analyze_intent_and_memory", "voice teaches the assistant nothing"),
            ("is_broken_assistant_response", "an empty reply is spoken as silence"),
            ("create_requested_artifacts", "a spoken 'make me a file' produces no file"),
            (
                "sanitize_model_artifact_placeholders",
                "placeholder markers get read out loud as words",
            ),
        ):
            self.assertIn(required, body, f"/api/voice/chat regressed: {why}")

    def test_spoken_content_excludes_artifact_markdown(self):
        """She must not read markdown links aloud.

        The stored message keeps `artifact_block` so the file stays findable in
        history; the `done` event that the browser speaks must not, because a
        markdown link read by a TTS engine is a stream of punctuation.
        """
        body = self._voice_endpoint_source()
        self.assertIn("full + artifact_block", body, "artifact links are not stored")
        self.assertNotIn(
            "'content': full + artifact_block",
            body,
            "artifact markdown is being spoken aloud",
        )


class SingleVoiceIdentityTests(unittest.TestCase):
    """Akansha must sound like one person.

    `shouldUseFastBrowserSpeech` used to return true for any queued English chunk
    of <=180 characters, which sent that chunk to the browser's system voice while
    longer chunks used the server voice. Her identity changed between chunks of a
    single reply -- two voices in one answer -- and the rule also suppressed the
    `preparedAudio` prefetch for exactly the short chunks it was meant to speed up,
    so it was not even a real latency trade.
    """

    def test_no_character_length_voice_switch(self):
        voice_hook = (PROJECT_ROOT / "src/hooks/useVoice.ts").read_text(encoding="utf-8")
        self.assertNotIn(
            "FAST_BROWSER_SPEECH_CHAR_LIMIT",
            voice_hook,
            "a character-length threshold again picks which voice speaks",
        )

    def test_browser_fallback_cannot_double_speak(self):
        """A late `audio.onerror` must not re-read what was already spoken.

        `onerror` can fire after `onplay` -- a truncated blob, a mid-sentence
        decode failure -- and that rejection lands in the same `catch` as a total
        failure. Without this guard the fallback spoke the entire text again, in a
        different voice, over audio the user had already heard.
        """
        voice_hook = (PROJECT_ROOT / "src/hooks/useVoice.ts").read_text(encoding="utf-8")
        self.assertIn("serverAudioStarted = true", voice_hook)
        self.assertIn(
            "if (serverAudioStarted || !isCurrentPlayback()) {",
            voice_hook,
            "the browser fallback can still speak over server audio",
        )

    def test_chat_page_uses_the_same_voice_as_the_voice_page(self):
        """The third vocal identity.

        `ChatThread` built its own `SpeechSynthesisUtterance` and picked a local
        Windows voice by name (Samantha, Heera, Neerja...). It was correctly
        arbitrated against `useVoice`, so the two never overlapped -- which made
        this quieter than a collision and worse than one: the same assistant was a
        different person depending on which page you were on. It must reach for
        `/api/voice/tts` first and keep the local engine as failure-only.
        """
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "fetchAkanshaSpeech(",
            chat_thread,
            "the chat page no longer routes speech through the shared server voice",
        )
        # Order matters, not just presence: the local engine has to come after the
        # server attempt, or it is the primary path again with extra steps.
        self.assertLess(
            chat_thread.index("fetchAkanshaSpeech("),
            chat_thread.index("new SpeechSynthesisUtterance("),
            "the local Windows voice is constructed before the server voice is tried",
        )
        self.assertIn(
            "if (serverAudioStarted) return;",
            chat_thread,
            "the chat fallback can speak over server audio it already started",
        )

    def test_stop_chat_speech_can_stop_server_audio(self):
        """`hardCancelBrowserSpeech()` cannot stop an `<audio>` element.

        `stopChatSpeech` is the function registered with `claimAkanshaAudio`, so it
        is what the voice page calls to take audio from the chat page. Once the
        chat page plays server audio, a stop path that only cancels
        `speechSynthesis` lets that audio run straight through the takeover -- two
        voices again, arriving by a different route.
        """
        chat_thread = (
            PROJECT_ROOT / "src/app/chat-interface/components/ChatThread.tsx"
        ).read_text(encoding="utf-8")
        stop_start = chat_thread.index("const stopChatSpeech = useCallback(")
        stop_body = chat_thread[stop_start : chat_thread.index("}, [", stop_start)]
        self.assertIn("audio.pause()", stop_body)
        self.assertIn("releaseChatTtsUrl()", stop_body)


class VoiceLiveLookupTests(unittest.TestCase):
    """Spoken questions get the same live web context as typed ones.

    `generate_chat_stream` used to skip live fetches whenever the session id
    started with `voice`, to avoid 2-8s of blocking latency. That traded accuracy
    for responsiveness on the one channel where a wrong answer is hardest to
    notice -- there is no visible citation to disbelieve when it is spoken.

    The latency was real, so it is covered rather than reintroduced: a short
    acknowledgement is yielded before the fetch, which the client's TTS speaks
    over the network wait. That is what a person does before looking something up.
    """

    def _engine_source(self) -> str:
        return (PROJECT_ROOT / "backend/ai_engine.py").read_text(encoding="utf-8")

    def test_voice_sessions_are_not_excluded_from_live_context(self):
        source = self._engine_source()
        self.assertNotIn(
            "if _needs_live_web_context(user_input) and not _is_voice_session:",
            source,
            "voice sessions are again excluded from live web context",
        )
        self.assertIn("if _needs_live_web_context(user_input):", source)

    def test_lookup_latency_is_covered_by_speech(self):
        source = self._engine_source()
        gate = source.index("if _needs_live_web_context(user_input):")
        fetch = source.index("_live_context_within_budget(", gate)
        window = source[gate:fetch]
        self.assertIn(
            "yield _live_lookup_acknowledgement(",
            window,
            "nothing is spoken to cover the live fetch, so voice goes silent for seconds",
        )

    def test_the_covered_fetch_is_also_bounded(self):
        """Covering the wait with speech is not the same as bounding it.

        The acknowledgement makes a 2-8s lookup bearable. It does nothing for the
        unbounded case, which is what "a very high amount of latency" was: the
        pre-model lookup walks up to four questions with per-request timeouts as
        long as 14s, and nothing capped the total, so a spoken turn could sit
        through a minute of network work after saying "let me check that".
        `_live_context_within_budget` is what puts a wall clock on it, and voice
        must get the shorter of the two budgets -- the listener is already waiting.
        """
        source = self._engine_source()
        gate = source.index("if _needs_live_web_context(user_input):")
        call = source.index("_live_context_within_budget(", gate)
        window = source[call : call + 240]
        self.assertIn("_PRE_MODEL_LIVE_BUDGET_VOICE_S if _is_voice_session", window)
        self.assertLess(
            _PRE_MODEL_LIVE_BUDGET_VOICE_S,
            _PRE_MODEL_LIVE_BUDGET_TEXT_S,
            "voice waits at least as long as text for a lookup it is speaking over",
        )
        self.assertLessEqual(
            _PRE_MODEL_LIVE_BUDGET_VOICE_S,
            _CASCADE_BUDGET_VOICE_S,
            "the lookup alone may outlast the whole model cascade budget",
        )

    def test_the_fallback_lookup_cannot_outlast_the_fallback(self):
        """The apology must not be slower than the answer it replaces.

        `_provider_failure_fallback` forces a live lookup when it has no context,
        and that runs *after* the cascade budget has already been spent, in
        silence, because the fallback is one `yield` at the end of the generator.
        Unbounded, it is the other half of "no fallback reply is coming": the
        fallback was not missing, it was still shopping for sources.
        """
        self.assertLess(
            _FALLBACK_LIVE_BUDGET_S,
            _PRE_MODEL_LIVE_BUDGET_VOICE_S,
            "the fallback's lookup is allowed more time than the pre-model one",
        )
        source = self._engine_source()
        fallback = source.index("def _provider_failure_fallback(")
        window = source[fallback : fallback + 4000]
        self.assertIn("_live_context_within_budget(user_input, force=True)", window)
        self.assertNotIn(
            "_build_multi_question_live_context(user_input, force=True)",
            window,
            "the fallback again calls the unbounded lookup directly",
        )

    def test_acknowledgement_is_deterministic_and_varied(self):
        """Same question -> same opener; different questions -> different openers.

        Randomness here would be untestable and would make a repeated question
        sound different each time for no reason. One fixed phrase would make her
        sound like a recording. A hash of the question gives both.
        """
        first = _live_lookup_acknowledgement("what is the weather in hyderabad")
        self.assertEqual(first, _live_lookup_acknowledgement("what is the weather in hyderabad"))
        self.assertTrue(first.strip(), "the acknowledgement is empty, so nothing covers the fetch")

        openers = {
            _live_lookup_acknowledgement(f"who won the match on day {n}") for n in range(24)
        }
        self.assertGreater(len(openers), 1, "every lookup uses the same fixed phrase")

    def test_acknowledgement_follows_the_spoken_language(self):
        """Answering a Telugu question with an English filler breaks the illusion."""
        telugu = _live_lookup_acknowledgement("ఇవాళ వాతావరణం ఎలా ఉంది", "telugu")
        hindi = _live_lookup_acknowledgement("आज मौसम कैसा है", "hindi")
        english = _live_lookup_acknowledgement("how is the weather today", "english")
        self.assertTrue(any("ఀ" <= ch <= "౿" for ch in telugu), telugu)
        self.assertTrue(any("ऀ" <= ch <= "ॿ" for ch in hindi), hindi)
        self.assertTrue(english.isascii(), english)


class ChatStreamTransportTests(unittest.TestCase):
    """The socket must not go quiet, and the endpoint must not leak a thread a turn.

    Both of these are `/api/chat/stream` rather than the engine, and both are part
    of what "very high latency, no fallback reply" looked like from the browser:

      - the SSE loop awaited the chunk queue with no timeout, so between the POST
        and the first model token -- a live lookup plus the whole model cascade --
        the response body was empty. A spinner with no bytes behind it is
        indistinguishable from a hang;
      - a `ThreadPoolExecutor(max_workers=1)` was created per request and never
        shut down. Its worker blocks on an empty queue rather than exiting, so
        every message left a live thread behind for the life of the process.

    Asserted against the source because the alternative is driving a real model
    cascade to observe a heartbeat, and the thing worth protecting here is the
    shape of the loop rather than a timing measurement.
    """

    def _main_source(self) -> str:
        return (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")

    def _stream_endpoint(self) -> str:
        source = self._main_source()
        start = source.index('@app.post("/api/chat/stream")')
        return source[start : source.index("\n@app.", start + 10)]

    def test_the_stream_sends_a_heartbeat_while_it_waits(self):
        body = self._stream_endpoint()
        self.assertIn("asyncio.wait_for(chunk_queue.get()", body)
        self.assertIn("_STREAM_HEARTBEAT_S", body)
        self.assertNotIn(
            "chunk = await chunk_queue.get()",
            body,
            "the stream again waits on the queue with no timeout, so it writes nothing for the whole turn",
        )

    def test_the_heartbeat_is_a_frame_the_client_already_ignores(self):
        """It must not become part of the reply.

        ChatThread accumulates every `data:` frame's content into the message it
        saves, so a visible "still working" chunk would end up in the transcript.
        An SSE comment has no `data:` line, and the client's parser skips frames
        without one -- which is why this is a comment and not a new payload type.
        """
        body = self._stream_endpoint()
        self.assertIn('yield ": keep-alive\\n\\n"', body)

    def test_the_generator_executor_is_shut_down(self):
        body = self._stream_endpoint()
        self.assertIn("executor.shutdown(wait=False)", body)
        self.assertIn("finally:", body)


class SendConfirmationTests(unittest.TestCase):
    """Nothing leaves the machine without slots and a yes.

    Voice has no undo. A typed message can be re-read before you press send; a
    spoken one is gone the moment it is understood, and it was understood by a
    recogniser that guesses. So `voice_dialog` asks which app, asks who, reads the
    message back, and waits -- and asks for a name letter by letter when what came
    back is not name-shaped.
    """

    def test_a_send_needs_platform_recipient_message_and_a_yes(self):
        turn = voice_dialog.open_send_dialog("send a message to ravi", None)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.pending.stage, "platform")
        self.assertIn("whatsapp", turn.prompt.lower())

        turn = voice_dialog.open_send_dialog("whatsapp", turn.pending)
        self.assertEqual(turn.pending.stage, "message")

        turn = voice_dialog.open_send_dialog("I am running ten minutes late", turn.pending)
        self.assertEqual(turn.pending.stage, "confirm")
        # The read-back has to contain the message, or confirming is a formality.
        self.assertIn("ten minutes late", turn.prompt)
        self.assertIn("ravi", turn.prompt.lower())
        self.assertFalse(turn.ready, "sent before the user said yes")

        turn = voice_dialog.open_send_dialog("yes", turn.pending)
        self.assertTrue(turn.ready)
        self.assertIn("ravi", turn.instruction.lower())
        self.assertIn("ten minutes late", turn.instruction)
        self.assertIsNone(turn.pending, "the dialog stayed open after sending")

    def test_no_at_the_confirmation_does_not_send(self):
        turn = voice_dialog.open_send_dialog(
            'whatsapp ravi saying "on my way"', None
        )
        self.assertEqual(turn.pending.stage, "confirm")
        turn = voice_dialog.open_send_dialog("no", turn.pending)
        self.assertTrue(turn.cancelled)
        self.assertFalse(turn.ready)
        self.assertIsNone(turn.pending)

    def test_cancel_words_abandon_the_send(self):
        turn = voice_dialog.open_send_dialog("send ravi a message", None)
        turn = voice_dialog.open_send_dialog("never mind", turn.pending)
        self.assertTrue(turn.cancelled)
        self.assertIsNone(turn.pending)

    def test_unsayable_name_triggers_a_spelling_request(self):
        """And the spelling is understood as a name, not as six words."""
        turn = voice_dialog.open_send_dialog(
            "send a whatsapp to brtsch saying hi", None
        )
        self.assertIn("spell", turn.prompt.lower())
        self.assertEqual(turn.pending.stage, "recipient")
        self.assertIsNone(
            turn.pending.recipient, "the unusable name was kept, so the spelling cannot land"
        )

        turn = voice_dialog.open_send_dialog("S-R-I-N-U", turn.pending)
        self.assertEqual(turn.pending.recipient, "Srinu")
        self.assertEqual(turn.pending.stage, "confirm")

    def test_spelled_letters_are_read_as_one_word(self):
        self.assertEqual(voice_dialog.parse_spelled_letters("R-A-V-I"), "Ravi")
        self.assertEqual(voice_dialog.parse_spelled_letters("r a v i"), "Ravi")
        self.assertEqual(voice_dialog.parse_spelled_letters("it is A-K-A-N-S-H-A"), "Akansha")
        # Ordinary speech is not a spelling. Two single letters happen by accident
        # ("a bit", "I know"); three in a row essentially do not.
        self.assertIsNone(voice_dialog.parse_spelled_letters("send it now"))
        self.assertIsNone(voice_dialog.parse_spelled_letters("a bit later"))

    def test_talking_about_messages_is_not_a_request_to_send_one(self):
        """The expensive failure is the false positive.

        Hijacking an unrelated turn with "which app should I send it on?" is worse
        than missing a real request, because the user has to fight their way back
        out of a dialog they never opened. `search for the best email marketing
        tools` contains `email`; a bare keyword test opens a send on it.
        """
        for utterance in (
            "search for the best email marketing tools",
            "what did ravi text me",
            "did you send it yet",
            "who messaged me",
            "show me my email",
            "open youtube",
        ):
            with self.subTest(utterance=utterance):
                self.assertIsNone(voice_dialog.start_send_dialog(utterance))

    def test_polite_phrasing_is_still_a_request(self):
        """"Can you send Ravi a message" is not a question about her abilities."""
        for utterance in (
            "can you send ravi a message",
            "please text ravi that I am late",
            "hey akansha message ravi on telegram",
        ):
            with self.subTest(utterance=utterance):
                pending = voice_dialog.start_send_dialog(utterance)
                self.assertIsNotNone(pending)
                self.assertEqual((pending.recipient or "").lower(), "ravi")

    def test_the_body_marker_bounds_the_name(self):
        """One split point for both, so the name cannot contain the message."""
        pending = voice_dialog.start_send_dialog(
            'send a telegram to ravi kumar saying "running late"'
        )
        self.assertEqual(pending.platform, "telegram")
        self.assertEqual(pending.recipient, "ravi kumar")
        self.assertEqual(pending.message, "running late")

    def test_an_abandoned_dialog_does_not_swallow_a_later_yes(self):
        """A confirmation is only valid while the question is still in the air.

        Otherwise "yes" minutes later sends something the user has forgotten
        agreeing to -- which is precisely the failure the confirmation exists to
        prevent, arriving through the confirmation itself.
        """
        turn = voice_dialog.open_send_dialog(
            'whatsapp ravi saying "on my way"', None, now=1_000.0
        )
        stale = turn.pending
        self.assertTrue(stale.is_stale(1_000.0 + voice_dialog.PENDING_TTL_SECONDS + 1))
        resumed = voice_dialog.open_send_dialog(
            "yes", stale, now=1_000.0 + voice_dialog.PENDING_TTL_SECONDS + 1
        )
        self.assertIsNone(resumed, "a stale dialog answered a yes and sent")

    def test_repeated_misses_end_the_dialog_instead_of_looping(self):
        turn = voice_dialog.open_send_dialog("send ravi a message", None)
        for _ in range(voice_dialog.MAX_ATTEMPTS):
            if turn.pending is None:
                break
            turn = voice_dialog.open_send_dialog("mmhmm", turn.pending)
        self.assertIsNone(turn.pending, "the platform question loops forever")

    def test_stop_clears_a_half_built_send(self):
        """A pending send that survives "stop" is a trap."""
        engine_source = (PROJECT_ROOT / "backend/voice_engine.py").read_text(encoding="utf-8")
        start = engine_source.index("def _apply_send_dialog(")
        body = engine_source[start : engine_source.index("\n#: Engines are per session", start)]
        self.assertIn("if intent.is_stop_command:", body)
        self.assertIn("state.pending_send = None", body)

    def test_confirmed_send_is_planned_against_the_instruction_not_the_yes(self):
        """`req.transcript` at confirmation time is the word "yes".

        Planning automation against that produces a plan for the confirmation
        instead of for the thing confirmed, so the resolved instruction has to be
        what reaches the planner.
        """
        main_source = (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")
        self.assertIn(
            'automation_goal = intent.get("send_instruction") or req.transcript',
            main_source,
        )
        self.assertNotIn(
            "_agentic.create_plan(goal=req.transcript",
            main_source,
            "the planner still plans against the raw transcript",
        )

    def test_mid_dialog_turns_never_reach_the_model(self):
        """Given "who should I send it to?" the model answers it itself."""
        main_source = (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")
        self.assertIn('if intent.get("send_dialog_reply"):', main_source)
        branch = main_source[main_source.index('if intent.get("send_dialog_reply"):') :][:900]
        self.assertIn("_persist_assistant(send_reply)", branch)
        self.assertIn("return", branch)


class ChatPanelMemoTests(unittest.TestCase):
    """The five chat panels stay memoized, and their call sites stay stable.

    `memo` is easy to add and easy to silently neuter: pass one inline arrow at the
    call site and the props comparison fails on every render, so the wrapper skips
    nothing while still looking present in the diff. These tests pin both halves --
    the wrapper *and* the stable prop -- because only the pair does anything.

    The cost being avoided is concrete: ChatWorkspace re-renders whenever
    `onStatsChange` fires, and contextUnits is ceil(characters / 4), so a
    thousand-character answer re-rendered this subtree a couple of hundred times.
    """

    COMPONENT_DIR = "src/app/chat-interface/components"
    MEMOIZED = (
        "TaskCalendarPanel",
        "ConversationSidebar",
        "ContextPanel",
        "AvatarPanel",
        "PromptTemplateModal",
    )

    def _component(self, name: str) -> str:
        return (PROJECT_ROOT / f"{self.COMPONENT_DIR}/{name}.tsx").read_text(encoding="utf-8")

    def test_all_five_panels_are_wrapped_in_memo(self):
        for name in self.MEMOIZED:
            with self.subTest(component=name):
                source = self._component(name)
                self.assertIn(f"export default memo({name});", source)
                self.assertIn("memo", source.split("from 'react'")[0])
                # A bare `export default function` would mean the wrap was reverted
                # and the memo import left behind.
                self.assertNotIn(f"export default function {name}", source)

    def test_workspace_passes_stable_callbacks_to_memoized_children(self):
        workspace = self._component("ChatWorkspace")

        for callback in ("startNewChat", "closeContextPanel", "closePromptModal"):
            with self.subTest(callback=callback):
                self.assertIn(f"const {callback} = React.useCallback", workspace)

        self.assertIn("onNewChat={startNewChat}", workspace)
        self.assertIn("onClose={closeContextPanel}", workspace)
        self.assertIn("onClose={closePromptModal}", workspace)
        self.assertIn("onSelect={applyPromptTemplate}", workspace)
        # The inline arrows these replaced. Any one of them coming back makes the
        # corresponding memo inert.
        self.assertNotIn("onClose={() => setContextPanelOpen(false)}", workspace)
        self.assertNotIn("onClose={() => setIsPromptModalOpen(false)}", workspace)

    def test_chat_thread_passes_stable_callbacks_to_avatar_and_prompt_library(self):
        thread = self._component("ChatThread")

        for callback in (
            "toggleVoiceEnabled",
            "collapseAvatar",
            "closePromptLibrary",
            "applyPromptFromLibrary",
        ):
            with self.subTest(callback=callback):
                self.assertIn(f"const {callback} = useCallback", compact_source(thread))

        self.assertIn("onToggleVoice={toggleVoiceEnabled}", thread)
        self.assertIn("onToggleMinimize={collapseAvatar}", thread)
        self.assertIn("onClose={closePromptLibrary}", thread)
        self.assertIn("onSelect={applyPromptFromLibrary}", thread)
        self.assertNotIn("onToggleMinimize={() => setAvatarExpanded(false)}", thread)
        self.assertNotIn("onClose={() => setPromptModalOpen(false)}", thread)

    def test_voice_toggle_uses_the_updater_form_so_its_deps_stay_empty(self):
        """Reading `voiceEnabled` directly would put it in the dep list.

        That hands `AvatarPanel` a new `onToggleVoice` every time the mic flips,
        which is exactly the render the memo is supposed to absorb.
        """
        thread = compact_source(self._component("ChatThread"))
        start = thread.index("const toggleVoiceEnabled = useCallback")
        body = thread[start : start + 400]
        self.assertIn("setVoiceEnabled((previous) => {", body)
        self.assertNotIn("const next = !voiceEnabled;", body)

    def test_task_calendar_panel_takes_no_props_at_its_call_site(self):
        """The one unconditional win: nothing to compare, so it never re-renders.

        If a prop is ever added here it must be checked for stability, or this
        wrapper quietly stops working.
        """
        context_panel = self._component("ContextPanel")
        self.assertIn("<TaskCalendarPanel />", context_panel)


class GraphOwnerIdentityTests(unittest.TestCase):
    """The knowledge-graph owner comes from one resolver, not sixteen literals.

    The id is the graph's filename, so a hardcoded "default_user" at every call site
    meant that adding real identity later required finding all sixteen -- and a single
    miss would split one person's facts across two files, with the second one never
    read again. These tests pin the single definition and the fallback value.
    """

    def test_no_endpoint_hardcodes_the_owner_id(self):
        main_source = (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")
        # The docstring of the resolver names the old literal, so strip the resolver
        # before counting: the assertion is about call sites, not about prose.
        body = main_source.split("def resolve_graph_owner", 1)[1]
        body = body.split('"""', 2)[2]
        self.assertNotIn('"default_user"', body)

    def test_fallback_id_is_unchanged_so_the_existing_graph_is_not_orphaned(self):
        from backend.user_knowledge_graph import LOCAL_GRAPH_OWNER_ID, user_graph_path

        self.assertEqual(LOCAL_GRAPH_OWNER_ID, "default_user")
        self.assertEqual(
            user_graph_path(None).name,
            "default_user.akansha-graph.json",
            "renaming the fallback id abandons the graph already on disk",
        )

    def test_missing_or_invalid_authorization_falls_back_to_the_local_owner(self):
        from backend.main import resolve_graph_owner
        from backend.user_knowledge_graph import LOCAL_GRAPH_OWNER_ID

        for header in (None, "", "Basic abc", "Bearer", "Bearer   ", "Bearer forged.sig"):
            with self.subTest(authorization=header):
                self.assertEqual(resolve_graph_owner(header), LOCAL_GRAPH_OWNER_ID)

    def test_a_valid_bearer_token_resolves_to_that_account(self):
        from backend.auth_service import AuthService
        from backend.main import resolve_graph_owner

        token = AuthService.create_access_token(7, "owner@example.com")
        self.assertEqual(resolve_graph_owner(f"Bearer {token}"), "owner@example.com")

    def test_webhooks_use_the_constant_rather_than_a_caller_identity(self):
        """A platform-to-server callback structurally cannot carry a user token.

        Using the dependency there would imply an identity that never exists, so
        those sites name the constant instead.
        """
        main_source = (PROJECT_ROOT / "backend/main.py").read_text(encoding="utf-8")
        start = main_source.index("async def receive_social_webhook")
        # Bound on the next route decorator, not on a character count -- a fixed
        # window spilled into send_social_reply and failed on that function's
        # legitimate use of the dependency.
        webhook = main_source[start : main_source.index("\n@app.", start)]
        self.assertIn("LOCAL_GRAPH_OWNER_ID", webhook)
        self.assertNotIn("graph_owner", webhook)

    def test_authenticated_and_anonymous_requests_read_different_graphs(self):
        from fastapi.testclient import TestClient

        from backend.auth_service import AuthService
        from backend.main import app

        client = TestClient(app)
        anonymous = Path(client.get("/api/knowledge/graph").json()["path"]).name
        token = AuthService.create_access_token(7, "owner@example.com")
        authenticated = Path(
            client.get(
                "/api/knowledge/graph", headers={"Authorization": f"Bearer {token}"}
            ).json()["path"]
        ).name

        self.assertEqual(anonymous, "default_user.akansha-graph.json")
        self.assertEqual(authenticated, "owner@example.com.akansha-graph.json")
        # An explicit query parameter still wins over both.
        override = Path(client.get("/api/knowledge/graph?user_id=someone-else").json()["path"]).name
        self.assertEqual(override, "someone-else.akansha-graph.json")


class WakeWordTests(unittest.TestCase):
    """Regression coverage for `src/lib/wakeWord.ts`.

    This module is the only piece of the voice path that can be tested honestly
    without a microphone: it takes a transcript string and returns a decision,
    with no audio, no network and no browser. Everything downstream of it depends
    on that decision being right, and until now it had no test at all -- its own
    header claimed "the tests in `backend/test_audit_regressions.py` exercise it",
    which was false.

    The tests run the real TypeScript rather than a Python re-implementation. A
    translated copy of the algorithm would pass while the shipped file was broken,
    which is worse than no test: it reports safety it cannot see. Node 24 strips
    types natively, so the module is imported exactly as the browser bundle
    imports it, through one Node process for the whole table.

    Writing these found three real defects, all now fixed and pinned below:

    1. Every Telugu and Hindi greeting in `GREETINGS` was dead. Normalisation
       kept `\\p{L}` and `\\p{N}` but not `\\p{M}`, so the virama in "ఏయ్" and the
       vowel sign in "హే" were stripped before the lookup, and the set was keyed
       on the un-stripped spellings. Four entries that could never match.
    2. "ok akansha" was not recognised as a bare call while "hey akansha" was.
       The two-token join produced "okakansha", which is within the edit-distance
       limit, so the greeting was swallowed into the matched name. "heyakansha"
       only escaped by being three characters too long.
    3. `isBareWakeCall` recovered the leading text with
       `normalized.indexOf(match.matchedToken)`. For the joined forms that token
       never appears literally, so `indexOf` returned -1 and `slice(0, -1)` handed
       back the whole string minus its last character instead of an empty prefix.
    """

    WAKE_WORD_SOURCE = Path(__file__).resolve().parents[1] / "src" / "lib" / "wakeWord.ts"

    # Mishearings this project's own recogniser has produced for the name. The
    # module's header names them as the reason it matches by edit distance instead
    # of by equality, so they are the exact claim worth pinning.
    MISHEARINGS = (
        "akansha",
        "akanksha",
        "akansa",
        "akasha",
        "akansh",
        "aakansha",
        "akanchaa",
        "aakanshaa",
        "a kansha",
        "aa kansha",
    )

    # A greeting in front of the name, in all four languages the set claims to
    # support. Each of these is someone calling the assistant and stopping.
    GREETED = (
        "hey akansha",
        "hi akansha",
        "ok akansha",
        "okay akansha",
        "hai akansha",
        "ఏయ్ akansha",
        "హే akansha",
        "अरे akansha",
        "हे akansha",
    )

    # Ordinary speech. A wake word that fires on any of these is worse than one
    # that misses, because it takes the microphone away mid-sentence.
    NON_MATCHES = (
        "hello",
        "computer",
        "what is the weather",
        "thanks",
        "telugu",
        "kansas",
        "alaska",
        "asana",
        "answer",
        "another",
        "a",
        "",
    )

    NODE_DRIVER = """
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [modulePath, casesPath] = process.argv.slice(2);
const wake = await import(pathToFileURL(modulePath).href);
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

process.stdout.write(
  JSON.stringify(
    cases.map((text) => {
      const match = wake.matchWakeWord(text);
      return {
        text,
        matched: match.matched,
        remainder: match.remainder,
        matchedToken: match.matchedToken,
        leading: match.leading,
        bare: wake.isBareWakeCall(text),
        stripped: wake.stripWakeWord(text),
        normalized: wake.normalizeForWakeWord(text),
      };
    })
  )
);
"""

    results: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls):
        """Evaluate the whole case table in one Node process.

        One process, not one per case: Node costs ~150ms to start and there are
        forty-odd inputs, so per-case invocation would add seconds to the suite
        for no extra coverage.
        """
        node = shutil.which("node")
        if not node:
            # An honest skip. This proves nothing about the wake word -- it says
            # the runner had no Node. `test_source_pins_the_tuned_constants` below
            # does not need Node and still runs.
            raise unittest.SkipTest("node is not on PATH, so the TypeScript module cannot be run")

        cases = list(cls.MISHEARINGS) + list(cls.GREETED) + list(cls.NON_MATCHES) + [
            "hey akansha open chrome",
            "Hey, Akansha! open chrome.",
            "so i told akansha",
            "so i told akansha about it",
            "akansha    open     notepad",
        ]

        with tempfile.TemporaryDirectory() as workdir:
            driver = Path(workdir) / "driver.mjs"
            payload = Path(workdir) / "cases.json"
            driver.write_text(cls.NODE_DRIVER, encoding="utf-8")
            payload.write_text(json.dumps(cases), encoding="utf-8")
            completed = subprocess.run(
                [
                    node,
                    "--no-warnings",
                    str(driver),
                    str(cls.WAKE_WORD_SOURCE),
                    str(payload),
                ],
                capture_output=True,
            )

        if completed.returncode != 0:
            raise AssertionError(
                "node failed to run wakeWord.ts:\n"
                + completed.stderr.decode("utf-8", "replace")
            )

        cls.results = {
            row["text"]: row for row in json.loads(completed.stdout.decode("utf-8"))
        }

    def outcome(self, text: str) -> dict:
        self.assertIn(text, self.results, "case was not evaluated by the Node driver")
        return self.results[text]

    def test_documented_mishearings_all_wake_the_assistant(self):
        for heard in self.MISHEARINGS:
            with self.subTest(heard=heard):
                result = self.outcome(heard)
                self.assertTrue(
                    result["matched"],
                    "this is a transcription the recogniser really produces for the name",
                )
                self.assertTrue(
                    result["bare"],
                    "the name alone is a bare call and must be answered, not executed",
                )
                self.assertEqual(result["remainder"], "")

    def test_greetings_in_both_languages_still_count_as_a_bare_call(self):
        for heard in self.GREETED:
            with self.subTest(heard=heard):
                result = self.outcome(heard)
                self.assertTrue(result["matched"])
                self.assertTrue(
                    result["bare"],
                    "a greeting in front of the name is still just being called; for the "
                    "Telugu and Hindi entries this fails if normalisation drops combining marks",
                )

    def test_ordinary_speech_does_not_wake_the_assistant(self):
        for heard in self.NON_MATCHES:
            with self.subTest(heard=heard):
                result = self.outcome(heard)
                self.assertFalse(
                    result["matched"],
                    "a false wake takes the microphone away mid-sentence",
                )
                self.assertFalse(result["bare"])

    def test_command_after_the_wake_phrase_is_extracted_and_the_phrase_stripped(self):
        for heard in ("hey akansha open chrome", "Hey, Akansha! open chrome."):
            with self.subTest(heard=heard):
                result = self.outcome(heard)
                self.assertTrue(result["matched"])
                self.assertEqual(result["remainder"], "open chrome")
                # The intent router must never see the wake words, or it looks for
                # an app called "hey".
                self.assertEqual(result["stripped"], "open chrome")
                self.assertFalse(
                    result["bare"],
                    "a call with a command attached should execute it, not answer 'yes?'",
                )

        # Collapsed whitespace, so the router gets a single-spaced command.
        self.assertEqual(self.outcome("akansha    open     notepad")["remainder"], "open notepad")

    def test_the_name_mid_sentence_is_not_an_address(self):
        # Matching anywhere in the transcript is deliberate -- continuous
        # recognition opens with fragments -- but "I told Akansha" is speech
        # *about* the assistant, not to it.
        spoken_about = self.outcome("so i told akansha")
        self.assertTrue(spoken_about["matched"])
        self.assertFalse(
            spoken_about["bare"],
            "the name was incidental; treating this as an address hijacks the mic",
        )
        self.assertEqual(spoken_about["leading"], "so i told")
        self.assertEqual(self.outcome("so i told akansha about it")["remainder"], "about it")

    def test_greeting_is_not_absorbed_into_the_name_token(self):
        result = self.outcome("ok akansha")
        self.assertEqual(
            result["matchedToken"],
            "akansha",
            "'ok' + 'akansha' joins to 'okakansha', which is within the distance limit; "
            "the join must skip a leading greeting or two spellings of one utterance diverge",
        )
        self.assertEqual(result["leading"], "ok")
        # The asymmetry that hid the bug: "hey" is long enough that the joined
        # form fails the length guard on its own.
        self.assertEqual(self.outcome("hey akansha")["matchedToken"], "akansha")

    def test_leading_is_carried_rather_than_recovered_from_the_matched_token(self):
        split = self.outcome("a kansha")
        self.assertEqual(split["matchedToken"], "akansha")
        self.assertEqual(
            split["leading"],
            "",
            "the joined token does not appear literally in the transcript, so an "
            "indexOf-based prefix silently returned everything but the last character",
        )
        self.assertTrue(split["bare"])

    def test_normalization_keeps_combining_marks(self):
        normalized = self.outcome("ఏయ్ akansha")["normalized"]
        self.assertIn(
            "ఏయ్",
            normalized,
            "the virama is \\p{M}, not \\p{L}; stripping it makes the greeting unmatchable",
        )

    def test_source_pins_the_tuned_constants(self):
        """Runs without Node, so the invariants stay covered on a bare runner."""
        source = self.WAKE_WORD_SOURCE.read_text(encoding="utf-8")
        compact = compact_source(source)

        # Two, not three: at three the distance-2 neighbourhood of a seven-letter
        # target starts to include ordinary words.
        self.assertIn("const MAX_NAME_DISTANCE = 2;", compact)
        # Six, because distance-2 matching would otherwise accept five-letter noise.
        self.assertIn("const MIN_NAME_LENGTH = 6;", compact)
        self.assertIn(r"[^\p{L}\p{N}\p{M}\s]+", compact)
        self.assertIn("if (next && !GREETINGS.has(token))", compact)
        self.assertNotIn("indexOf(match.matchedToken)", compact)


class SpeakerIdentityTests(unittest.TestCase):
    """Who the assistant thinks it is talking to, and what that answer permits.

    These exist because of a measured failure, not a review comment. Before
    `backend/speaker_identity.py`, `_chat_speaker_profile` merged the request's
    claimed `speaker_profile` on top of the owner's and then tried to derive
    `access_level` behind `if not merged.get("access_level")`. That guard could
    never fire -- the owner defaults always set `access_level` -- so the derived
    value was computed and thrown away. Probed against the running app, every
    claim came back identical::

        relationship 'friend' -> access=owner   closeness=close
        relationship None     -> access=owner   closeness=close
        relationship 'guest'  -> access=owner   closeness=close
        relationship 'owner'  -> access=owner   closeness=close

    A request that said "I am a guest" received owner authority *and* the owner's
    bio, education, current project and running conversation summary, because the
    merge handed the claim every field it had not overridden.

    What is verified here is authority, not identity. There is no voiceprint, no
    speaker embedding, no face check and no password in this codebase -- the
    `voice_signature` column is stored and never compared against anything. So the
    honest claim is that a *self-declared* relationship no longer grants owner
    rights, and `test_every_resolved_profile_admits_it_is_unverified` is what stops
    a later reader from mistaking that for authentication.
    """

    def _owner_defaults(self) -> dict:
        """A stand-in for `_default_owner_speaker_profile`, with the private fields set.

        Hand-built rather than read from the database so the leak assertions have
        something to detect: if the local profile happened to have an empty bio, a
        test asserting "the owner's bio did not leak" would pass without the fix.
        """

        return {
            "display_name": "Owner Name",
            "relationship_to_owner": "owner",
            "access_level": "owner",
            "closeness_level": "close",
            "communication_style": "proactive close companion",
            "language_preference": "telugu",
            "notes": "Owner bio: final-year student, lives in Hyderabad.",
            "context_profile": {"education": "B.Tech", "project": "Akansha"},
            "conversation_summary": "Owner has been debugging the voice pipeline.",
        }

    def test_a_claimed_relationship_does_not_grant_owner_authority(self):
        from backend.speaker_identity import resolve_speaker_identity

        owner = self._owner_defaults()
        # Left column is what the request claims; right is what it may have. The
        # regression is that every one of these used to come back as "owner".
        expected = {
            "owner": "owner",
            "self": "owner",
            "mother": "trusted",
            "amma": "trusted",
            "friend": "trusted",
            "guest": "guest",
            "professor": "guest",
            "teacher": "guest",
            "": "guest",
            "some stranger": "guest",
        }
        for relationship, level in expected.items():
            with self.subTest(relationship=relationship):
                resolved = resolve_speaker_identity(
                    {"relationship_to_owner": relationship}, owner
                )
                self.assertEqual(resolved["access_level"], level)

    def test_a_body_supplied_access_level_is_ignored(self):
        """The claim may not name its own authority.

        This is the exact shape the old code was vulnerable to: the request said
        `access_level: "owner"` and the merge put it straight through.
        """

        from backend.speaker_identity import resolve_speaker_identity

        owner = self._owner_defaults()
        for relationship in ("guest", "professor", "", "some stranger"):
            with self.subTest(relationship=relationship):
                resolved = resolve_speaker_identity(
                    {"relationship_to_owner": relationship, "access_level": "owner"}, owner
                )
                self.assertEqual(resolved["access_level"], "guest")

    def test_owner_private_context_does_not_reach_a_non_owner_profile(self):
        from backend.speaker_identity import (
            owner_context_leaked_into,
            resolve_speaker_identity,
        )

        owner = self._owner_defaults()
        for relationship in ("guest", "friend", "professor", "some stranger"):
            with self.subTest(relationship=relationship):
                resolved = resolve_speaker_identity(
                    {"relationship_to_owner": relationship, "display_name": "Visitor"}, owner
                )
                leaked = owner_context_leaked_into(resolved, owner)
                self.assertEqual(
                    leaked,
                    [],
                    f"owner's {', '.join(leaked)} reached a '{relationship}' profile",
                )

    def test_no_claim_still_resolves_to_the_owner(self):
        """Hardening must not lock out the person sitting at the machine.

        The shipped frontend sends no `speaker_profile` on any request, so if a
        missing claim resolved to `guest` this fix would break every real turn while
        looking correct in isolation.
        """

        from backend.speaker_identity import LOCAL_SESSION, resolve_speaker_identity

        owner = self._owner_defaults()
        for claim in (None, {}):
            with self.subTest(claim=claim):
                resolved = resolve_speaker_identity(claim, owner)
                self.assertEqual(resolved["access_level"], "owner")
                self.assertEqual(resolved["identification_method"], LOCAL_SESSION)
                # The owner's own context is theirs to keep.
                self.assertEqual(resolved["notes"], owner["notes"])

    def test_every_resolved_profile_admits_it_is_unverified(self):
        """Nothing downstream may mistake a self-declaration for a verified identity."""

        from backend.speaker_identity import (
            LOCAL_SESSION,
            SELF_DECLARED,
            resolve_speaker_identity,
        )

        owner = self._owner_defaults()
        cases = [
            (None, LOCAL_SESSION),
            ({"relationship_to_owner": "owner"}, SELF_DECLARED),
            ({"relationship_to_owner": "friend"}, SELF_DECLARED),
            ({"relationship_to_owner": "guest"}, SELF_DECLARED),
        ]
        for claim, method in cases:
            with self.subTest(claim=claim):
                resolved = resolve_speaker_identity(claim, owner)
                self.assertIs(resolved["identity_verified"], False)
                self.assertEqual(resolved["identification_method"], method)

    def test_closeness_may_be_claimed_but_authority_may_not(self):
        """Warmth and rights are separate axes.

        A guest is allowed to ask to be spoken to casually. Conflating that with
        access is how a friendly-sounding request ends up holding owner rights.
        """

        from backend.speaker_identity import resolve_speaker_identity

        owner = self._owner_defaults()
        resolved = resolve_speaker_identity(
            {"relationship_to_owner": "guest", "closeness_level": "close"}, owner
        )
        self.assertEqual(resolved["closeness_level"], "close")
        self.assertEqual(resolved["access_level"], "guest")

    def test_the_chat_helper_and_the_standalone_helper_cannot_drift(self):
        """`_speaker_access_level` used to hold its own copy of the alias table.

        Two tables meant two answers: the chat path and this helper disagreed about
        whether "teacher" was a relationship at all. The helper now delegates, so
        this is what fails if somebody re-inlines the table.
        """

        from backend.speaker_identity import (
            RELATIONSHIP_ALIASES,
            access_level_for_relationship,
        )

        words = set(RELATIONSHIP_ALIASES) | set(RELATIONSHIP_ALIASES.values())
        words |= {"guest", "professor", "sister", "", "unknown word"}
        for word in sorted(words):
            with self.subTest(relationship=word):
                self.assertEqual(
                    _speaker_access_level(word), access_level_for_relationship(word)
                )

    def test_the_dead_guard_that_masked_this_is_gone(self):
        source = compact_source((PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8"))

        # The guard itself. Unreachable, because the merge base always sets the key.
        self.assertNotIn('if not merged.get("access_level")', source)
        # And the merge that made it unreachable while also leaking owner context.
        self.assertNotIn("{**_default_owner_speaker_profile(db), **req.speaker_profile}", source)

    def test_save_voice_speaker_derives_the_stored_access_level(self):
        """A stored speaker's authority comes from its relationship, not the request body.

        Source-level rather than a live POST because the endpoint writes to the real
        speaker table; what matters is that the body's `access_level` is never read.
        """

        source = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        start = source.index("def save_voice_speaker(")
        function = source[start : source.index("@app.", start + 1)]
        # Comments stripped before the `assertNotIn`, because the comment above the
        # fix quotes the expression it replaced. Without this the test fails on its
        # own explanation -- which is the wrong thing to make somebody delete.
        code = "\n".join(
            line for line in function.splitlines() if not line.strip().startswith("#")
        )
        body = compact_source(code)

        self.assertIn("access_level = access_level_for_relationship(relationship)", body)
        # The old expression, which let the caller name its own level.
        self.assertNotIn("req.access_level", body)

    def test_desktop_automation_requires_owner_access(self):
        """The reason any of this matters.

        `analyze_intent_and_memory` runs as a background task from the chat
        endpoints and calls `execute_desktop_command`, so a chat turn can launch a
        real application on this machine. The action is chosen by a model reading the
        conversation, which makes "who asked for this" a question that has to be
        answered before the launch rather than after.

        `execute_desktop_command` is replaced here, and the replacement is asserted
        into place before the call: a bug in this test must not open Notepad.
        """

        payload = {
            "memories_to_add": [],
            "memories_to_update": [],
            "new_tasks": [],
            "automation": {"action": "open_notepad", "target": "regression-test"},
        }
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: completion)
            )
        )

        class _Query:
            def all(self):
                return []

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return None

        class _Session:
            def query(self, *args, **kwargs):
                return _Query()

            def add(self, obj):
                pass

            def commit(self):
                pass

        launched = []

        async def _record(action, target=None):
            launched.append((action, target))
            return {"status": "recorded-by-test"}

        cases = [
            (None, True, "no claim is the local desktop session"),
            ({}, True, "empty profile falls back to the local session"),
            ({"access_level": "owner"}, True, "owner"),
            ({"access_level": "trusted"}, False, "trusted"),
            ({"access_level": "guest"}, False, "guest"),
        ]
        for profile, should_launch, label in cases:
            with self.subTest(speaker=label):
                launched.clear()
                with patch.object(
                    automation, "execute_desktop_command", _record
                ), patch.object(
                    ai_engine_module, "_openrouter_client", lambda: fake_client
                ), patch.object(
                    ai_engine_module, "_capture_deterministic_memories", lambda db, text: None
                ), patch.object(
                    ai_engine_module, "_should_skip_ai_memory_analysis", lambda a, b: False
                ):
                    self.assertIs(
                        automation.execute_desktop_command,
                        _record,
                        "executor not patched -- refusing to exercise the real one",
                    )
                    ai_engine_module.analyze_intent_and_memory(
                        _Session(), "open notepad", "Opening notepad.", profile
                    )
                self.assertEqual(bool(launched), should_launch)

class AppConnectTests(unittest.TestCase):
    """One registry, one dispatcher, and no button that lies about being connected.

    Before `backend/app_connect.py` there was no single answer to "what can Akansha
    connect to, how, and is it connected right now". Social platforms went through
    `/api/social/setup/{platform}` against a hardcoded five-entry catalog; desktop
    apps were launch commands in `automation.py` with no concept of connection at
    all; model providers were a third surface, and `/api-keys` turned out to be
    display-only -- hardcoded placeholder strings, a reveal toggle that unmasks a
    dummy, and an add button that adds nothing.

    The property worth defending is that a click never answers with a bare boolean.
    "Not connected" is useless: the user cannot tell a missing credential from an
    uninstalled application from a self-hosted bridge that is not running, and only
    the first of those three is fixable by typing. So every branch returns one of
    six named outcomes, and `test_no_branch_returns_a_bare_failure` is what stops a
    later shortcut from collapsing them.

    The probes are injected rather than imported. A test that shells out to find
    Chrome reports what this machine happens to have installed; it does not test
    this code.
    """

    def _connector(self, app_id: str):
        from backend.app_connect import APP_CONNECTORS

        connector = APP_CONNECTORS.get(app_id)
        self.assertIsNotNone(connector, f"registry lost '{app_id}'")
        return connector

    def test_every_entry_declares_a_known_strategy_and_the_fields_it_needs(self):
        from backend.app_connect import (
            API_KEY,
            APP_CONNECTORS,
            LOCAL_BRIDGE,
            LOCAL_EXECUTABLE,
            OAUTH2,
            STRATEGIES,
        )

        for app_id, connector in APP_CONNECTORS.items():
            with self.subTest(app=app_id):
                self.assertIn(connector.strategy, STRATEGIES)
                # A credential strategy with no fields would render an empty form and
                # then report connected, which is the exact lie this module exists to
                # prevent.
                if connector.strategy in (API_KEY, OAUTH2, LOCAL_BRIDGE):
                    self.assertTrue(connector.fields, f"{app_id} needs credential fields")
                # An executable strategy with no launch key would probe for "".
                if connector.strategy == LOCAL_EXECUTABLE:
                    self.assertTrue(connector.launch_key, f"{app_id} needs a launch key")
                    self.assertFalse(connector.fields, f"{app_id} cannot need credentials")
                self.assertTrue(connector.capabilities, f"{app_id} declares no capabilities")

    def test_every_desktop_entry_points_at_a_real_launcher_key(self):
        """The registry and `automation` must not disagree about what an app is called.

        A launch key with no entry in `APP_LAUNCH_COMMANDS` or
        `APP_REGISTRY_EXECUTABLES` would probe for a bare name, so the card would
        read `not_installed` for an app that is installed -- or worse, resolve
        something unintended off PATH.
        """

        from backend.app_connect import APP_CONNECTORS, LOCAL_EXECUTABLE
        from backend.automation import APP_LAUNCH_COMMANDS, APP_REGISTRY_EXECUTABLES

        known = set(APP_LAUNCH_COMMANDS) | set(APP_REGISTRY_EXECUTABLES)
        for app_id, connector in APP_CONNECTORS.items():
            if connector.strategy != LOCAL_EXECUTABLE:
                continue
            with self.subTest(app=app_id):
                self.assertIn(connector.launch_key, known)

    def test_a_spoken_app_name_resolves_to_the_same_entry_as_a_click(self):
        """"open whats app" and the WhatsApp card have to reach one connector.

        The mishearings here are the same class the wake word deals with: the
        recogniser adds and splits words far more often than it drops them.
        """

        from backend.app_connect import resolve_app_id

        expected = {
            "chrome": "chrome",
            "Google Chrome": "chrome",
            "chrome browser": "chrome",
            "whats app": "whatsapp_desktop",
            "whatsup": "whatsapp_desktop",
            "WhatsApp Desktop": "whatsapp_desktop",
            "visual studio code": "vscode",
            "openrouter": "openrouter",
            "Ollama (local models)": "ollama",
            "notion workspace": "notion",
        }
        for spoken, app_id in expected.items():
            with self.subTest(said=spoken):
                self.assertEqual(resolve_app_id(spoken), app_id)

        for nonsense in ("", None, "   ", "definitely not an app"):
            with self.subTest(said=nonsense):
                self.assertIsNone(resolve_app_id(nonsense))

    def test_a_missing_executable_is_not_reported_as_a_missing_credential(self):
        """The two failures are not interchangeable.

        `needs_input` is answerable by typing. `not_installed` is not answerable by
        this application at all. Collapsing them is what makes an integrations page
        useless.
        """

        from backend.app_connect import CONNECTED, NOT_INSTALLED, plan_connect

        connector = self._connector("chrome")

        absent = plan_connect(connector, {}, resolve_executable=lambda key: "")
        self.assertEqual(absent.outcome, NOT_INSTALLED)
        self.assertEqual(absent.required, [], "an uninstalled app must not ask for a credential")

        found = plan_connect(
            connector, {}, resolve_executable=lambda key: r"C:\fake\chrome.exe"
        )
        self.assertEqual(found.outcome, CONNECTED)
        self.assertEqual(found.connected_to, r"C:\fake\chrome.exe")

    def test_a_partly_filled_form_names_the_fields_still_missing(self):
        """The credential route, exercised where it is genuinely the only route.

        `whatsapp_bridge` now also carries a browser login, and a connector with a
        browser login deliberately reports `needs_sign_in` first -- see
        `test_a_website_beats_a_client_secret_as_the_first_route`. What is under test
        here is the field reporting itself, so the browser route is cleared rather
        than the assertion being weakened: the fields must still be named, and named
        with somewhere to find them, for every connector that has no other way in.
        """
        import dataclasses

        from backend.app_connect import CONNECTED, NEEDS_INPUT, plan_connect

        connector = dataclasses.replace(self._connector("whatsapp_bridge"), web_url="")

        empty = plan_connect(connector, {})
        self.assertEqual(empty.outcome, NEEDS_INPUT)
        self.assertEqual([field["key"] for field in empty.required], ["base_url"])
        # `api_key` on this connector is optional, so it must not be demanded.
        self.assertNotIn("api_key", [field["key"] for field in empty.required])

        # Every required field named, with somewhere to find it -- the reason
        # integration pages stall is that the user cannot locate the value.
        for field in empty.required:
            self.assertTrue(field["label"])
            self.assertTrue(field["where"])

        filled = plan_connect(
            connector, {"base_url": "http://localhost:8080"}, http_ok=lambda url: True
        )
        self.assertEqual(filled.outcome, CONNECTED)

    def test_a_blank_stored_value_counts_as_missing(self):
        """Submitting the form without filling it in must not report success.

        A stored empty string is what that produces, and treating it as present is
        how an app reports connected and then fails on first use.
        """

        from backend.app_connect import NEEDS_INPUT, missing_fields, plan_connect

        connector = self._connector("openrouter")
        for stored in ({}, {"api_key": ""}, {"api_key": "   "}, {"api_key": None}):
            with self.subTest(stored=stored):
                self.assertEqual(missing_fields(connector, stored), ["api_key"])
                self.assertEqual(plan_connect(connector, stored).outcome, NEEDS_INPUT)

    def test_a_configured_bridge_that_is_not_running_says_so(self):
        """"Unreachable" and "wrong value" are different problems.

        The address is already saved, so the fix is starting the server -- telling
        the user to re-check their credentials would send them the wrong way.
        """

        from backend.app_connect import CONNECTED, UNREACHABLE, plan_connect

        connector = self._connector("ollama")
        stored = {"base_url": "http://localhost:11434"}

        probed = []

        def _http(url):
            probed.append(url)
            return False

        down = plan_connect(connector, stored, http_ok=_http)
        self.assertEqual(down.outcome, UNREACHABLE)
        self.assertEqual(down.required, [], "the address is stored; do not ask for it again")
        # Probed at the health path, not the bare base URL -- a base URL that 404s is
        # still a running server.
        self.assertEqual(probed, ["http://localhost:11434/api/tags"])

        up = plan_connect(connector, stored, http_ok=lambda url: True)
        self.assertEqual(up.outcome, CONNECTED)
        self.assertEqual(up.connected_to, "http://localhost:11434")

    def test_oauth_asks_for_registration_first_and_consent_second(self):
        """Two steps that people conflate, reported separately.

        Consent cannot be requested before an app registration exists, and "not
        connected" gives no hint which of the two is outstanding.

        The browser route is cleared for the first assertion only. Google now offers
        a one-click login, which deliberately outranks "go and register an
        application" -- but a person who *has* registered one must still get the
        registration/consent distinction rather than a generic failure, and that is
        what the rest of this exercises.
        """

        import dataclasses

        from backend.app_connect import (
            CONNECTED,
            NEEDS_AUTHORIZATION,
            NEEDS_INPUT,
            plan_connect,
        )

        connector = self._connector("google")

        unregistered = plan_connect(dataclasses.replace(connector, web_url=""), {})
        self.assertEqual(unregistered.outcome, NEEDS_INPUT)
        self.assertEqual(
            sorted(field["key"] for field in unregistered.required),
            ["client_id", "client_secret"],
        )

        registered = plan_connect(
            connector, {"client_id": "cid", "client_secret": "sec"}
        )
        self.assertEqual(registered.outcome, NEEDS_AUTHORIZATION)
        # Reuses the OAuth flow that already exists rather than inventing a route.
        self.assertEqual(registered.authorize_url, "/api/social/oauth/start/google")

        authorized = plan_connect(
            connector,
            {"client_id": "cid", "client_secret": "sec"},
            has_token=True,
            account_label="owner@example.com",
        )
        self.assertEqual(authorized.outcome, CONNECTED)
        self.assertEqual(authorized.connected_to, "owner@example.com")

    def test_no_branch_returns_a_bare_failure(self):
        """Every outcome is one of the six named ones, and carries something to act on.

        This is the invariant the whole module exists for. A later shortcut that
        returns a generic failure passes every other test in this class and breaks
        the only property that made the surface usable.
        """

        from backend.app_connect import (
            APP_CONNECTORS,
            NEEDS_AUTHORIZATION,
            NEEDS_INPUT,
            OUTCOMES,
            plan_connect,
        )

        for app_id, connector in APP_CONNECTORS.items():
            for stored in ({}, {field.key: "value" for field in connector.fields}):
                with self.subTest(app=app_id, stored=bool(stored)):
                    result = plan_connect(connector, stored)
                    self.assertIn(result.outcome, OUTCOMES)
                    self.assertTrue(result.detail, "every outcome needs a sentence")
                    if result.outcome == NEEDS_INPUT:
                        self.assertTrue(result.required)
                    if result.outcome == NEEDS_AUTHORIZATION:
                        self.assertTrue(result.authorize_url)

    def test_the_catalog_can_answer_without_touching_the_network(self):
        """`probe=False` is what the voice path uses.

        Answering "what can you connect to" out loud must not fire a dozen HTTP
        requests and a registry sweep for an answer that does not change between
        restarts.
        """

        from backend.app_connect import APP_CONNECTORS, catalog

        calls = []
        declarative = catalog(
            probe=False,
            resolve_executable=lambda key: calls.append(key) or "",
            http_ok=lambda url: bool(calls.append(url)),
        )
        self.assertEqual(calls, [])
        self.assertEqual(len(declarative["apps"]), len(APP_CONNECTORS))
        for entry in declarative["apps"]:
            self.assertNotIn("status", entry)

        probed = catalog(resolve_executable=lambda key: "", http_ok=lambda url: False)
        for entry in probed["apps"]:
            self.assertIn("status", entry)

    def test_a_secret_field_is_never_marked_public(self):
        """The catalog is readable without owner access, so it must carry no secrets.

        It ships field *descriptions*, never values. This pins the `secret` flag on
        anything that is one, so a UI built off the catalog cannot render an API key
        as a plain-text input.
        """

        from backend.app_connect import APP_CONNECTORS, catalog

        entries = {entry["app_id"]: entry for entry in catalog(probe=False)["apps"]}
        for app_id, connector in APP_CONNECTORS.items():
            for field in entries[app_id]["fields"]:
                with self.subTest(app=app_id, field=field["key"]):
                    if any(word in field["key"] for word in ("secret", "token", "api_key")):
                        self.assertTrue(field["secret"])
            # And no values, masked or otherwise.
            self.assertNotIn("value", json.dumps(entries[app_id]["fields"]))

    def test_connecting_an_app_requires_owner_access(self):
        """Connecting grants Akansha standing authority over an account or this desktop.

        That is the class of action `speaker_identity` exists to gate. Reading the
        catalog and the machine scan are deliberately ungated -- knowing which apps
        *could* be connected, or which are installed, leaks nothing that pressing the
        Windows key does not.

        Written as a sweep over every `/api/apps/*` route rather than a count of
        `_require_owner_for_apps(` occurrences. The count version passed for exactly
        as long as there were three write routes and then failed the moment adopt,
        sign-in and control were added -- reporting a number, not which route was
        unguarded. A sweep says the second thing, and does not need editing when a
        seventh route lands.
        """

        source = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        compact = compact_source(source)

        for route in (
            '@app.post("/api/apps/connect/{app_id}")',
            '@app.post("/api/apps/credentials/{app_id}")',
            '@app.post("/api/apps/disconnect/{app_id}")',
            '@app.post("/api/apps/adopt")',
            '@app.post("/api/apps/web/login/{app_id}")',
            '@app.post("/api/apps/control/{app_id}")',
        ):
            with self.subTest(route=route):
                self.assertIn(compact_source(route), compact)

        for decorator, body in self._app_route_bodies(source):
            gated = "_require_owner_for_apps" in body
            with self.subTest(route=decorator):
                if decorator.startswith("@app.get"):
                    # A read of the registry or of the Start Menu. Ungated on purpose.
                    self.assertFalse(gated, f"{decorator} is a read and must not be gated")
                else:
                    self.assertTrue(gated, f"{decorator} writes or acts and must be gated")

    @staticmethod
    def _app_route_bodies(source: str) -> list[tuple[str, str]]:
        """Every `/api/apps/*` route decorator paired with its handler body."""
        bodies: list[tuple[str, str]] = []
        for match in re.finditer(r'@app\.(get|post|delete)\("(/api/apps/[^"]*)"\)', source):
            decorator = f'@app.{match.group(1)}("{match.group(2)}")'
            following = source.find("@app.", match.end())
            bodies.append((decorator, source[match.end() : following if following > 0 else len(source)]))
        return bodies

    def test_credentials_are_stored_encrypted_under_the_key_that_reads_them(self):
        """The save and the read-back have to agree on where the blob lives.

        `encrypt_social_config` returns a bare `{"scheme", "payload"}` pair, so
        `metadata.update(...)` puts those two at the top level while
        `decrypt_social_config` looks under `config_encrypted`. That mismatch made
        the save report connected and the very next status check report the field
        still missing -- measured, then fixed.
        """

        source = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        start = source.index("def app_save_credentials(")
        body = compact_source(source[start : source.index("@app.", start + 1)])

        self.assertIn(
            'metadata["config_encrypted"] = encrypt_social_config(merged)', body
        )
        # No second credential store: a second store is a second thing to leak.
        self.assertNotIn("open(", body)

    def test_disconnect_reports_the_state_it_actually_leaves_behind(self):
        """Disconnect must re-probe, not assume.

        It used to hardcode `outcome="disconnected"` and the sentence
        "<app> credentials removed from this machine". For a desktop app that was
        false twice over: Chrome never had a credential to remove, and Chrome is
        still installed, so the very next status check answered `connected` again.
        The card flipped between two states on alternate reads -- the same class of
        lie as a button that reports connected without a credential, pointed the
        other way. Measured against the live endpoint, then fixed by returning
        `_app_plan_now` rather than a literal.
        """

        source = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        start = source.index("def app_disconnect(")
        body = source[start : source.index("@app.", start + 1)]
        # Past the docstring. The docstring quotes the sentence being banned, so a
        # test that searched the whole function would fail on the explanation of its
        # own fix -- the same trap `test_save_voice_speaker_derives_the_stored_access_level`
        # fell into with a comment.
        code = compact_source(body[body.index('"""', body.index('"""') + 3) + 3 :])

        # The outcome is probed, not asserted.
        self.assertIn("_app_plan_now(db, connector)", code)
        self.assertNotIn('"outcome": "disconnected"', code)
        self.assertNotIn("credentials removed from this machine", code)
        # And the row is still deleted rather than flagged -- a disabled row is an
        # encrypted secret left on disk for a later bug to read back.
        self.assertIn("db.delete(connection)", code)

    def test_every_endpoint_that_reports_state_uses_one_probe_path(self):
        """Connect, save and disconnect cannot be allowed to disagree.

        Copies of the `plan_connect(...)` probe wiring are chances for one of them to
        drift -- and a save that reports connected while the next connect reports
        otherwise is precisely the bug this module was written to end.

        Pinned as "there is one wiring and nobody bypasses it" rather than as a count
        of callers: the caller count is a number that goes up every time an endpoint
        is added, which says nothing about whether they agree.
        """

        source = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        compact = compact_source(source)

        self.assertEqual(compact.count("def _app_plan_now("), 1)
        # `plan_connect` itself is reached only through that helper: one call site,
        # inside the helper's own body.
        self.assertEqual(compact.count("plan_connect("), 1)
        helper = source[source.index("def _app_plan_now(") :]
        helper = helper[: helper.index("\n@app.")]
        self.assertIn("plan_connect(", helper)
        # And every endpoint that reports a state goes through it. `catalog()` is the
        # one exception, and it takes the same probes as arguments rather than
        # re-deriving them.
        for decorator, body in self._app_route_bodies(source):
            # `status_code=` is HTTP plumbing, not a connection state. Left in, it
            # made the sign-in endpoint -- which reports no state at all, only whether
            # a browser opened -- look like it was bypassing the probe.
            reports_state = 'outcome' in body.replace("status_code=", "") or '"status"' in body
            with self.subTest(route=decorator):
                if reports_state:
                    self.assertTrue(
                        "_app_plan_now(" in body or "app_catalog(" in body,
                        f"{decorator} reports a state without the shared probe path",
                    )

    def test_a_desktop_app_is_never_offered_a_disconnect_it_cannot_perform(self):
        """The button label and the button's effect come from one function.

        `actionFor` decides both, keyed on strategy as well as outcome, so a
        connected executable offers "Re-check" while a connected API key offers
        "Disconnect". Deriving the label from the outcome alone is what produced a
        Disconnect button on Chrome.
        """

        source = (PROJECT_ROOT / "src" / "lib" / "appConnect.ts").read_text(encoding="utf-8")

        self.assertIn("export function actionFor(", source)
        self.assertIn("local_executable", source.split("export function actionFor(")[1])
        # The old outcome-only label table must not still carry actions, or there are
        # two answers to "what does this button say".
        presentation = source.split("OUTCOME_PRESENTATION")[1].split("};")[0]
        self.assertNotIn("action:", presentation)

        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")
        # The click handler dispatches on the same function the label came from.
        self.assertIn("actionFor(entry)", view)
        self.assertIn("{action.label}", view)

    def test_the_connect_surface_renders_no_credential_value(self):
        """The page this replaced was display-only, and its controls operated on lies.

        It listed three hardcoded providers whose "keys" were bullet characters, a
        reveal toggle that unmasked the dummy, a copy button that put bullets on the
        clipboard, and an Add Provider button wired to nothing. The replacement can
        only be honest if there is no value to show: the catalog ships field
        descriptions, so a reveal or copy control here would have nothing real to
        act on.

        The route is `/connections` now. `/api-keys` and `/channel-integrations`
        were the two surfaces whose overlap caused *"what is this? It's still the
        old one, not the real one"*, and both are gone -- redirected, not just
        deleted, because a path that shipped is somebody's bookmark.
        """

        page = (PROJECT_ROOT / "src" / "app" / "connections" / "page.tsx").read_text(
            encoding="utf-8"
        )
        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")

        # No placeholder keys anywhere.
        for fake in ("sk-proj-", "sk-ant-", "AIzaSy", "\u2022"):
            with self.subTest(fake=fake):
                self.assertNotIn(fake, page)
                self.assertNotIn(fake, view)
        # No reveal and no copy: there is nothing to reveal or copy.
        for control in ("EyeOff", "clipboard.writeText", "showKeys"):
            with self.subTest(control=control):
                self.assertNotIn(control, view)
        # Secret fields are entered masked.
        self.assertIn("field.secret ? 'password' : 'text'", view)
        # And the page is wired to the real registry rather than a local array.
        self.assertIn("ConnectionsDirectory", page)

    def test_the_replaced_routes_are_gone_and_still_answer(self):
        """Deleting a shipped path 404s somebody's bookmark. Redirect instead.

        Three routes were removed: the two overlapping connect surfaces, and
        `/browser-automation`, whose working parts moved into the chat interface
        (*"please delete the browser page, integrate its features only features to
        the chat interface directly"*). A deletion with no redirect turns each of
        those into a dead link, which is a worse answer than the stale page was.
        """

        for gone in (
            PROJECT_ROOT / "src" / "app" / "api-keys",
            PROJECT_ROOT / "src" / "app" / "channel-integrations",
            PROJECT_ROOT / "src" / "app" / "browser-automation",
            PROJECT_ROOT / "src" / "components" / "integrations" / "ChannelIntegrationsView.tsx",
            PROJECT_ROOT / "src" / "components" / "automation" / "BrowserAutomationCenter.tsx",
        ):
            with self.subTest(path=gone.name):
                self.assertFalse(gone.exists(), "this is the surface that was reported as stale")

        config = compact_source((PROJECT_ROOT / "next.config.mjs").read_text(encoding="utf-8"))
        for source, destination in (
            ("/api-keys", "/connections"),
            ("/channel-integrations", "/connections"),
            ("/browser-automation", "/chat-interface"),
        ):
            with self.subTest(route=source):
                self.assertIn(
                    f"{{ source: '{source}', destination: '{destination}', permanent: false }}",
                    config,
                    "temporary, because these destinations are still being reshaped",
                )

        # Nothing in the app may still link to a route that no longer exists.
        for tsx in (PROJECT_ROOT / "src").rglob("*.tsx"):
            text = tsx.read_text(encoding="utf-8")
            for dead in ("href=\"/api-keys\"", "href=\"/channel-integrations\"", "href=\"/browser-automation\""):
                with self.subTest(file=tsx.name, link=dead):
                    self.assertNotIn(dead, text)

    def test_the_directory_can_reach_apps_no_list_contains(self):
        """A longer hardcoded list is not the answer to *"not like this Static route"*.

        The card grid this replaced was honest about each of its twenty-seven apps
        and useless anyway, because the machine has sixty-three and the web has the
        rest. Two affordances are the whole fix and neither can be faked with data:
        a scan of this PC, and a box you paste a URL into. If either disappears the
        surface is a static list again, whatever its shape.
        """

        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")

        for reach in ("discoverApps", "adoptApp", "signInToSite"):
            with self.subTest(call=reach):
                self.assertIn(reach, view)
        # Both adoption kinds are wired: an app on this PC, and any site.
        self.assertIn("kind: 'desktop'", view)
        self.assertIn("kind: 'web'", view)
        # Every action `actionFor` can return is handled. A kind with no branch is a
        # button that looks live and does nothing -- the exact complaint about the
        # page before this one.
        for kind in ("'input'", "'install'", "'signin'", "'remove'", "'disconnect'"):
            with self.subTest(kind=kind):
                self.assertIn(f"kind === {kind}", view)

    def test_the_website_flow_never_asks_this_app_for_a_site_password(self):
        """One-click website auth has exactly one honest implementation.

        No OAuth client can be registered for an arbitrary domain, so the only way
        to connect `mail.google.com` is to open it in a browser profile Akansha owns
        and let the person sign in themselves. A password field on this surface for
        a `web_session` connector would be the alternative, and it would be the worst
        thing in this repo -- so the URL box must post a URL and nothing else.
        """

        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")

        site_form = view.split("const handleAddSite")[1].split("const entries")[0]
        for forbidden in ("password", "username", "email:", "credential"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, site_form.lower())
        # And the promise is made to the user's face, not only in a comment.
        self.assertIn("never passes through this app", view)

    def test_no_tile_fetches_a_brand_logo(self):
        """This repo ships no brand assets, and hotlinking is not a substitute.

        A `<img src="https://cdn.../slack.png">` per row would put thirty
        third-party requests -- with a referrer naming this page -- on the one
        surface whose entire subject is not leaking anything. Monograms are derived
        locally and are deterministic, so a tile is the same colour on every load.
        """

        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")

        self.assertNotIn("<img", view)
        self.assertNotIn("https://logo", view)
        self.assertNotIn("favicon", view)
        self.assertIn("function hueFor(", view)

        # Determinism, checked rather than asserted about: the same id must hash to
        # the same hue, and different ids must not all collapse onto one colour.
        def hue_for(app_id: str) -> float:
            digest = 0
            for char in app_id:
                digest = (digest * 31 + ord(char)) % 3600
            return digest / 10

        self.assertEqual(hue_for("discord"), hue_for("discord"))
        hues = {hue_for(name) for name in ("discord", "cursor", "vlc", "chrome", "slack")}
        self.assertGreater(len(hues), 3, "a palette that collapses is not a palette")

    def test_a_website_beats_a_client_secret_as_the_first_route(self):
        """*"not oauth client secret all processes complex one click browser login"*.

        The X row in the paste opened by demanding an "OAuth client id" and an
        "OAuth client secret" -- i.e. become a registered developer before Akansha
        may post for you. Any connector carrying a `web_url` must instead plan
        `needs_sign_in` when its credentials are absent, so the first thing offered
        is a browser login, not a developer application. The fields are still
        *carried* (the API route stays reachable), but they are no longer the only
        way in.
        """
        from backend.app_connect import (
            APP_CONNECTORS,
            CONNECTED,
            NEEDS_INPUT,
            NEEDS_SIGN_IN,
            plan_connect,
        )

        twitter = APP_CONNECTORS["twitter"]
        self.assertTrue(twitter.web_url, "the connector under test must have a browser route")

        # Nothing stored, no session: one click to sign in, not a client secret.
        cold = plan_connect(twitter, {})
        self.assertEqual(cold.outcome, NEEDS_SIGN_IN)
        self.assertNotIn("client secret", cold.detail.lower())
        # The developer fields survive as the secondary route rather than vanishing.
        self.assertTrue(cold.required, "the API-key route must still be offered")

        # Signed in in the browser profile: connected, with no developer app at all.
        warm = plan_connect(twitter, {}, session_ok=lambda _a: True)
        self.assertEqual(warm.outcome, CONNECTED)
        self.assertIn("no client secret", warm.detail.lower())
        self.assertEqual(warm.connected_to, twitter.web_url)

        # A real OAuth token, once granted, still wins over a cookie.
        tokened = plan_connect(twitter, {}, has_token=True)
        self.assertEqual(tokened.outcome, CONNECTED)

        # A model provider is deliberately NOT signable-into: a cookie at the
        # provider does not let this app request a completion, so it keeps asking
        # for a key rather than pretending a browser login connected it.
        openrouter = APP_CONNECTORS["openrouter"]
        self.assertEqual(openrouter.web_url, "")
        self.assertEqual(plan_connect(openrouter, {}).outcome, NEEDS_INPUT)

    def test_a_web_url_connector_can_be_driven_through_the_browser(self):
        """A one-click login that connects nothing you can then use is theatre.

        `effective_capabilities` widens any `web_url` connector with the browser
        verbs, so `/api/apps/control` accepts `open`/`read_page` on it -- without
        this the very connectors the browser login was added for would reject every
        action with "'open' is not one of:". The declared capabilities must still
        come first and survive.
        """
        from backend.app_connect import APP_CONNECTORS, effective_capabilities
        from backend.app_control import WEB_VERBS

        twitter = APP_CONNECTORS["twitter"]
        caps = effective_capabilities(twitter)
        for verb in WEB_VERBS:
            with self.subTest(verb=verb):
                self.assertIn(verb, caps)
        for declared in twitter.capabilities:
            self.assertIn(declared, caps)

        # No `web_url`, no widening: a pure API connector is not suddenly drivable.
        self.assertEqual(
            effective_capabilities(APP_CONNECTORS["openrouter"]),
            tuple(APP_CONNECTORS["openrouter"].capabilities),
        )

    def test_every_messaging_service_has_a_browser_way_in(self):
        """*"also appplies for messaging apps"*, *"also all n complex process"*.

        Messaging was the worst case of the thing being complained about: WhatsApp's
        only route was *run a self-hosted bridge server*, and Telegram's and
        Discord's were bot tokens -- a bot cannot read your chats, so even after all
        that work "read my messages" did not become possible. Every service in this
        family must therefore be reachable as *you*, in a browser, in one click.

        The two bot connectors are allowed to remain credential-only: a bot really
        is a different account, and pretending otherwise is what made the old
        surface misleading. Each is required to have a browser sibling instead.
        """
        from backend.app_connect import APP_CONNECTORS, FAMILY_MESSAGING, LOCAL_EXECUTABLE, WEB_SESSION

        credential_only = sorted(
            c.app_id
            for c in APP_CONNECTORS.values()
            if c.family == FAMILY_MESSAGING
            and c.strategy not in (LOCAL_EXECUTABLE, WEB_SESSION)
            and not c.web_url
        )
        self.assertEqual(
            credential_only,
            ["discord_bot", "telegram_bot"],
            "a messaging service with no browser route is the complaint, not a design",
        )
        # And the human account for each of those two exists and is a browser login.
        for sibling in ("discord_web", "telegram_web"):
            with self.subTest(app=sibling):
                connector = APP_CONNECTORS[sibling]
                self.assertEqual(connector.strategy, WEB_SESSION)
                self.assertTrue(connector.site_url)
        # WhatsApp specifically: the bridge may stay, but it is no longer the only
        # way. One row with two routes rather than a second connector, because a
        # bridge drives the same account the QR code signs into -- unlike a bot.
        self.assertTrue(APP_CONNECTORS["whatsapp_bridge"].web_url)
        self.assertNotIn("whatsapp_web", APP_CONNECTORS)

    def test_the_ui_offers_browser_verbs_by_route_not_by_strategy(self):
        """A one-click login on an `oauth2` row must still produce usable buttons.

        `CONTROL_VERBS[entry.strategy]` was the bug waiting to happen: X connects
        through the browser but is declared `oauth2`, so keying the control strip on
        strategy alone would connect it and then offer nothing to do with it.
        """
        view = (
            PROJECT_ROOT / "src" / "components" / "integrations" / "ConnectionsDirectory.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function verbsFor(", view)
        self.assertIn("drivesBrowser(entry)", view)
        # The assignment, not the phrase: the comment above `verbsFor` quotes the old
        # expression to explain what it was.
        self.assertNotIn("const verbs = CONTROL_VERBS[", view)
        self.assertIn("const verbs = verbsFor(entry)", view)

        # Every desktop button must name a verb the driver accepts, or its only
        # possible outcome is a 400. `click` is the documented omission: it needs the
        # name of a control, which the row has no room to ask for.
        from backend.app_control import DESKTOP_VERBS

        strip = view.split("local_executable: [")[1].split("]")[0]
        offered = re.findall(r"verb: '([a-z_]+)'", strip)
        self.assertIn("read_window", offered)
        self.assertNotIn("click", offered)
        for verb in offered:
            with self.subTest(verb=verb):
                self.assertIn(verb, DESKTOP_VERBS)

        lib = (PROJECT_ROOT / "src" / "lib" / "appConnect.ts").read_text(encoding="utf-8")
        # The client's verb list must not drift from the driver's.
        from backend.app_control import WEB_VERBS

        listed = lib.split("export const WEB_VERBS = [")[1].split("]")[0]
        for verb in WEB_VERBS:
            with self.subTest(verb=verb):
                self.assertIn(f"'{verb}'", listed)
        # `web_url` has to reach the client at all, and the browser route has to be
        # said in words rather than left as a differently-coloured chip.
        self.assertIn("web_url: string", lib)
        self.assertIn("No developer app, no client secret", view)
        # And a connection that is a cookie must not offer to "Disconnect" it: that
        # deletes a credential row which does not exist and then reads `connected`
        # again on the next probe, which is the broken action offering to disconnect
        # an installed desktop app used to be.
        self.assertIn("connectedInBrowser(entry)", lib)

    def test_the_catalog_exposes_the_browser_route_to_the_client(self):
        """`describe_connector` must emit `web_url`, or the UI cannot tell the two
        routes apart and falls back to demanding a client secret again."""
        from backend.app_connect import APP_CONNECTORS, describe_connector

        described = describe_connector(APP_CONNECTORS["twitter"])
        self.assertEqual(described["web_url"], APP_CONNECTORS["twitter"].web_url)
        # And the widened capabilities travel with it.
        self.assertIn("read_page", described["capabilities"])


class PresenceBehaviorTests(unittest.TestCase):
    """Regression coverage for `src/lib/presenceBehavior.ts`.

    Two complaints from the person using this app produced this file: the voice
    panel showed "old akansha" -- a warped photograph -- and the full-screen
    presence showed "only image ... no hand movement lip movement eye movement".
    Measured in the browser, both were true, and neither was a missing asset:

        reducedMotion: true, <video> elements: 0, <img>: akansha-presence.webp

    The footage of her exists, carries real hands, real eyes and a real mouth,
    and never played. Two defects put it there, and both are pinned below.

    1. `prefers-reduced-motion: reduce` replaced the whole performance with a
       still. That preference is about movement a person did not ask for; the
       assistant's face is the content of the page. So `MotionPlan` deliberately
       has no field that can stop playback -- the bug was a boolean that could,
       and `test_reduced_motion_cannot_stop_the_performance` fails if one comes
       back.
    2. One `onError` set a single `failed` flag for all five clips, so one file
       this Chromium cannot decode -- and it cannot decode H.264 at all, measured
       -- took away the four that had loaded. Health is per clip now, and
       `playableClip` returns `null` only when every clip is broken.

    The rules run through Node against the real shipped `.ts`, the same
    discipline `WakeWordTests` is held to and for the same reason: a Python
    re-implementation would encode what I believe the rules are and pass while
    the shipped module was broken. Node 24 strips types natively and this module
    imports nothing, so it loads exactly as the browser bundle loads it.
    """

    SOURCE = Path(__file__).resolve().parents[1] / "src" / "lib" / "presenceBehavior.ts"
    COMPONENT = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "components"
        / "assistant"
        / "BodyPresence.tsx"
    )

    NODE_DRIVER = """
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [modulePath, casesPath] = process.argv.slice(2);
const p = await import(pathToFileURL(modulePath).href);
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

process.stdout.write(
  JSON.stringify({
    clipNames: p.CLIP_NAMES,
    constants: {
      FADE_MS: p.FADE_MS,
      GESTURE_REFRACTORY_MS: p.GESTURE_REFRACTORY_MS,
      EMPHASIS_THRESHOLD: p.EMPHASIS_THRESHOLD,
      STRONG_EMPHASIS: p.STRONG_EMPHASIS,
      GESTURE_LEAD_IN_MS: p.GESTURE_LEAD_IN_MS,
    },
    plans: { reduced: p.motionPlan(true), full: p.motionPlan(false) },
    shouldPlay: cases.baseClip.map((c) => ({ ...c, play: p.shouldPlay(c) })),
    stances: cases.turns.map((turn) => ({
      turn,
      stance: p.stanceFor(turn),
      entry: p.entryFractionFor(turn),
    })),
    gestureCycle: p.GESTURE_CYCLE,
    baseClip: cases.baseClip.map((c) => ({ ...c, clip: p.baseClip(c) })),
    gestureFor: cases.gestureFor.map((c) => ({
      ...c,
      gesture: p.gestureFor(c.emphasis, c.gestureCount),
    })),
    playableClip: cases.playableClip.map((c) => ({
      ...c,
      visible: p.playableClip(c.wanted, c.health),
      anyPlayable: p.anyClipPlayable(c.health),
    })),
  })
);
"""

    # Every combination that matters, with `thinking` deliberately resting: a
    # figure gesturing at nothing while it waits on a model reads as stalling,
    # and the amber halo already says it is working.
    BASE_CLIP_CASES = (
        {"speaking": True, "listening": True, "thinking": True, "expected": "speaking"},
        {"speaking": True, "listening": False, "thinking": False, "expected": "speaking"},
        {"speaking": False, "listening": True, "thinking": True, "expected": "listening"},
        {"speaking": False, "listening": True, "thinking": False, "expected": "listening"},
        {"speaking": False, "listening": False, "thinking": True, "expected": "idle"},
        {"speaking": False, "listening": False, "thinking": False, "expected": "idle"},
    )

    # Emphasis is a *rise* over the running level, so these numbers are gaps
    # between two envelopes of the same signal, not loudness.
    GESTURE_CASES = (
        {"emphasis": 0.0, "gestureCount": 0, "expected": None},
        {"emphasis": -0.2, "gestureCount": 0, "expected": None},
        # Exactly at the threshold is not over it.
        {"emphasis": 0.055, "gestureCount": 0, "expected": None},
        {"emphasis": 0.056, "gestureCount": 0, "expected": "open"},
        {"emphasis": 0.056, "gestureCount": 1, "expected": "beat"},
        {"emphasis": 0.114, "gestureCount": 4, "expected": "beat"},
        {"emphasis": 0.116, "gestureCount": 4, "expected": "open"},
    )

    BROKEN = "broken"

    # Each row is a state this Chromium can actually be in. The all-broken row is
    # the only one allowed to reach the still.
    PLAYABLE_CASES = (
        {"wanted": "speaking", "health": {}, "expected": "speaking"},
        {"wanted": "speaking", "health": {"speaking": "ready"}, "expected": "speaking"},
        {"wanted": "speaking", "health": {"speaking": BROKEN}, "expected": "open"},
        {
            "wanted": "speaking",
            "health": {"speaking": BROKEN, "open": BROKEN},
            "expected": "beat",
        },
        {
            "wanted": "speaking",
            "health": {"speaking": BROKEN, "open": BROKEN, "beat": BROKEN},
            "expected": "idle",
        },
        {"wanted": "listening", "health": {"listening": BROKEN}, "expected": "idle"},
        {"wanted": "idle", "health": {"idle": BROKEN}, "expected": "listening"},
        {"wanted": "beat", "health": {"beat": BROKEN}, "expected": "open"},
        {
            "wanted": "speaking",
            "health": {
                "speaking": BROKEN,
                "listening": BROKEN,
                "idle": BROKEN,
                "beat": BROKEN,
                "open": BROKEN,
            },
            "expected": None,
        },
    )

    evaluated: dict = {}

    @classmethod
    def setUpClass(cls):
        """Evaluate every rule in one Node process."""
        node = shutil.which("node")
        if not node:
            # An honest skip: it says the runner had no Node, and nothing about
            # the rules. The source-pinning tests below need no Node and run.
            raise unittest.SkipTest("node is not on PATH, so the TypeScript module cannot be run")

        cases = {
            "baseClip": [dict(row) for row in cls.BASE_CLIP_CASES],
            "gestureFor": [dict(row) for row in cls.GESTURE_CASES],
            "playableClip": [dict(row) for row in cls.PLAYABLE_CASES],
            # Twelve turns: three full trips round both the stance list (4) and
            # the entry list (5), which is where an off-by-one in either modulo
            # would show up as a repeat landing on the same index.
            "turns": list(range(12)),
        }

        with tempfile.TemporaryDirectory() as workdir:
            driver = Path(workdir) / "driver.mjs"
            payload = Path(workdir) / "cases.json"
            driver.write_text(cls.NODE_DRIVER, encoding="utf-8")
            payload.write_text(json.dumps(cases), encoding="utf-8")
            completed = subprocess.run(
                [node, "--no-warnings", str(driver), str(cls.SOURCE), str(payload)],
                capture_output=True,
            )

        if completed.returncode != 0:
            raise AssertionError(
                "node failed to run presenceBehavior.ts:\n"
                + completed.stderr.decode("utf-8", "replace")
            )

        cls.evaluated = json.loads(completed.stdout.decode("utf-8"))

    def test_reduced_motion_cannot_stop_the_performance(self):
        """The defect that made her a photograph, pinned two ways."""
        reduced = self.evaluated["plans"]["reduced"]
        full = self.evaluated["plans"]["full"]

        # First: the plan has exactly three fields and every one of them narrows
        # decoration. There is no field a future edit could read as "show a still".
        self.assertEqual(
            sorted(reduced),
            ["allowGestures", "allowProxemics", "fadeMs"],
            "a new field here is how she became a photograph the first time",
        )
        self.assertEqual(sorted(reduced), sorted(full))

        # Second: what reduced motion actually takes away is the cross-fade, the
        # gesture interrupts and the lean-in. Nothing else.
        self.assertEqual(reduced["fadeMs"], 0)
        self.assertFalse(reduced["allowGestures"])
        self.assertFalse(reduced["allowProxemics"])
        self.assertGreater(full["fadeMs"], 0)
        self.assertTrue(full["allowGestures"])
        self.assertTrue(full["allowProxemics"])

        # And she still has a clip to play in every state, which is the whole
        # point: reduced motion means at rest, not absent.
        for row in self.evaluated["baseClip"]:
            with self.subTest(row=row):
                self.assertIn(row["clip"], self.evaluated["clipNames"])

    def test_base_behaviour_follows_the_conversation(self):
        for row in self.evaluated["baseClip"]:
            with self.subTest(row=row):
                self.assertEqual(
                    row["clip"],
                    row["expected"],
                    "speech outranks the microphone, and thinking rests",
                )

    def test_gestures_need_a_rise_over_the_running_level(self):
        for row in self.evaluated["gestureFor"]:
            with self.subTest(row=row):
                self.assertEqual(row["gesture"], row["expected"])

    def test_the_first_gesture_of_a_turn_is_the_presenting_one(self):
        first = next(
            row
            for row in self.evaluated["gestureFor"]
            if row["gestureCount"] == 0 and row["gesture"] is not None
        )
        self.assertEqual(
            first["gesture"],
            "open",
            "the opening gesture reads as 'here is the thing I am about to explain'",
        )

    def test_one_broken_clip_substitutes_rather_than_freezing_all_five(self):
        """The defect that made one undecodable file delete four working ones."""
        for row in self.evaluated["playableClip"]:
            with self.subTest(wanted=row["wanted"], health=row["health"]):
                self.assertEqual(row["visible"], row["expected"])
                if row["expected"] is not None:
                    self.assertNotEqual(
                        row["health"].get(row["visible"]),
                        self.BROKEN,
                        "a substitute that is itself broken is the original bug",
                    )
                    self.assertTrue(row["anyPlayable"])

    def test_the_still_is_reached_only_when_every_clip_is_broken(self):
        stills = [row for row in self.evaluated["playableClip"] if row["visible"] is None]
        self.assertEqual(len(stills), 1, "exactly one case in the table is all-broken")
        self.assertEqual(
            sorted(stills[0]["health"]),
            sorted(self.evaluated["clipNames"]),
            "null must require the whole set to have failed, not a subset",
        )
        self.assertFalse(stills[0]["anyPlayable"])

    def test_every_clip_has_a_substitution_chain_covering_the_others(self):
        names = self.evaluated["clipNames"]
        self.assertEqual(sorted(names), ["beat", "idle", "listening", "open", "speaking"])
        # Four alternatives per clip: any single survivor must be reachable, or a
        # partial failure still reaches the photograph.
        source = self.SOURCE.read_text(encoding="utf-8")
        for name in names:
            with self.subTest(clip=name):
                line = next(
                    stripped
                    for stripped in (raw.strip() for raw in source.splitlines())
                    if stripped.startswith(f"{name}: [")
                )
                listed = {token.strip().strip("'") for token in line.split("[")[1].split("]")[0].split(",")}
                self.assertEqual(
                    listed,
                    set(names) - {name},
                    "the chain must name every other clip exactly once",
                )

    def test_source_pins_the_tuned_constants(self):
        """Runs without Node, so the numbers stay covered on a bare runner."""
        compact = compact_source(self.SOURCE.read_text(encoding="utf-8"))
        # Long enough to hide a pose change, short enough that a gesture still
        # lands on the word that triggered it.
        self.assertIn("const FADE_MS = 240;", compact)
        # People gesture in bursts every few seconds, not on every stressed
        # syllable.
        self.assertIn("const GESTURE_REFRACTORY_MS = 2600;", compact)
        self.assertIn("const EMPHASIS_THRESHOLD = 0.055;", compact)
        self.assertIn("const STRONG_EMPHASIS = 0.115;", compact)
        self.assertIn("const GESTURE_LEAD_IN_MS = 420;", compact)
        self.assertLess(
            compact.index("EMPHASIS_THRESHOLD = 0.055"),
            compact.index("STRONG_EMPHASIS = 0.115"),
        )
        # The stance and entry cycles are literal lists, not generators, so that a
        # future edit cannot quietly turn a returning cycle into a drifting walk.
        self.assertIn("const STANCES: readonly Stance[] = [", compact)
        self.assertIn("{ x: 0, y: 0, scale: 1 },", compact)
        self.assertIn("const ENTRY_POINTS: readonly number[] = [0, 0.22, 0.44, 0.12, 0.33];", compact)
        self.assertIn(
            "const GESTURE_CYCLE: readonly ClipName[] = ['open', 'beat', 'open', 'beat', 'beat'];",
            compact,
        )
        # The gate itself: `shouldPlay` may read nothing but `speaking`, because
        # every other input it could read is one the user asked it to ignore.
        self.assertIn("function shouldPlay({ speaking }: PresenceInputs): boolean {", compact)
        self.assertIn("return speaking;", compact)

    def test_the_component_pauses_rather_than_swapping_when_silent(self):
        """A held frame of the same woman, not a photograph substituted for one."""
        component = self.COMPONENT.read_text(encoding="utf-8")
        compact = compact_source(component)

        # Playback is driven by the rule module, not by a local condition that
        # could drift from it.
        self.assertIn("shouldPlay(", compact)
        self.assertIn("current.pause();", compact)
        # Silence pauses the element it was already showing; it does not clear
        # `visible`, which is the only thing that can reach the still.
        self.assertIn("if (running) {", compact)
        # Each turn enters the clip at a different point, so two answers do not
        # open on the same head movement.
        self.assertIn("entryFractionFor(turn)", compact)
        self.assertIn("stanceFor(", compact)
        # And the state is inspectable from the DOM, which is how it was verified.
        self.assertIn("data-presence-running=", component)
        self.assertIn("data-presence-turn=", component)

    def test_the_component_offers_two_encodings_per_clip(self):
        """Because this Chromium cannot decode H.264 and says it can.

        Measured in the preview browser: `canPlayType('avc1.42E01E')` returns
        "probably", the load then fails with `MEDIA_ELEMENT_ERROR: Format error`,
        code 4, `decodedFrames: 0`. A single `src` makes that terminal. Two
        `<source>` children make the browser walk to the next candidate, and VP9
        decodes here, so the same build that showed a photograph now shows her.
        """
        component = self.COMPONENT.read_text(encoding="utf-8")
        self.assertIn("<source src={`${CLIPS[name].base}.mp4`} type=\"video/mp4\" />", component)
        self.assertIn("<source src={`${CLIPS[name].base}.webm`} type=\"video/webm\" />", component)
        # mp4 first: it is the one with hardware decode on this hardware.
        self.assertLess(component.index('type="video/mp4"'), component.index('type="video/webm"'))
        # And no `src` attribute on the element, which would win over both.
        self.assertNotIn("<video\n            src=", component)

        for name in self.evaluated["clipNames"] if self.evaluated else ():
            with self.subTest(clip=name):
                base = next(
                    line.split("'")[1]
                    for line in component.splitlines()
                    if line.strip().startswith(f"{name}: {{ base: ")
                )
                for suffix in (".mp4", ".webm"):
                    asset = (
                        Path(__file__).resolve().parents[1] / "public" / base.lstrip("/")
                    ).with_suffix(suffix)
                    self.assertTrue(asset.is_file(), f"{asset.name} is referenced but missing")
                    self.assertGreater(asset.stat().st_size, 20_000)

    def test_the_component_keeps_no_single_failure_flag_and_no_still_gate(self):
        """The two lines that produced both complaints, pinned as absences."""
        component = self.COMPONENT.read_text(encoding="utf-8")
        compact = compact_source(component)

        # `const still = reducedMotion || failed;` and `if (reducedMotion || failed) return;`
        self.assertNotIn("reducedMotion || failed", compact)
        self.assertNotIn("setFailed", compact)
        # Health is per clip, keyed by name.
        self.assertIn("onError={() => mark(name, 'broken')}", component)
        self.assertIn("playableClip(active, health)", compact)
        # The still is rendered behind `!visible`, which only `playableClip`
        # returning null can produce.
        self.assertIn("{!visible && (", component)
        # The scheduler depends on the plan, not on a reduced-motion early return.
        self.assertIn("}, [plan]);", component)

    def test_she_moves_only_while_there_is_audio(self):
        """Reported as "the voice interface is continuously moving".

        A loop that runs while the assistant is silent is a figure mouthing words
        with no sound, and movement that never stops carries no information. The
        microphone being open does not qualify -- it used to, and the movement that
        produced is what was complained about.
        """
        for row in self.evaluated["shouldPlay"]:
            with self.subTest(row=row):
                self.assertEqual(
                    row["play"],
                    row["speaking"],
                    "playback follows speech and nothing else -- not the mic, not thinking",
                )
        # The specific regression, named: mic open and nothing being said used to
        # play the listening loop, and that is the movement that was reported.
        listening_only = [
            row
            for row in self.evaluated["shouldPlay"]
            if row["listening"] and not row["speaking"]
        ]
        self.assertTrue(listening_only, "the table must cover an open mic with no speech")
        for row in listening_only:
            with self.subTest(row=row):
                self.assertFalse(row["play"], "an open microphone is not a reason to move")

    def test_each_utterance_gets_a_new_stance_and_a_new_entry_point(self):
        """Two answers in a row must not open from the identical pose and frame."""
        rows = self.evaluated["stances"]
        for previous, current in zip(rows, rows[1:]):
            with self.subTest(turn=current["turn"]):
                self.assertNotEqual(
                    (previous["stance"], previous["entry"]),
                    (current["stance"], current["entry"]),
                    "consecutive turns sharing a stance and an entry point is the "
                    "tell that this is a video and not a person",
                )

        # Cyclic, not random: a random walk drifts somewhere wrong over a long
        # conversation, and a cycle returns.
        self.assertEqual(rows[0]["stance"], rows[4]["stance"])
        self.assertEqual(rows[0]["entry"], rows[5]["entry"])

        # Small. Past a few percent a shift reads as the camera moving, and the
        # camera that shot this never moved.
        for row in rows:
            with self.subTest(turn=row["turn"]):
                self.assertLessEqual(abs(row["stance"]["x"]), 2.0)
                self.assertLessEqual(abs(row["stance"]["y"]), 2.0)
                self.assertGreaterEqual(row["stance"]["scale"], 1.0)
                self.assertLessEqual(row["stance"]["scale"], 1.05)
                # The last third of the talking clip is the reversal turnaround;
                # entering there opens the answer on motion playing backwards.
                self.assertGreaterEqual(row["entry"], 0.0)
                self.assertLess(row["entry"], 0.5)

    def test_gestures_vary_within_a_single_answer(self):
        """Only two gesture clips exist, so repetition has to be designed out."""
        cycle = self.evaluated["gestureCycle"]
        self.assertEqual(cycle[0], "open", "a turn opens on the presenting gesture")
        self.assertGreaterEqual(len(cycle), 4)
        self.assertEqual(
            {*cycle},
            {"open", "beat"},
            "the cycle may only name gestures that were actually cut",
        )
        self.assertLessEqual(
            cycle.count("open"),
            cycle.count("beat"),
            "two held palms in a row reads as a lecture; the short beat should dominate",
        )
        # No three consecutive identical entries, which would be the tic the cycle
        # exists to remove.
        for index in range(len(cycle) - 2):
            with self.subTest(index=index):
                self.assertFalse(
                    cycle[index] == cycle[index + 1] == cycle[index + 2],
                    "three of the same gesture in a row is a metronome",
                )


class AutomationLivenessTests(unittest.TestCase):
    """Regression coverage for `src/lib/automationStatus.ts`.

    The complaint: *"there are six automation permissions active. It is not live,
    actually, so all the things should be live."* The badge was reading
    `Object.keys(...).length` over a dict of six hardcoded `true`s on the server,
    so it printed "6 automation permissions active" on every load, in every
    condition -- including on a host with no reachable screen, where not one of
    those six could have been carried out.

    So the rule this class exists to hold: **capability outranks preference.**
    Whether the desktop can be driven is measured per request and decides the
    wording; the permission count is at most a detail appended to it, and never
    the headline on its own.

    Run through Node against the real shipped `.ts` for the reason
    `PresenceBehaviorTests` and `WakeWordTests` are: a Python re-implementation
    of the wording rules would assert what I believe they are and pass while the
    shipped module said something else. The module imports nothing, so Node 24's
    native type stripping loads it exactly as the browser bundle does.
    """

    SOURCE = Path(__file__).resolve().parents[1] / "src" / "lib" / "automationStatus.ts"
    API = Path(__file__).resolve().parent / "main.py"
    PROBE = Path(__file__).resolve().parent / "automation.py"

    NODE_DRIVER = """
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';

const [modulePath, casesPath] = process.argv.slice(2);
const p = await import(pathToFileURL(modulePath).href);
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

process.stdout.write(
  JSON.stringify(
    Object.fromEntries(cases.map((c) => [c.name, p.summariseAutomation(c.status)]))
  )
);
"""

    PERMISSION_KEYS = (
        "open_links",
        "open_close_tabs",
        "type_into_page",
        "edit_fields",
        "delete_draft_content",
        "background_open",
    )

    evaluated: dict = {}

    @classmethod
    def _runtime(cls, **overrides) -> dict:
        """A fully healthy probe, then whatever the case is actually about."""
        runtime = {
            "gui_control": True,
            "screen": {"width": 1920, "height": 1080},
            "screen_error": None,
            "window_manager": True,
            "runner_available": True,
            "scheduled_total": 0,
            "scheduled_due": 0,
            "checked_at": "2026-08-27T09:00:00",
        }
        runtime.update(overrides)
        # `gui_control` is the server's own conjunction of the two probes. Keep the
        # fixtures honest about that instead of hand-setting a combination the
        # backend could never emit.
        runtime["gui_control"] = runtime["screen"] is not None and runtime["window_manager"]
        return runtime

    @classmethod
    def _cases(cls) -> list[dict]:
        all_six = {key: True for key in cls.PERMISSION_KEYS}
        one_only = {key: key == "open_links" for key in cls.PERMISSION_KEYS}
        none_on = {key: False for key in cls.PERMISSION_KEYS}
        return [
            # The backend never answered. Not the same thing as revoked.
            {"name": "no-answer", "status": None},
            # An older backend with no probe at all.
            {"name": "no-runtime-block", "status": {"permissions": all_six}},
            # The exact machine the complaint was about: every permission granted,
            # nothing on screen to grant them over.
            {
                "name": "offline-with-every-permission-granted",
                "status": {
                    "permissions": all_six,
                    "granted": sorted(all_six),
                    "runtime": cls._runtime(
                        screen=None,
                        screen_error="DisplayError: no display found",
                    ),
                },
            },
            {
                "name": "offline-screen-missing-without-a-reason",
                "status": {"permissions": all_six, "runtime": cls._runtime(screen=None)},
            },
            {
                "name": "offline-window-manager-silent",
                "status": {"permissions": all_six, "runtime": cls._runtime(window_manager=False)},
            },
            # Live, and the state the running app is actually in right now.
            {
                "name": "overdue-plural",
                "status": {
                    "permissions": all_six,
                    "runtime": cls._runtime(scheduled_total=2, scheduled_due=2),
                },
            },
            {
                "name": "overdue-single",
                "status": {
                    "permissions": all_six,
                    "runtime": cls._runtime(scheduled_total=3, scheduled_due=1),
                },
            },
            {
                "name": "runner-missing",
                "status": {"permissions": all_six, "runtime": cls._runtime(runner_available=False)},
            },
            {
                "name": "healthy-with-schedule",
                "status": {
                    "permissions": all_six,
                    "runtime": cls._runtime(scheduled_total=2),
                },
            },
            {
                "name": "healthy-one-permission",
                "status": {"permissions": one_only, "runtime": cls._runtime()},
            },
            {
                "name": "healthy-no-permissions",
                "status": {"permissions": none_on, "runtime": cls._runtime()},
            },
        ]

    @classmethod
    def setUpClass(cls):
        """Summarise every case in one Node process."""
        node = shutil.which("node")
        if not node:
            # Honest skip: it says the runner had no Node and nothing about the
            # rules. The source-pinning tests below need no Node and still run.
            raise unittest.SkipTest("node is not on PATH, so the TypeScript module cannot be run")

        with tempfile.TemporaryDirectory() as workdir:
            driver = Path(workdir) / "driver.mjs"
            payload = Path(workdir) / "cases.json"
            driver.write_text(cls.NODE_DRIVER, encoding="utf-8")
            payload.write_text(json.dumps(cls._cases()), encoding="utf-8")
            completed = subprocess.run(
                [node, "--no-warnings", str(driver), str(cls.SOURCE), str(payload)],
                capture_output=True,
            )

        if completed.returncode != 0:
            raise AssertionError(
                "node failed to run automationStatus.ts:\n"
                + completed.stderr.decode("utf-8", "replace")
            )

        cls.evaluated = json.loads(completed.stdout.decode("utf-8"))

    def test_permissions_never_outrank_a_dead_screen(self):
        """The defect itself: six granted flags reported as working automation."""
        offline = self.evaluated["offline-with-every-permission-granted"]
        self.assertEqual(offline["tone"], "offline")
        self.assertEqual(offline["activeCount"], 6, "all six really are granted here")
        # The count may appear in the long form. It may not be the headline, and
        # the headline may not say the word the whole complaint was about.
        self.assertNotIn("6", offline["label"])
        self.assertNotIn("live", offline["label"].lower())
        self.assertIn("no desktop control", offline["label"].lower())
        # Why, verbatim, rather than a generic failure. The person reading this is
        # the one who has to fix the host.
        self.assertIn("DisplayError: no display found", offline["detail"])

        for name in ("offline-screen-missing-without-a-reason", "offline-window-manager-silent"):
            with self.subTest(case=name):
                summary = self.evaluated[name]
                self.assertEqual(summary["tone"], "offline")
                self.assertNotIn("live", summary["label"].lower())
        # Each failing probe is named in its own words -- "no screen" and "the
        # window manager" are different repairs.
        self.assertIn(
            "screen",
            self.evaluated["offline-screen-missing-without-a-reason"]["detail"].lower(),
        )
        self.assertIn(
            "window manager",
            self.evaluated["offline-window-manager-silent"]["detail"].lower(),
        )

    def test_an_unreachable_backend_is_not_a_revoked_permission(self):
        """The old code collapsed "no answer" and "nothing granted" into silence."""
        for name in ("no-answer", "no-runtime-block"):
            with self.subTest(case=name):
                summary = self.evaluated[name]
                self.assertEqual(summary["tone"], "unknown")
                # Never the word the badge could not back up.
                self.assertNotIn("live ", summary["label"].lower())
        self.assertEqual(self.evaluated["no-answer"]["activeCount"], 0)
        # An older backend still knows its permissions; what it cannot say is
        # whether they mean anything, and the label has to say that out loud.
        self.assertEqual(self.evaluated["no-runtime-block"]["activeCount"], 6)
        self.assertIn("unknown", self.evaluated["no-runtime-block"]["label"].lower())

    def test_a_schedule_that_slid_past_its_time_is_not_healthy(self):
        """The failure a user would otherwise notice only by absence."""
        plural = self.evaluated["overdue-plural"]
        self.assertEqual(plural["tone"], "degraded")
        self.assertIn("2 runs overdue", plural["label"])
        single = self.evaluated["overdue-single"]
        self.assertEqual(single["tone"], "degraded")
        self.assertIn("1 run overdue", single["label"], "no '1 runs'")

        # A missing runner is degraded too, but overdue work outranks it: an
        # absent module is a repair, an overdue run is something not done.
        runner = self.evaluated["runner-missing"]
        self.assertEqual(runner["tone"], "degraded")
        self.assertIn("scheduling unavailable", runner["label"])

    def test_a_healthy_host_says_so_and_counts_correctly(self):
        healthy = self.evaluated["healthy-with-schedule"]
        self.assertEqual(healthy["tone"], "live")
        self.assertIn("6 permissions", healthy["label"])
        self.assertIn("2 scheduled", healthy["label"])
        self.assertIn("1920x1080", healthy["detail"], "the label claims live; the detail proves it")

        one = self.evaluated["healthy-one-permission"]
        self.assertEqual(one["activeCount"], 1)
        self.assertIn("1 permission", one["label"])
        self.assertNotIn("1 permissions", one["label"])
        self.assertNotIn("scheduled", one["label"], "nothing scheduled is not worth a clause")

        # Zero granted on a working host is live-and-idle, not offline: the
        # desktop is reachable, the owner has simply not allowed anything yet.
        none = self.evaluated["healthy-no-permissions"]
        self.assertEqual(none["tone"], "live")
        self.assertEqual(none["activeCount"], 0)

    def test_source_checks_capability_before_it_checks_preference(self):
        """The ordering is the rule, so pin the order, not just the outputs.

        Output assertions alone would still pass if someone moved the healthy
        branch above the probe and the fixtures happened not to cover the gap.
        """
        source = self.SOURCE.read_text(encoding="utf-8")
        gui = source.index("if (!runtime.gui_control)")
        overdue = source.index("runtime.scheduled_due > 0")
        runner = source.index("if (!runtime.runner_available)")
        live = source.index("tone: 'live'")
        self.assertLess(gui, overdue, "a dead screen must be decided before the schedule")
        self.assertLess(overdue, runner)
        self.assertLess(runner, live, "nothing may report 'live' before every probe is read")
        # `gui_control` is the server's conjunction; the client must not re-derive
        # it from one half and call the desktop reachable.
        self.assertNotIn("screen && ", source)

    def test_the_backend_actually_probes_instead_of_declaring(self):
        """`runtime` has to be measured per request, or the badge lies again."""
        api = compact_source(self.API.read_text(encoding="utf-8"))
        self.assertIn("from .automation import execute_desktop_command, probe_gui_control", api)
        self.assertIn('"runtime": _automation_runtime(scheduled_actions)', api)
        # The GUI libraries stay behind automation.py. main.py importing pyautogui
        # is how the API layer starts failing to boot on a headless host.
        self.assertNotIn("import pyautogui", api)

        probe = compact_source(self.PROBE.read_text(encoding="utf-8"))
        self.assertIn("def probe_gui_control()", probe)
        self.assertIn("pyautogui.size()", probe)
        self.assertIn("gw.getActiveWindow()", probe)


class AppDiscoveryTests(unittest.TestCase):
    """Adopting whatever is on this machine, and any site.

    The registry in `app_connect` is 27 entries somebody typed. It answered "what
    can Akansha connect to" with a list, and it was measurably wrong about this
    machine: it reported "Discord Desktop was not found" while `Discord.lnk` sits in
    this user's Start Menu. These tests cover the scan that replaces the list, and
    the one honest mechanism for signing into an arbitrary site.

    Every filesystem root is injected, so this runs against a temp tree and asserts
    the *rules*. Running it against the real Start Menu would make the assertions
    depend on what happens to be installed.
    """

    def _tree(self, root, entries):
        for relative in entries:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def test_the_scan_keeps_applications_and_drops_the_tooling_around_them(self):
        """A list where "Uninstall Docker" sits next to "Docker" is read once.

        The noise is not hypothetical: filtering the real Start Menu here took 124
        shortcuts to 63 apps, and every rule below removed something that was
        actually present.
        """
        from backend.app_discovery import discover_desktop_apps

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._tree(
                root,
                [
                    "Discord.lnk",
                    "Docker/Docker Desktop.lnk",
                    "Docker/Uninstall Docker Desktop.lnk",
                    "Python 3.11/IDLE.lnk",
                    "Python 3.11/Python 3.11 Manuals (64-bit).lnk",
                    "Python 3.11/Module Docs.lnk",
                    "Administrative Tools/Event Viewer.lnk",
                    "Accessories/Character Map.lnk",
                    "System Tools/Control Panel.lnk",
                    "VLC/VideoLAN Website.lnk",
                    "VLC/VLC media player.lnk",
                    "VLC/VLC media player - reset preferences and cache files.lnk",
                    "Node.js/Install Additional Tools for Node.js.lnk",
                    "HelpScout.lnk",
                    "Bookmark.url",
                    "notes.txt",
                ],
            )
            found = {app.label for app in discover_desktop_apps(roots=[root], resolve=lambda p: "")}

        self.assertEqual(
            found,
            {
                "Discord",
                "Docker Desktop",
                "IDLE",
                "VLC media player",
                # Kept deliberately: a substring match on "help" would drop this, so
                # the pattern is anchored. An app named HelpScout is an app.
                "HelpScout",
            },
        )

    def test_a_url_shortcut_is_a_site_not_an_app(self):
        """`.url` is excluded so sites go through the strategy that can sign in.

        A launcher would open `mail.google.com` in the default browser, signed out,
        and call that a connection.
        """
        from backend.app_discovery import APP_SUFFIXES

        self.assertNotIn(".url", APP_SUFFIXES)

    def test_the_same_app_in_both_start_menus_is_listed_once(self):
        """Per-user and all-users installs both exist; the user should not see two."""
        from backend.app_discovery import discover_desktop_apps

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self._tree(Path(a), ["Google Chrome.lnk"])
            self._tree(Path(b), ["Google Chrome.lnk"])
            found = discover_desktop_apps(roots=[Path(a), Path(b)], resolve=lambda p: "")

        self.assertEqual([app.label for app in found], ["Google Chrome"])
        # The shortcut, not a resolved exe: Windows resolves the `.lnk` including the
        # working directory and arguments the installer chose.
        self.assertTrue(found[0].launch_target.endswith("Google Chrome.lnk"))

    def test_an_unreadable_shortcut_is_still_a_launchable_app(self):
        """`pywin32` missing must degrade to "we do not know the exe", not drop it."""
        from backend.app_discovery import discover_desktop_apps

        def explode(_path):
            raise OSError("no COM here")

        with tempfile.TemporaryDirectory() as raw:
            self._tree(Path(raw), ["Kiro.lnk"])
            found = discover_desktop_apps(roots=[Path(raw)], resolve=explode)

        self.assertEqual([app.label for app in found], ["Kiro"])
        self.assertEqual(found[0].exe_path, "")
        self.assertTrue(found[0].launch_target.endswith("Kiro.lnk"))

    def test_a_site_address_is_accepted_the_way_people_type_it(self):
        """People type `gmail.com`. Rejecting that is friction this feature exists to remove."""
        from backend.app_discovery import normalise_site_url

        for raw, expected in (
            ("gmail.com", "https://gmail.com"),
            ("  https://mail.google.com/mail/u/0/#inbox  ", "https://mail.google.com"),
            ("http://localhost:3000/dashboard", "http://localhost:3000"),
            ("https://www.notion.so?utm_source=x", "https://www.notion.so"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_site_url(raw), expected)

        for junk in ("", "   ", "not a url", "ftp://files.example.com", "localhostt", "://x"):
            with self.subTest(junk=junk):
                self.assertEqual(normalise_site_url(junk), "")

    def test_a_site_can_never_take_a_declared_app_id(self):
        """`web_` prefixed, so adopting `slack.com` cannot shadow the Slack entry."""
        from backend.app_connect import APP_CONNECTORS
        from backend.app_discovery import site_app_id

        for host in ("slack.com", "notion.so", "github.com", "spotify.com"):
            with self.subTest(host=host):
                self.assertTrue(site_app_id(host).startswith("web_"))
                self.assertNotIn(site_app_id(host), APP_CONNECTORS)

    def test_signed_in_means_a_cookie_for_that_host_exists_not_that_the_site_answers(self):
        """The false-Connected this replaces.

        Every public site returns 200 to a signed-out visitor, so an HTTP probe would
        have reported Gmail connected before anybody logged in.
        """
        from backend.app_discovery import profile_signed_in

        with tempfile.TemporaryDirectory() as raw:
            profile = Path(raw)
            self.assertFalse(profile_signed_in("https://gmail.com", profile_dir=profile))

            cookies = profile / "Default" / "Network" / "Cookies"
            cookies.parent.mkdir(parents=True, exist_ok=True)
            cookies.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
            self.assertFalse(profile_signed_in("https://gmail.com", profile_dir=profile))

            cookies.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64 + b"gmail.com" + b"\x00" * 64)
            self.assertTrue(profile_signed_in("https://gmail.com", profile_dir=profile))
            self.assertTrue(profile_signed_in("https://www.gmail.com", profile_dir=profile))
            self.assertFalse(profile_signed_in("https://notion.so", profile_dir=profile))

    def test_an_adopted_app_that_was_uninstalled_says_so(self):
        """The point of storing the path: it can be checked, and it can go stale."""
        from backend.app_connect import CONNECTED, NOT_INSTALLED, plan_connect
        from backend.app_discovery import DiscoveredApp, desktop_connector

        connector = desktop_connector(
            DiscoveredApp(app_id="cursor", label="Cursor", launch_target=r"C:\x\Cursor.lnk")
        )
        self.assertTrue(connector.adopted)

        alive = plan_connect(connector, {}, path_exists=lambda p: True)
        self.assertEqual(alive.outcome, CONNECTED)
        self.assertEqual(alive.connected_to, r"C:\x\Cursor.lnk")

        gone = plan_connect(connector, {}, path_exists=lambda p: False)
        self.assertEqual(gone.outcome, NOT_INSTALLED)
        self.assertIn("no longer exists", gone.detail)
        # Named, so the user can tell which of two Cursors went away.
        self.assertIn(r"C:\x\Cursor.lnk", gone.detail)

    def test_a_site_is_needs_sign_in_until_the_profile_holds_the_cookie(self):
        from backend.app_connect import CONNECTED, NEEDS_SIGN_IN, WEB_SESSION, plan_connect
        from backend.app_discovery import site_connector

        connector = site_connector("gmail.com")
        self.assertEqual(connector.strategy, WEB_SESSION)
        self.assertEqual(connector.fields, ())  # nothing to type: that is the point

        pending = plan_connect(connector, {}, session_ok=lambda _id: False)
        self.assertEqual(pending.outcome, NEEDS_SIGN_IN)
        self.assertIn("never through this app", pending.detail)

        live = plan_connect(connector, {}, session_ok=lambda _id: True)
        self.assertEqual(live.outcome, CONNECTED)
        self.assertEqual(live.connected_to, "https://gmail.com")

    def test_one_bad_stored_row_does_not_empty_the_whole_list(self):
        """Otherwise every app the user adopted vanishes, with no way to tell why."""
        from backend.app_discovery import connectors_from_records

        connectors = connectors_from_records(
            [
                {"kind": "desktop", "app_id": "cursor", "label": "Cursor", "launch_target": "C:/c.lnk"},
                {"kind": "web", "site_url": "notion.so", "label": "Notion"},
                {"kind": "web", "site_url": "not a url"},
                {"kind": "desktop", "label": ""},
                {"kind": "nonsense"},
                {},
            ]
        )
        self.assertEqual([c.app_id for c in connectors], ["cursor", "web_notion_so"])


class AppControlTests(unittest.TestCase):
    """Doing something once the connection is real."""

    def test_sign_in_opens_a_browser_that_is_not_being_automated(self):
        """Google refuses a CDP-driven Chrome, and is right to.

        So the sign-in step is a plain subprocess pointed at Akansha's profile: no
        debugging port, no automation flag, no headless. Only afterwards, with the
        cookie already in the profile, does Playwright attach. The other order
        produces a login page that cannot be logged into.
        """
        from backend.app_control import open_for_sign_in

        spawned = []
        with tempfile.TemporaryDirectory() as raw:
            result = open_for_sign_in(
                "https://gmail.com",
                profile_dir=Path(raw) / "profile",
                browser=r"C:\chrome.exe",
                spawn=lambda command, **kwargs: spawned.append(command),
            )

        self.assertTrue(result["ok"])
        command = spawned[0]
        self.assertEqual(command[0], r"C:\chrome.exe")
        self.assertEqual(command[-1], "https://gmail.com")
        self.assertTrue(any(part.startswith("--user-data-dir=") for part in command))
        for forbidden in (
            "--remote-debugging-port",
            "--enable-automation",
            "--headless",
            "--disable-web-security",
        ):
            with self.subTest(flag=forbidden):
                self.assertFalse(any(forbidden in part for part in command))
        # And no password anywhere near it.
        self.assertNotIn("password", " ".join(command).lower())

    def test_no_browser_is_a_sentence_not_a_traceback(self):
        from backend.app_control import open_for_sign_in

        result = open_for_sign_in(
            "https://x.com", profile_dir="/tmp/p", find=lambda: "", spawn=None
        )
        self.assertFalse(result["ok"])
        self.assertIn("No Chrome, Edge or Brave", result["detail"])

    def test_a_verb_is_checked_before_anything_moves(self):
        """Every one of these has a side effect, so a typo must be a message."""
        from backend.app_control import DESKTOP_VERBS, WEB_VERBS, desktop_action, web_action

        def never(*_args, **_kwargs):
            raise AssertionError("the runner must not be reached")

        for bad in ("nope", "", "LAUNCH; rm -rf /"):
            with self.subTest(verb=bad):
                self.assertFalse(
                    desktop_action(bad, launch_target="x", label="X", runner=never)["ok"]
                )
                self.assertFalse(
                    web_action(bad, url="https://x.com", profile_dir="/tmp", runner=never)["ok"]
                )

        # A click with nothing to click on, and typing with nothing to type.
        self.assertFalse(
            web_action("click", url="https://x.com", profile_dir="/tmp", runner=never)["ok"]
        )
        self.assertFalse(
            web_action("type", url="https://x.com", profile_dir="/tmp", selector="#q", runner=never)[
                "ok"
            ]
        )
        self.assertFalse(desktop_action("type_text", launch_target="x", label="X", runner=never)["ok"])
        # A desktop click with nothing named: coordinates are what this replaced.
        blind = desktop_action("click", launch_target="x", label="X", runner=never)
        self.assertFalse(blind["ok"])
        self.assertIn("needs the name", blind["detail"])
        self.assertEqual(
            set(DESKTOP_VERBS), {"launch", "focus", "type_text", "click", "read_window", "close"}
        )
        self.assertEqual(set(WEB_VERBS), {"open", "navigate", "type", "click", "read_page"})

    def test_launching_a_path_that_is_gone_never_reaches_the_shell(self):
        """Checked here as well as in `plan_connect`, because this one has the side effect."""
        from backend.app_control import desktop_action

        def never(*_args, **_kwargs):
            raise AssertionError("the runner must not be reached")

        result = desktop_action(
            "launch", launch_target=r"C:\gone\Cursor.lnk", label="Cursor", runner=never
        )
        self.assertFalse(result["ok"])
        self.assertIn("no longer exists", result["detail"])
        self.assertIn("Re-scan", result["detail"])

    def test_a_profile_already_in_use_says_close_the_window(self):
        """Chrome allows one process per profile. The Playwright error names a lock file."""
        from backend.app_control import web_action

        def locked(*_args, **_kwargs):
            raise RuntimeError("ProcessSingleton: failed to create /Default/SingletonLock")

        result = web_action("open", url="https://x.com", profile_dir="/tmp", runner=locked)
        self.assertFalse(result["ok"])
        self.assertIn("close the sign-in window", result["detail"])
        self.assertNotIn("SingletonLock", result["detail"])

    def test_the_capability_list_and_the_verb_table_agree(self):
        """A capability the UI offers and the driver rejects is a button that fails."""
        from backend.app_control import DESKTOP_VERBS, WEB_VERBS
        from backend.app_discovery import DiscoveredApp, desktop_connector, site_connector

        desktop = desktop_connector(DiscoveredApp(app_id="x", label="X", launch_target="x"))
        self.assertEqual(set(desktop.capabilities), set(DESKTOP_VERBS))
        self.assertEqual(set(site_connector("example.com").capabilities), set(WEB_VERBS))

    def test_a_declared_desktop_app_can_be_closed_and_brought_to_the_front(self):
        """The connections page offers four desktop buttons; three of them 400'd.

        The declared capability tuples on the built-in desktop entries were written
        for the *automation engine* -- `open_url`, `new_tab`, `open_chat` -- and
        `/api/apps/control` gates on them. So not one of the twelve declared `close`,
        and only three declared `focus`, while the page rendered Launch / Bring to
        front / Type / Close on every single one. What a window can be asked to do is
        a property of the route, so `effective_capabilities` contributes it.
        """
        from backend.app_connect import APP_CONNECTORS, LOCAL_EXECUTABLE, effective_capabilities
        from backend.app_control import DESKTOP_VERBS

        desktop = [c for c in APP_CONNECTORS.values() if c.strategy == LOCAL_EXECUTABLE]
        self.assertTrue(desktop)
        for connector in desktop:
            with self.subTest(app=connector.app_id):
                allowed = effective_capabilities(connector)
                for verb in DESKTOP_VERBS:
                    self.assertIn(verb, allowed)
                # The declared list is still first and still intact -- the catalog
                # shows what the connector chose, with the route verbs appended.
                self.assertEqual(allowed[: len(connector.capabilities)], connector.capabilities)

    def test_an_api_key_connector_gains_no_verbs(self):
        """OpenRouter has no window and no page. Merging verbs in would be a lie."""
        from backend.app_connect import APP_CONNECTORS, effective_capabilities

        openrouter = APP_CONNECTORS["openrouter"]
        self.assertEqual(effective_capabilities(openrouter), openrouter.capabilities)


class AppOperatorTests(unittest.TestCase):
    """Doing something *through* a connected app, said in a sentence.

    The gap these cover, measured before they were written: `/api/apps/control` had
    exactly one caller in the repository -- the buttons on the connections page. So
    signing into a site bought a green dot and a row of buttons, and saying "post
    this on X" went down `build_browser_prompt_plan`, which knows nothing about the
    registry and would launch an application or write a file instead. *"means own
    path after sign can excahnage pass control informatiion webs and all after sign
    in so it caqn control the web o rapp completely by its architecture design in
    that way add reasoning decision making and all"*.
    """

    def _grant(self, app_id, *, live=True, launch_target="C:/app.exe"):
        from backend.app_connect import APP_CONNECTORS, CONNECTED
        from backend.app_operator import grant_for

        connector = APP_CONNECTORS[app_id]
        return grant_for(
            connector,
            {
                "outcome": CONNECTED if live else "needs_sign_in",
                "detail": "Signed in." if live else "One click opens the site.",
                "connected_to": connector.web_url or connector.site_url,
            },
            launch_target=launch_target,
        )

    def test_a_sentence_names_its_app_wherever_the_name_sits(self):
        """`resolve_app_id` answers "is this string an app name". A sentence isn't one.

        "send amma a whatsapp message" carries the name in the middle, where the
        prefix/suffix match in `resolve_app_id` cannot see it -- and the two most-used
        connectors in the registry, X and Google, declare no aliases at all: their
        names live inside the labels "X / Twitter" and "Google (Gmail, Calendar,
        Drive)". Until the label was split, neither was reachable by voice.
        """
        from backend.app_operator import OP_POST, OP_SEARCH, OP_SEND, understand

        cases = [
            ('post "ship it" on X', "twitter", OP_POST, "ship it", ""),
            ("send a whatsapp message to Amma saying I am on my way",
             "whatsapp_bridge", OP_SEND, "I am on my way", "Amma"),
            ("search for fastapi middleware on github", "github", OP_SEARCH,
             "fastapi middleware", ""),
            ("read my gmail", "google", "read", "", ""),
        ]
        for utterance, app_id, operation, payload, recipient in cases:
            with self.subTest(utterance=utterance):
                intent = understand(utterance)
                self.assertEqual(intent.app_id, app_id)
                self.assertEqual(intent.operation, operation)
                self.assertEqual(intent.payload, payload)
                self.assertEqual(intent.recipient, recipient)

    def test_the_recipient_stops_before_the_message(self):
        """"to Amma saying I'm late" is one recipient and one message, not one blob.

        A greedy `to (.+)` makes the recipient "Amma saying I'm", which is then typed
        into WhatsApp's contact search and finds nobody -- so the message would be
        typed into whichever chat happened to already be open. Worth its own test:
        the failure is silent and lands in a stranger's chat.
        """
        from backend.app_operator import understand

        intent = understand("send a whatsapp message to Amma saying I am on my way")
        self.assertEqual(intent.recipient, "Amma")
        self.assertNotIn("saying", intent.recipient)
        self.assertNotIn("Amma", intent.payload)

    def test_a_verb_that_names_a_service_is_recorded_as_an_assumption(self):
        """"Tweet the release notes" names no app, and everyone knows which one.

        Inferring it is right; inferring it *silently* is not, because the correction
        has to be possible in one word. So `assumed_app` is set and the reasoning says
        which app was picked.
        """
        from backend.app_operator import decide, understand

        intent = understand("tweet that the build is green")
        self.assertEqual(intent.app_id, "twitter")
        self.assertTrue(intent.assumed_app)
        self.assertEqual(intent.payload, "the build is green")
        reasoning = " ".join(decide(intent, self._grant("twitter")).reasoning)
        self.assertIn("tweet", reasoning)
        self.assertIn("X / Twitter", reasoning)

    def test_the_browser_route_wins_a_tie_with_the_bot_route(self):
        """"message the dev channel on discord" means the user's account, not a bot.

        `discord_bot` and `discord_web` both answer to "discord". A bot token cannot
        read the person's own chats, so resolving to it would be a connection that
        exists and cannot do the thing asked. The whole point of the sign-in work is
        that the browser route is the one that acts as *you*.
        """
        from backend.app_operator import understand

        self.assertEqual(understand("message the dev channel on discord").app_id, "discord_web")
        self.assertEqual(understand("message amma on telegram").app_id, "telegram_web")

    def test_the_grant_carries_the_route_and_refuses_to_re_decide_liveness(self):
        """The control information a connection hands onward.

        Route and `live` both come from the `plan_connect` result, so a caller cannot
        act on an app that stopped being connected between the status read and the
        action -- and cannot form a second opinion about whether it is connected.
        """
        from backend.app_operator import ROUTE_API, ROUTE_BROWSER, ROUTE_DESKTOP

        self.assertEqual(self._grant("twitter").route, ROUTE_BROWSER)
        self.assertEqual(self._grant("notepad").route, ROUTE_DESKTOP)
        self.assertEqual(self._grant("openrouter").route, ROUTE_API)
        self.assertFalse(self._grant("twitter", live=False).live)
        # Browser grants only ever carry browser verbs, desktop grants desktop ones.
        # A grant is what the executor reads, so a leaked verb here becomes a 400 at
        # the far end of a plan that had already opened a window.
        from backend.app_control import DESKTOP_VERBS, WEB_VERBS

        self.assertTrue(set(self._grant("twitter").verbs) <= set(WEB_VERBS))
        self.assertTrue(set(self._grant("notepad").verbs) <= set(DESKTOP_VERBS))

    def test_nothing_runs_until_the_connection_is_live(self):
        """Refused *before* the plan, and `open` is deliberately still allowed.

        Opening the sign-in page is how you stop being disconnected, so refusing that
        too would make the disconnected state unrecoverable by voice.
        """
        from backend.app_operator import decide, understand

        cold = decide(understand('post "ship it" on X'), self._grant("twitter", live=False))
        self.assertFalse(cold.runnable)
        self.assertEqual(cold.steps, [])
        self.assertIn("isn't connected", cold.blocked)
        self.assertTrue(cold.needs)

        opening = decide(understand("open X"), self._grant("twitter", live=False))
        self.assertTrue(opening.runnable)
        self.assertEqual([s.verb for s in opening.steps], ["open"])

    def test_an_api_key_connection_is_refused_with_the_right_reason(self):
        """"There is nothing to click here" is not "it is not connected"."""
        from backend.app_operator import decide, understand

        decision = decide(understand("post something on openrouter"), self._grant("openrouter"))
        self.assertFalse(decision.runnable)
        self.assertIn("API key", decision.blocked)
        self.assertNotIn("not connected", decision.blocked)

    def test_a_url_is_preferred_over_typing_into_a_search_box(self):
        """Selectors rot; a query string does not.

        So "play lofi beats on spotify" navigates to the search URL and reads the
        result, rather than clicking a search field and typing into it.
        """
        from backend.app_operator import decide, understand

        decision = decide(understand("play lofi beats on spotify"), self._grant("spotify"))
        self.assertTrue(decision.runnable)
        self.assertEqual([s.verb for s in decision.steps], ["open", "read_page"])
        self.assertEqual(decision.steps[0].url, "https://open.spotify.com/search/lofi+beats")
        self.assertIn("query string", " ".join(decision.reasoning))

    def test_an_unknown_site_gets_read_rather_than_a_fabricated_selector(self):
        """A site nobody wrote a recipe for is still openable and readable.

        That is a real capability and the model can decide what to do from the text it
        gets back. Inventing a selector would fail *after* opening a window, which is
        strictly worse -- and would report a site as broken when it is merely
        undocumented.
        """
        from backend.app_operator import decide, playbook_for, understand

        self.assertIsNone(playbook_for("www.notion.so", "post"))
        decision = decide(understand("post a status update on notion"), self._grant("notion"))
        self.assertTrue(decision.runnable)
        self.assertEqual([s.verb for s in decision.steps], ["open", "read_page"])
        self.assertTrue(decision.needs)
        self.assertIn("no declared", decision.summary)

    def test_an_empty_body_is_refused_rather_than_posted(self):
        """A misheard sentence must not become a visible empty post."""
        from backend.app_operator import decide, understand

        decision = decide(understand("post on X"), self._grant("twitter"))
        self.assertFalse(decision.runnable)
        self.assertIn("did not catch", decision.blocked)

    def test_whatsapp_needs_a_contact_before_it_types_anything(self):
        """A blank contact search leaves whichever chat was open selected.

        Which means the message lands in a stranger's chat. Refused, not attempted.
        """
        from backend.app_operator import decide, understand

        intent = understand('send "running late" on whatsapp')
        self.assertEqual(intent.recipient, "")
        decision = decide(intent, self._grant("whatsapp_bridge"))
        self.assertFalse(decision.runnable)
        self.assertIn("who to send", decision.blocked)

    def test_no_playbook_presses_send_on_a_message(self):
        """Typed into the right chat, and the person presses send.

        `commit_step` is -1 on every messaging playbook and the verb sets contain no
        Enter, so a misheard contact name cannot deliver a real message to the wrong
        person. A deliberate stop, asserted so it cannot be removed by accident.
        """
        from backend.app_operator import OP_SEND, SITE_PLAYBOOKS

        for host, books in SITE_PLAYBOOKS.items():
            for book in books:
                if book.operation != OP_SEND:
                    continue
                with self.subTest(host=host):
                    self.assertEqual(book.commit_step, -1)

    def test_the_irreversible_step_is_held_back_by_default(self):
        """Posting publicly is the one thing in here that a closed tab cannot undo.

        So the plan is run up to it and stopped, and the caller has to ask again. The
        hold point comes from the playbook rather than from the verb, because "click"
        is harmless on a search page and publishes on the compose page.
        """
        from backend.main import _operator_hold_point
        from backend.app_operator import decide, understand

        decision = decide(understand('post "ship it" on X'), self._grant("twitter"))
        self.assertEqual([s.verb for s in decision.steps], ["open", "click", "type", "click"])
        hold = _operator_hold_point(decision)
        self.assertEqual(hold, 3)
        held = [s.verb for s in decision.steps[:hold]]
        self.assertEqual(held, ["open", "click", "type"])
        self.assertIn("irreversible", " ".join(decision.reasoning))

        # A search has nothing to hold back.
        self.assertIsNone(
            _operator_hold_point(decide(understand("search for x on github"), self._grant("github")))
        )

    def test_execute_never_reaches_a_real_browser_or_a_real_mouse(self):
        """Both runners are arguments with no defaults, and this is why.

        A default of `web_action` would put "launch Chrome on the user's desktop" one
        forgotten keyword argument away from the unit tests. Here the whole plan runs
        against recorders, so the sequence, the substituted text and the URL are all
        checked without a window opening.
        """
        from backend.app_operator import decide, execute, understand

        calls = []

        def fake_web(verb, **kwargs):
            calls.append((verb, kwargs))
            return {"ok": True, "detail": f"{verb} ok", "text": "page text" if verb == "read_page" else ""}

        decision = decide(understand('post "ship it" on X'), self._grant("twitter"))
        report = execute(decision, run_web=fake_web, run_desktop=self.fail, up_to=3)
        self.assertTrue(report["ok"])
        self.assertTrue(report["truncated"])
        self.assertEqual([c[0] for c in calls], ["open", "click", "type"])
        self.assertEqual(calls[0][1]["url"], "https://x.com/compose/post")
        self.assertEqual(calls[2][1]["text"], "ship it")
        # The URL carries forward: only the first step names it, and every later step
        # has to act on the same page rather than on the connector's home page.
        self.assertEqual(calls[2][1]["url"], "https://x.com/compose/post")
        self.assertIn("irreversible", " ".join(report["reasoning"]))

    def test_a_failed_selector_is_diagnosed_as_a_stale_recipe(self):
        """Sites redesign. "The app is broken" would send the user to re-authenticate.

        A step that cannot find its selector says which selector, on which host, and
        that the recipe may be out of date -- and the whole run reports failure. What
        it must never do is report success with steps missing.
        """
        from backend.app_operator import decide, execute, understand

        def fake_web(verb, **kwargs):
            if verb == "click":
                return {"ok": False, "detail": "Timeout 15000ms exceeded."}
            return {"ok": True, "detail": "ok"}

        report = execute(
            decide(understand('post "ship it" on X'), self._grant("twitter")),
            run_web=fake_web,
            run_desktop=self.fail,
        )
        self.assertFalse(report["ok"])
        self.assertIn("tweetTextarea_0", report["summary"])
        self.assertIn("x.com", report["summary"])
        self.assertIn("may have changed", report["summary"])
        self.assertEqual(len(report["steps"]), 2)

    def test_stopping_is_honoured_between_steps_and_says_so(self):
        """"Stop" means "stop before the next step" -- a sent keystroke cannot be recalled."""
        from backend.app_operator import decide, execute, understand

        seen = []

        def fake_web(verb, **kwargs):
            seen.append(verb)
            return {"ok": True, "detail": "ok"}

        report = execute(
            decide(understand('post "ship it" on X'), self._grant("twitter")),
            run_web=fake_web,
            run_desktop=self.fail,
            stop=lambda: len(seen) >= 2,
        )
        self.assertTrue(report["cancelled"])
        self.assertEqual(seen, ["open", "click"])
        self.assertIn("Stopped after 2", report["summary"])

    def test_the_desktop_route_reads_a_window_instead_of_refusing_to(self):
        """It used to say "it has no way to see the screen contents". That stopped being true.

        `desktop_ui` reads the accessibility tree, so `read notepad` plans a
        `read_window` step. The refusal did not disappear -- it moved to the runner,
        which is the only place that knows whether *this* window publishes a tree.
        """
        from backend.app_operator import decide, understand

        decision = decide(understand("read notepad"), self._grant("notepad"))
        self.assertTrue(decision.runnable)
        self.assertEqual([s.verb for s in decision.steps], ["read_window"])
        self.assertIn("accessibility tree", " ".join(decision.reasoning))

        finding = decide(understand("search for invoices in notepad"), self._grant("notepad"))
        self.assertEqual([s.verb for s in finding.steps], ["focus", "type_text"])
        self.assertEqual(finding.steps[1].selector, "Search")
        self.assertEqual(finding.steps[1].text, "invoices")
        # and it stops there, the way the browser route stops before pressing send
        self.assertIn("presses Enter", " ".join(finding.reasoning))

        typing = decide(understand("type hello world into notepad"), self._grant("notepad"))
        self.assertEqual([s.verb for s in typing.steps], ["focus", "type_text"])
        self.assertEqual(typing.steps[1].text, "hello world")
        # Focus first, and the reasoning says why: typing goes to whatever has focus.
        self.assertIn("focus", " ".join(s.why for s in typing.steps).lower())
        self.assertIn("cursor", " ".join(typing.reasoning))

    def test_an_unclear_sentence_is_read_not_guessed_at(self):
        """The safe interpretation of an unclear sentence has no side effect."""
        from backend.app_operator import decide, understand

        decision = decide(understand("summarise my notion workspace"), self._grant("notion"))
        self.assertEqual([s.verb for s in decision.steps], ["open", "read_page"])
        self.assertIn("could not tell", " ".join(decision.reasoning))

    def test_the_voice_path_asks_the_registry_before_the_plan_builder(self):
        """The bridge, and the reason the sign-in was previously inert.

        `/api/apps/control` had exactly one caller -- the connections page -- so a
        signed-in site could only be driven by clicking buttons there. `_run_automation`
        now offers the registry first refusal, and returns `None` for anything it does
        not recognise so it cannot become a second, worse plan builder.
        """
        source = (Path(__file__).resolve().parent / "voice_executor.py").read_text(encoding="utf-8")
        self.assertIn("def _try_connected_app", source)
        self.assertIn("connected = _try_connected_app(prompt, stop)", source)
        self.assertLess(
            source.index("connected = _try_connected_app"),
            source.index("plan = build_browser_prompt_plan(prompt)"),
        )
        self.assertIn("return None  # No app named. Not ours.", source)

    def test_the_operate_endpoint_is_owner_gated_and_can_be_dry_run(self):
        """Standing authority over someone's real accounts. The turn has to say who."""
        api = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
        route = api[api.index('@app.post("/api/apps/operate")'):]
        route = route[: route.index("\ndef _operator_hold_point")]
        self.assertIn('_require_owner_for_apps(req.speaker_profile, "Operating an app")', route)
        self.assertIn("if req.dry_run or not decision.runnable:", route)
        # The runners are bound in exactly one place, and it is the only function in
        # the API that is allowed to touch the real browser and the real mouse.
        binder = api[api.index("def _run_decision("):api.index("def _operate_if_connected(")]
        self.assertIn("profile_dir=profile", binder)
        self.assertIn("from .app_control import desktop_action, web_action", binder)
        self.assertIn("hold_at_commit", api)

    def test_the_typed_automation_route_asks_the_registry_first_too(self):
        """Chat is not the voice socket, and it had the same hole.

        `/api/automation/browser/prompt` is where a typed "post this on X" lands, and
        its plan builder knows nothing about the registry either. The check has to sit
        above the Playwright bypass, or a site with a scripted skill would be driven in
        a fresh browser that is not signed in.
        """
        api = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
        route = api[api.index('@app.post("/api/automation/browser/prompt")'):]
        route = route[: route.index("\n@app.")]
        # Threaded, not inline. This asserted the inline call until the inline call
        # turned out to be the reported `open failed: It looks like you are using
        # Playwright Sync API inside the asyncio loop` -- `_operate_if_connected`
        # ends in sync Playwright and this route is `async def`. The ordering
        # assertions below are the part of this test that was always the point, and
        # they are untouched; see tests/test_operate_off_the_loop.py for the thread.
        self.assertIn("await asyncio.to_thread(_operate_if_connected, db, prompt)", route)
        self.assertLess(route.index("_operate_if_connected"), route.index("should_use_playwright"))
        self.assertLess(route.index("_operate_if_connected"), route.index("build_browser_prompt_plan"))
        # Unrecognised prompts fall through untouched.
        helper = api[api.index("def _operate_if_connected("):]
        helper = helper[: helper.index("\n@app.")]
        self.assertIn("if decision.grant is None:\n        return None", helper)


class GoalPursuitTests(unittest.TestCase):
    """Setting a goal and actually reaching it.

    The gap these cover, measured before they were written: `grep -rn app_operator
    backend/hermes` returned nothing, and `"app_control"` appeared inside the
    cognitive OS only as a string in a capability list. The goal engine could hold
    a goal, decompose it, remember it and draw lessons from it -- and never move
    it, because it had no route to the layer that drives a real window. *"Setting
    the goals is the goal. Set the goals and reach the target."*
    """

    def _decision(self, text="t", *, runnable=True, grant=True, blocked="", live=True):
        from backend.app_operator import ControlDecision, ControlGrant, ControlStep, Intent

        made = ControlGrant(
            app_id="x", label="X / Twitter", route="browser", target="https://x.com",
            verbs=("open", "navigate", "type", "click", "read_page"), live=live,
        )
        return ControlDecision(
            intent=Intent(utterance=text, operation="post", app_id="x"),
            grant=made if grant else None,
            steps=[ControlStep(verb="open", url="https://x.com")] if runnable else [],
            reasoning=["Chose the browser route."],
            blocked=blocked,
            summary="Post it on X",
        )

    def _nothing(self, _text):
        from backend.app_operator import ControlDecision, Intent

        return ControlDecision(intent=Intent(utterance="t", operation="unknown"), grant=None)

    def test_one_sentence_becomes_the_separate_things_it_asked_for(self):
        """A goal is usually several instructions in one breath.

        Splitting is conservative on purpose: a missed split leaves one larger
        action the operator will either handle or decline out loud, while a wrong
        split invents an instruction that was never given.
        """
        from backend.goal_pursuit import split_intentions

        got = split_intentions("open whatsapp and then message amma; also search github for playwright")
        self.assertEqual(got, ["open whatsapp", "message amma", "search github for playwright"])
        # A bare conjunction left over from a split is not an instruction.
        self.assertNotIn("and", split_intentions("do this and then that"))

    def test_a_spoken_goal_keeps_its_horizon_and_every_word_of_it(self):
        from backend.goal_pursuit import goal_from_utterance

        read = goal_from_utterance("my goal is to ship the voice agent by friday and then write the docs")
        self.assertEqual(read["title"], "ship the voice agent by friday")
        self.assertEqual(read["horizon"], "week")
        # Nothing said is dropped: the second instruction survives in the context.
        self.assertIn("write the docs", read["context"])
        self.assertEqual(len(read["intentions"]), 2)

    def test_template_prose_is_never_reported_as_work_done(self):
        """The goal graph writes five generic milestones for every goal.

        "Clarify objective" and "Learn and optimize" describe the shape of any
        project rather than anything to do. Classifying them as operations would
        make every goal look 60% finished the moment it was created.
        """
        from backend.goal_pursuit import KIND_MANUAL, classify

        for prose in ("Milestone 1: Clarify objective", "Learn and optimize", "Validate output"):
            action = classify(prose, resolve=self._nothing)
            self.assertEqual(action.kind, KIND_MANUAL, prose)
            self.assertFalse(action.runnable)
            self.assertIn("placeholder", " ".join(action.reasoning))

    def test_a_step_that_is_someones_decision_stops_and_says_so(self):
        from backend.goal_pursuit import KIND_MANUAL, classify

        action = classify("decide which offer to accept", resolve=self._nothing)
        self.assertEqual(action.kind, KIND_MANUAL)
        self.assertIn("your call", " ".join(action.reasoning))

    def test_a_lesson_from_last_time_is_read_before_the_step_not_after(self):
        """Learning that only lands after the failure is a diary, not learning."""
        from backend.goal_pursuit import classify

        lessons = [{
            "task": "post the release notes on twitter",
            "failure": "the compose box was not found",
            "fix": "open the compose URL directly instead of clicking New Post",
        }]
        action = classify(
            "post the release notes on twitter",
            resolve=lambda t: self._decision(),
            lessons=lessons,
        )
        joined = " ".join(action.reasoning)
        self.assertIn("Last time this failed", joined)
        self.assertIn("open the compose URL directly", joined)

    def test_an_unrelated_lesson_is_not_attached_to_the_wrong_step(self):
        """Confident advice on the wrong step is worse than silence."""
        from backend.goal_pursuit import classify

        lessons = [{"task": "book a flight to chennai", "failure": "no seats", "fix": "try the next morning"}]
        action = classify("post the release notes on twitter", resolve=lambda t: self._decision(), lessons=lessons)
        self.assertNotIn("Last time this failed", " ".join(action.reasoning))

    def test_nothing_connected_is_read_up_on_rather_than_guessed_at(self):
        from backend.goal_pursuit import KIND_MANUAL, KIND_RESEARCH, classify

        # With no researcher available it must say it needs a person, not invent a click.
        alone = classify("what changed in playwright 1.49", resolve=self._nothing)
        self.assertEqual(alone.kind, KIND_MANUAL)
        self.assertIn("needs you", " ".join(alone.reasoning))

        looked_up = classify(
            "what changed in playwright 1.49",
            resolve=self._nothing,
            research=lambda t: self._decision(),
        )
        self.assertEqual(looked_up.kind, KIND_RESEARCH)
        self.assertIn("looked it up instead of guessing", " ".join(looked_up.reasoning))

    def _goal(self, **over):
        base = {
            "id": "goal_1",
            "title": "ship the voice agent",
            "goal_context": "open whatsapp and then message amma",
            "tasks": [
                {"id": "t1", "title": "Milestone 1: Clarify objective", "status": "pending"},
                {"id": "t2", "title": "post the notes on twitter", "status": "pending"},
                {"id": "t3", "title": "search github for playwright", "status": "done"},
            ],
        }
        base.update(over)
        return base

    def test_a_concrete_instruction_is_not_lost_behind_generic_milestones(self):
        """The template's five milestones must not crowd out what was actually said."""
        from backend.goal_pursuit import plan_pursuit

        plan = plan_pursuit(self._goal(), resolve=lambda t: self._decision())
        texts = [a.text for a in plan.actions]
        self.assertIn("open whatsapp", texts)
        self.assertIn("message amma", texts)
        # A finished task is not queued again.
        self.assertNotIn("search github for playwright", texts)

    def test_the_step_that_cannot_be_undone_is_held_before_not_after(self):
        """A goal ending in "post it" drafts the post and stops, like a spoken one."""
        from backend.goal_pursuit import plan_pursuit

        plan = plan_pursuit(
            self._goal(),
            resolve=lambda t: self._decision(t),
            hold_point=lambda d: 3 if "twitter" in d.intent.utterance else None,
        )
        self.assertIsNotNone(plan.hold_at)
        self.assertIn("cannot be undone", " ".join(plan.reasoning))

    def test_pursuing_records_what_happened_whether_it_worked_or_not(self):
        """A failure with a reason teaches more than a clean run, so both are filed."""
        from backend.goal_pursuit import plan_pursuit, pursue

        learned: list[dict] = []
        marked: list[tuple[str, str]] = []
        plan = plan_pursuit(self._goal(), resolve=lambda t: self._decision())
        report = pursue(
            plan,
            run=lambda d: {"ok": False, "summary": "the selector was not found", "steps": [], "route": "browser"},
            learn=learned.append,
            mark_task=lambda tid, status: marked.append((tid, status)),
        )
        self.assertEqual(report["completed"], 0)
        self.assertGreater(report["failed"], 0)
        self.assertTrue(learned, "a failed step must still be recorded as experience")
        self.assertFalse(learned[0]["task_success"])
        self.assertIn("the selector was not found", learned[0]["errors"][0])
        self.assertIn(("t2", "blocked"), marked)

    def test_pursuit_stops_at_its_cap_rather_than_running_until_it_decides_it_is_done(self):
        """An agent that cannot be interrupted is the failure mode, not slowness."""
        from backend.goal_pursuit import plan_pursuit, pursue

        many = self._goal(goal_context="; ".join(f"open app{i}" for i in range(10)), tasks=[])
        plan = plan_pursuit(many, resolve=lambda t: self._decision())
        report = pursue(plan, run=lambda d: {"ok": True, "summary": "done"}, learn=lambda p: None, max_actions=3)
        self.assertEqual(len(report["results"]), 3)
        self.assertIn("limit", " ".join(report["reasoning"]))
        self.assertTrue(report["remaining"])

    def test_stopping_a_goal_is_honoured_between_steps_and_admitted(self):
        from backend.goal_pursuit import plan_pursuit, pursue

        plan = plan_pursuit(self._goal(), resolve=lambda t: self._decision())
        report = pursue(plan, run=lambda d: {"ok": True}, learn=lambda p: None, stop=lambda: True)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["completed"], 0)
        self.assertIn("stopped me", " ".join(report["reasoning"]))

    def test_a_goal_made_only_of_prose_reports_that_none_of_it_is_mine_to_do(self):
        """Better than a cheerful 0%: it names how many steps are waiting on a person."""
        from backend.goal_pursuit import plan_pursuit, pursue

        prose = self._goal(
            goal_context="Clarify objective",
            tasks=[{"id": "t1", "title": "Validate output", "status": "pending"}],
        )
        plan = plan_pursuit(prose, resolve=self._nothing)
        report = pursue(plan, run=lambda d: {"ok": True}, learn=lambda p: None)
        self.assertFalse(report["ran"])
        self.assertIn("need you", report["summary"])
        self.assertTrue(report["needs_you"])

    def test_the_pursuit_engine_cannot_reach_a_browser_on_its_own(self):
        """Same rule as the operator: the module is pure, the API binds the runners."""
        source = (Path(__file__).resolve().parent / "goal_pursuit.py").read_text(encoding="utf-8")
        for forbidden in ("web_action", "desktop_action", "playwright", "pyautogui", "subprocess", "webbrowser"):
            self.assertNotIn(forbidden, source, f"goal_pursuit must not reference {forbidden}")
        # And the runners are required arguments, so no default can smuggle one in.
        self.assertIn("run: Callable[[ControlDecision], dict[str, Any]],", source)
        self.assertIn("learn: Callable[[dict[str, Any]], None],", source)

    def test_a_goal_only_starts_when_it_was_actually_declared_as_one(self):
        """"Open WhatsApp" is an instruction. Turning it into a goal would make
        every sentence unpredictable, so the voice path demands a declaration."""
        import threading

        from backend.voice_executor import _try_goal

        self.assertIsNone(_try_goal("open whatsapp", threading.Event()))
        self.assertIsNone(_try_goal("what is the weather", threading.Event()))

    def test_research_needs_no_account_and_can_only_read(self):
        """Reading a public page is honest without a sign-in -- but it must never
        become a way to act on a site nobody authenticated."""
        from backend.main import _research_decision

        decision = _research_decision("what changed in playwright 1.49")
        self.assertTrue(decision.grant.live)
        self.assertEqual(sorted(decision.grant.verbs), ["open", "read_page"])
        self.assertEqual([s.verb for s in decision.steps], ["open", "read_page"])
        for step in decision.steps:
            self.assertNotIn(step.verb, ("type", "click"))

    def test_the_goal_routes_are_owner_gated_and_bind_the_one_real_runner(self):
        api = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
        for route in ('@app.post("/api/goals/set")', '@app.post("/api/goals/{goal_id}/pursue")'):
            slice_ = api[api.index(route):]
            slice_ = slice_[: slice_.index("\n@app.")] if "\n@app." in slice_ else slice_
            self.assertIn("_require_owner_for_apps", slice_)
        runner = api[api.index("def _pursue_goal("):]
        runner = runner[: runner.index("\n@app.")]
        self.assertIn("run=lambda decision: _run_decision(", runner)
        self.assertIn("learn=lambda payload: brain.experiences.record(payload)", runner)

    def test_the_search_window_offers_an_action_and_never_acts_while_typing(self):
        """A result you cannot do anything with is worse than no result -- but the
        act row is resolved as a decision only, so typing never runs anything."""
        api = (Path(__file__).resolve().parent / "main.py").read_text(encoding="utf-8")
        route = api[api.index('@app.get("/api/search/omni")'):]
        route = route[: route.index("\n@app.")]
        self.assertIn("operate_decision(db, query)", route)
        self.assertNotIn("_run_decision", route)
        self.assertNotIn("operator_execute", route)

    def test_a_heading_is_never_turned_into_a_web_search(self):
        """Found by running it: the first live plan classified all six product
        milestones as research and would have opened six results pages for noun
        phrases like "Backend implementation". Research answers a question."""
        from backend.goal_pursuit import KIND_MANUAL, KIND_RESEARCH, classify

        for heading in ("Backend implementation", "Architecture and data model", "Testing and quality gates"):
            action = classify(heading, resolve=self._nothing, research=lambda t: self._decision(t))
            self.assertEqual(action.kind, KIND_MANUAL, heading)

        for question in ("what changed in playwright 1.49", "look up the train times", "how much is a visa"):
            action = classify(question, resolve=self._nothing, research=lambda t: self._decision(t))
            self.assertEqual(action.kind, KIND_RESEARCH, question)

    def test_the_goal_is_not_queued_as_one_of_its_own_steps(self):
        """Also found live: "ship the voice agent by friday" appeared as a step,
        which could only ever be marked done by fiat."""
        from backend.goal_pursuit import plan_pursuit

        goal = self._goal(
            title="ship the voice agent",
            goal_context="my goal is to ship the voice agent, and post the notes on twitter",
            tasks=[],
        )
        plan = plan_pursuit(goal, resolve=lambda t: self._decision(t))
        texts = [a.text.lower() for a in plan.actions]
        self.assertNotIn("ship the voice agent", texts)
        self.assertIn("post the notes on twitter", texts)
        # The opener is stripped, so no step begins "my goal is to".
        for text in texts:
            self.assertFalse(text.startswith("my goal is"), text)

    def test_two_instructions_joined_by_and_split_only_when_the_second_is_one(self):
        """"message Amma and Nanna" is one instruction with two recipients;
        "post this and search github" is two. The tell is the verb."""
        from backend.goal_pursuit import split_intentions

        self.assertEqual(
            split_intentions("post the release notes on twitter and search github for playwright"),
            ["post the release notes on twitter", "search github for playwright"],
        )
        self.assertEqual(
            split_intentions("message Amma and Nanna about dinner"),
            ["message Amma and Nanna about dinner"],
        )

    def test_a_doubt_is_asked_as_a_question_not_stated_as_a_problem(self):
        """"I could not match that" is true and useless -- it leaves the person
        guessing what to say back. Every unresolved action carries the question."""
        from backend.goal_pursuit import classify

        unknown = classify("frobnicate the widget", resolve=self._nothing)
        self.assertTrue(unknown.question.endswith("?"), unknown.question)
        self.assertIn("which one should I open", unknown.question)

        cold = classify(
            "message amma",
            resolve=lambda t: self._decision(t, runnable=False, blocked="WhatsApp isn't connected.", live=False),
        )
        self.assertIn("Shall I open it so you can sign in?", cold.question)

    def test_a_runnable_step_has_nothing_to_ask(self):
        """Asking about work it can already do is the other failure mode."""
        from backend.goal_pursuit import classify

        fine = classify("post the notes on twitter", resolve=lambda t: self._decision(t))
        self.assertEqual(fine.question, "")

    def test_the_questions_are_capped_and_come_after_the_work(self):
        """Three at once is an interrogation, and the first answer usually
        changes the rest. They are also asked after trying, so every question
        left is one the work did not settle."""
        from backend.goal_pursuit import plan_pursuit, pursue

        goal = self._goal(
            goal_context="; ".join(f"frobnicate widget {i}" for i in range(6)),
            tasks=[],
        )
        plan = plan_pursuit(goal, resolve=self._nothing)
        self.assertEqual(len(plan.questions), 3)
        report = pursue(plan, run=lambda d: {"ok": True}, learn=lambda p: None)
        self.assertEqual(report["questions"], plan.questions)
        narrative = report["reasoning"]
        self.assertTrue(narrative[-1].startswith("Before I go further:"))


class OwnerVerificationTests(unittest.TestCase):
    """Proving who the boss is, and being honest about what each proof is worth.

    The line these tests hold is that an unavailable check is not a passing one.
    A machine with no face model must not conclude "looks like him" from silence,
    and a machine with no passphrase must not conclude "not him" either -- the
    first is a security hole and the second locks the owner out of their own desk.
    """

    _PHRASE = "correct horse battery"

    def _record(self):
        from backend.owner_verify import hash_passphrase

        return hash_passphrase(self._PHRASE)

    def _rows(self):
        return [
            {
                "kind": "memory",
                "topic": "sister's birthday",
                "text": "Divya has her birthday on 12 March and we were in Hyderabad",
                "when": "March",
            },
            {
                "kind": "memory",
                "topic": "laptop",
                "text": "the thinkpad charger stays in the blue rucksack",
                "when": "June",
            },
        ]

    def test_a_check_that_could_not_be_made_never_counts_as_a_pass(self):
        """No face model is installed, so the camera earns nothing -- and reports
        `unavailable`, which is a different thing from a mismatch."""
        from backend.owner_verify import OUTCOME_UNAVAILABLE, camera_factor

        factor = camera_factor()
        self.assertEqual(factor.outcome, OUTCOME_UNAVAILABLE)
        self.assertEqual(factor.earned, 0.0)
        self.assertFalse(factor.contradicts)
        self.assertIn("proves nothing", factor.detail)

    def test_a_camera_that_is_off_is_not_a_failed_face_check(self):
        from backend.owner_verify import OUTCOME_ABSENT, camera_factor

        installed = {"cv2": True, "face_recognition": True, "insightface": True}
        self.assertEqual(camera_factor(frame=None, stack=installed).outcome, OUTCOME_ABSENT)

    def test_sitting_at_the_desk_is_enough_to_work_and_not_enough_to_grant(self):
        """The one asymmetry the whole design rests on."""
        from backend.owner_verify import TIER_ACT, TIER_GRANT, assess, local_session_factor

        session = [local_session_factor()]
        self.assertTrue(assess(TIER_ACT, session).allowed)
        grant = assess(TIER_GRANT, session)
        self.assertFalse(grant.allowed)
        self.assertTrue(grant.challenge)

    def test_a_spoken_passphrase_survives_case_and_punctuation(self):
        """It arrives through a transcriber, so it cannot turn on an apostrophe."""
        from backend.owner_verify import OUTCOME_PASS, hash_passphrase, passphrase_factor

        stored = hash_passphrase("Open Sesame, please.")
        self.assertEqual(passphrase_factor("open sesame please", stored).outcome, OUTCOME_PASS)
        self.assertEqual(passphrase_factor("  OPEN  SESAME PLEASE ", stored).outcome, OUTCOME_PASS)
        self.assertNotEqual(passphrase_factor("open sesame", stored).outcome, OUTCOME_PASS)

    def test_no_passphrase_set_is_not_the_same_as_the_wrong_one(self):
        from backend.owner_verify import OUTCOME_NOT_ENROLLED, passphrase_factor

        factor = passphrase_factor("anything", None)
        self.assertEqual(factor.outcome, OUTCOME_NOT_ENROLLED)
        self.assertFalse(factor.contradicts)

    def test_a_wrong_passphrase_does_not_lock_the_owner_out_of_ordinary_work(self):
        from backend.owner_verify import (
            TIER_ACT,
            TIER_GRANT,
            assess,
            local_session_factor,
            passphrase_factor,
        )

        factors = [local_session_factor(), passphrase_factor("not it at all", self._record())]
        self.assertTrue(assess(TIER_ACT, factors).allowed)
        denied = assess(TIER_GRANT, factors)
        self.assertFalse(denied.allowed)
        self.assertTrue(denied.contradicted)
        self.assertIn("answered wrong", " ".join(denied.reasoning))

    def test_the_history_question_does_not_ship_its_own_answer(self):
        """The bug this catches: keys taken from the topic, which the question
        quotes. Anyone who heard the question could then answer it."""
        from backend.owner_verify import history_challenge

        challenge = history_challenge(self._rows(), seed=1)
        self.assertIsNotNone(challenge)
        asked = set(challenge.question.casefold().replace("'", " ").split())
        for key in challenge.answer_keys:
            self.assertNotIn(key, asked, f"the question gives away {key!r}")
        self.assertNotIn("keys", set(challenge.as_dict()) - {"keys"})
        self.assertIsInstance(challenge.as_dict()["keys"], int)

    def test_a_paraphrase_passes_where_parroting_the_question_fails(self):
        from backend.owner_verify import OUTCOME_FAIL, OUTCOME_PASS, history_challenge, history_factor

        challenge = history_challenge(self._rows(), seed=1)
        good = history_factor("divya, the twelfth of march, we were in hyderabad", challenge)
        self.assertEqual(good.outcome, OUTCOME_PASS)
        self.assertEqual(history_factor("it was about my sister's birthday", challenge).outcome, OUTCOME_FAIL)

    def test_nothing_written_down_yet_means_no_question_rather_than_a_hopeless_one(self):
        from backend.owner_verify import OUTCOME_NOT_ENROLLED, history_challenge, history_factor

        self.assertIsNone(history_challenge([]))
        self.assertIsNone(history_challenge([{"kind": "memory", "topic": "x", "text": "ok"}]))
        self.assertEqual(history_factor("anything", None).outcome, OUTCOME_NOT_ENROLLED)

    def test_passive_signals_cannot_add_up_to_a_grant(self):
        """At the desk plus a good-sounding voice is what anyone in the room has.
        A grant needs something that had to be *answered*, whatever the total."""
        from backend.owner_verify import (
            TIER_GRANT,
            OUTCOME_PASS,
            assess,
            local_session_factor,
            voice_factor,
        )

        print_ = {"algorithm": "mfcc20-meanstd-v1", "vector": [1.0, 2.0, 3.0, 4.0]}
        voice = voice_factor(print_, print_)
        self.assertEqual(voice.outcome, OUTCOME_PASS)
        verdict = assess(TIER_GRANT, [local_session_factor(), voice])
        self.assertFalse(verdict.allowed)
        self.assertLess(verdict.score, 1.0)

    def test_only_the_passphrase_clears_a_step_that_cannot_be_undone(self):
        from backend.owner_verify import (
            TIER_IRREVERSIBLE,
            assess,
            history_challenge,
            history_factor,
            local_session_factor,
            passphrase_factor,
        )

        challenge = history_challenge(self._rows(), seed=1)
        knows = history_factor("divya, twelfth of march, hyderabad", challenge)
        without = assess(TIER_IRREVERSIBLE, [local_session_factor(), knows])
        self.assertFalse(without.allowed)
        self.assertEqual(without.challenge_factor, "passphrase")
        with_it = assess(
            TIER_IRREVERSIBLE,
            [local_session_factor(), knows, passphrase_factor(self._PHRASE, self._record())],
        )
        self.assertTrue(with_it.allowed)

    def test_a_voiceprint_is_only_compared_against_its_own_algorithm(self):
        """A vector from a different feature set is not a mismatch, it is
        incomparable -- and saying "not him" there would be a lie."""
        from backend.owner_verify import OUTCOME_UNAVAILABLE, voice_factor, voice_similarity

        mine = {"algorithm": "mfcc20-meanstd-v1", "vector": [1.0, 0.0]}
        theirs = {"algorithm": "something-else-v9", "vector": [1.0, 0.0]}
        self.assertIsNone(voice_similarity(mine, theirs))
        self.assertEqual(voice_factor(theirs, mine).outcome, OUTCOME_UNAVAILABLE)

    def test_no_usable_recording_is_unavailable_not_a_mismatch(self):
        from backend.owner_verify import OUTCOME_NOT_ENROLLED, OUTCOME_UNAVAILABLE, voice_factor

        enrolled = {"algorithm": "mfcc20-meanstd-v1", "vector": [1.0, 0.0]}
        self.assertEqual(voice_factor(None, enrolled).outcome, OUTCOME_UNAVAILABLE)
        self.assertEqual(voice_factor(enrolled, None).outcome, OUTCOME_NOT_ENROLLED)

    def test_a_different_voice_is_evidence_against(self):
        from backend.owner_verify import OUTCOME_FAIL, voice_factor

        enrolled = {"algorithm": "mfcc20-meanstd-v1", "vector": [1.0, 0.0, 0.0]}
        heard = {"algorithm": "mfcc20-meanstd-v1", "vector": [0.0, 1.0, 0.0]}
        factor = voice_factor(heard, enrolled)
        self.assertEqual(factor.outcome, OUTCOME_FAIL)
        self.assertTrue(factor.contradicts)

    def test_a_denied_verdict_says_what_would_fix_it(self):
        from backend.owner_verify import TIER_GRANT, assess, local_session_factor, spoken_verdict

        verdict = assess(TIER_GRANT, [local_session_factor()])
        self.assertTrue(verdict.challenge.endswith(".") or verdict.challenge.endswith("?"))
        self.assertIn(verdict.challenge_factor, ("history_question", "passphrase"))
        self.assertIn(verdict.challenge, spoken_verdict(verdict))

    def test_an_allowed_verdict_asks_for_nothing_and_names_what_satisfied_it(self):
        from backend.owner_verify import TIER_GRANT, assess, local_session_factor, passphrase_factor, spoken_verdict

        verdict = assess(TIER_GRANT, [local_session_factor(), passphrase_factor(self._PHRASE, self._record())])
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.challenge, "")
        self.assertEqual(spoken_verdict(verdict), "")
        self.assertIn("passphrase", " ".join(verdict.reasoning))

    def test_the_unavailable_factors_are_reported_rather_than_dropped(self):
        """A verdict that silently omits the camera reads as though it agreed."""
        from backend.owner_verify import (
            TIER_GRANT,
            assess,
            camera_factor,
            local_session_factor,
            passphrase_factor,
        )

        verdict = assess(
            TIER_GRANT,
            [local_session_factor(), passphrase_factor(self._PHRASE, self._record()), camera_factor()],
        )
        self.assertTrue(verdict.allowed)
        self.assertIn("Not counted either way", " ".join(verdict.reasoning))
        names = {factor["name"] for factor in verdict.as_dict()["factors"]}
        self.assertIn("camera_match", names)

    def test_enforcement_waits_until_there_is_something_to_ask_for(self):
        """With no passphrase enrolled there is no answer that would satisfy a
        grant, so demanding one would wall the owner out of their own machine."""
        from backend.main import _require_owner_for_apps
        from backend.owner_verify import TIER_GRANT

        # db=None means no secret store, which is exactly the fresh-install case.
        self.assertIsNotNone(_require_owner_for_apps(None, "Connecting an app", tier=TIER_GRANT, db=None))

    def test_with_nothing_enrolled_there_is_no_question_to_ask(self):
        """The pure half of the rule above: `assess` still refuses -- it only
        reports evidence -- but it says there is nothing that would settle it,
        which is what lets the gate stand aside rather than guess."""
        from backend.owner_verify import (
            OUTCOME_NOT_ENROLLED,
            TIER_GRANT,
            TIER_IRREVERSIBLE,
            Factor,
            assess,
            local_session_factor,
            passphrase_factor,
        )

        bare = [
            local_session_factor(),
            passphrase_factor("anything", None),
            Factor("history_question", OUTCOME_NOT_ENROLLED),
        ]
        for tier in (TIER_GRANT, TIER_IRREVERSIBLE):
            verdict = assess(tier, bare)
            self.assertFalse(verdict.allowed, tier)
            self.assertEqual(verdict.challenge_factor, "", tier)
            self.assertEqual(verdict.challenge, "", tier)
            self.assertIn("Nothing is enrolled", " ".join(verdict.reasoning), tier)

    def test_a_question_not_yet_asked_is_not_a_question_that_cannot_be_asked(self):
        """Conflating the two either locks the owner out or lets a stranger skip
        the one challenge this machine can actually pose."""
        from backend.owner_verify import (
            OUTCOME_ABSENT,
            OUTCOME_NOT_ENROLLED,
            TIER_GRANT,
            Factor,
            assess,
            local_session_factor,
            passphrase_factor,
        )

        stored = self._record()
        askable = assess(
            TIER_GRANT,
            [
                local_session_factor(),
                passphrase_factor(None, stored),
                Factor("history_question", OUTCOME_ABSENT),
            ],
        )
        self.assertEqual(askable.challenge_factor, "history_question")
        hopeless = assess(
            TIER_GRANT,
            [
                local_session_factor(),
                passphrase_factor(None, stored),
                Factor("history_question", OUTCOME_NOT_ENROLLED),
            ],
        )
        # The passphrase exists, so there is still something to ask for.
        self.assertEqual(hopeless.challenge_factor, "passphrase")

    def test_a_declared_non_owner_is_still_refused_before_any_of_this(self):
        from fastapi import HTTPException

        from backend.main import _require_owner_for_apps

        with self.assertRaises(HTTPException) as caught:
            _require_owner_for_apps({"relationship_to_owner": "friend"}, "Connecting an app")
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_credential_routes_ask_at_the_grant_tier(self):
        """Connecting an account and storing a credential are the operations that
        hand Akansha standing authority; they do not run at the everyday tier."""
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        for action in (
            "Connecting an app",
            "Saving app credentials",
            "Disconnecting an app",
            "Adding a connection",
            "Signing in to a website",
        ):
            index = source.find(f'"{action}"')
            self.assertGreater(index, 0, action)
            self.assertIn("tier=TIER_GRANT", source[index - 200 : index + 200], action)

    def test_releasing_the_commit_hold_asks_for_the_passphrase(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        for action in (
            "Going through with a step that cannot be undone",
            "Letting a goal go through a step that cannot be undone",
        ):
            index = source.find(f'"{action}"')
            self.assertGreater(index, 0, action)
            self.assertIn("tier=TIER_IRREVERSIBLE", source[index : index + 200], action)

    def test_the_secret_store_is_kept_out_of_every_serialiser(self):
        """`speaker_profiles` rides out to the browser wholesale, which is why the
        passphrase hash and the voiceprint live in their own table."""
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        start = source.find("def serialize_speaker_profile")
        end = source.find("\ndef ", start + 10)
        self.assertGreater(start, 0)
        self.assertNotIn("OwnerSecret", source[start:end])
        status = source[source.find('@app.get("/api/owner/status")') :]
        status = status[: status.find("@app.post")]
        for leak in ("hash", "vector", "salt", "answer_keys"):
            self.assertNotIn(f'"{leak}"', status, leak)


class LocalVoiceStackTests(unittest.TestCase):
    """The on-machine voice stack: hearing, turn-taking and speech.

    Every test here runs with no model loaded and no network. The models are
    hundreds of megabytes and their latency was measured by hand and written into
    `voice_local`'s own comments; what needs guarding in CI is the logic *around*
    them -- that a missing model degrades instead of raising, that a Telugu
    sentence is not handed to an English phonemiser, and that "I could not
    listen" is never reported as "there was silence".
    """

    @staticmethod
    def _wav(samples, rate=16000):
        import io
        import wave

        import numpy as np

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        return buffer.getvalue()

    def test_audio_that_is_not_audio_is_the_callers_mistake_not_a_crash(self):
        from backend.voice_local import AudioUnreadable, decode_audio

        for payload in (b"", b"not audio at all", b"RIFF\x00\x00\x00\x00nope"):
            with self.subTest(payload=payload[:12]):
                with self.assertRaises(AudioUnreadable):
                    decode_audio(payload)

    def test_a_wav_is_resampled_and_flattened_to_what_both_models_want(self):
        import numpy as np

        from backend.voice_local import SAMPLE_RATE, _decode_wav

        # 48 kHz, one second. Whatever the browser sends, everything downstream
        # sees 16 kHz mono float32 -- a 48 kHz stream read as 16 kHz makes Silero
        # think the speech is three times faster than it is.
        original = (0.4 * np.sin(2 * np.pi * 220 * np.arange(48000) / 48000)).astype(np.float32)
        decoded = _decode_wav(self._wav(original, rate=48000))
        self.assertEqual(decoded.dtype, np.float32)
        self.assertAlmostEqual(len(decoded) / SAMPLE_RATE, 1.0, delta=0.02)
        self.assertLessEqual(float(np.abs(decoded).max()), 1.0)

    def test_a_telugu_reply_is_never_handed_to_an_english_phonemiser(self):
        from backend.voice_local import speakable_by_piper, synthesize

        # Piper's English voice spells non-Latin script out letter by letter, so
        # this has to fall through to edge-tts rather than being "supported".
        self.assertFalse(speakable_by_piper("నేను వింటున్నాను"))
        self.assertIsNone(synthesize("నేను వింటున్నాను"))
        self.assertIsNone(synthesize("   "))

    def test_a_missing_model_reports_its_own_fix_rather_than_a_stack_trace(self):
        from backend import voice_local

        for capability in voice_local.capabilities():
            with self.subTest(capability=capability.name):
                self.assertTrue(capability.detail.strip(), "every capability explains itself")
                if not capability.installed:
                    # The detail is the only thing a user sees when a factor is
                    # missing, so it has to name the command that fixes it.
                    self.assertTrue(
                        "pip install" in capability.detail
                        or "download_voices" in capability.detail
                        or "not installed" in capability.detail.lower(),
                        capability.detail,
                    )

    def test_not_being_able_to_listen_is_not_reported_as_silence(self):
        import backend.voice_local as voice_local

        # The whole reason `speech_activity` returns None instead of an empty
        # reading: a caller that cannot tell "no model" from "no speech" will
        # treat a missing install as a finished turn and cut the user off.
        original = voice_local.vad_available
        try:
            voice_local.vad_available = lambda: False
            self.assertIsNone(voice_local.speech_activity(self._wav([0.0] * 16000)))
        finally:
            voice_local.vad_available = original

        empty = voice_local.speech_activity(self._wav([]))
        if empty is not None:
            self.assertFalse(empty.any_speech)
            self.assertFalse(empty.speaking)

    def test_a_clip_that_ended_in_silence_is_a_finished_turn(self):
        from backend.voice_local import SpeechWindow, VadReading

        # `speaking` is the field `/api/voice/turn-boundary` reads. It must mean
        # "the audio ends inside speech", not "speech happened somewhere in here",
        # or every finished sentence looks like a live one.
        finished = VadReading(
            speaking=False,
            windows=(SpeechWindow(0.2, 1.4),),
            speech_ms=1200.0,
            trailing_silence_ms=900.0,
            leading_silence_ms=200.0,
            duration_ms=2300.0,
        )
        self.assertTrue(finished.any_speech)
        self.assertFalse(finished.speaking)
        self.assertEqual(finished.as_dict()["trailing_silence_ms"], 900.0)

    def test_turn_taking_does_not_wait_two_seconds_before_noticing(self):
        from backend.voice_local import _options

        # Silero's own default is 2000 ms, which is a transcription setting -- it
        # exists so a sentence is not chopped into fragments. Used for
        # turn-taking it would mean nearly two seconds of dead air after every
        # question. Anything above ~500 ms is felt as lag.
        self.assertLessEqual(_options().min_silence_duration_ms, 500)
        self.assertGreaterEqual(_options().min_silence_duration_ms, 150)

    def test_the_stt_endpoint_says_what_to_install_instead_of_failing_opaquely(self):
        from fastapi.testclient import TestClient

        import backend.main as main_module
        import backend.voice_local as voice_local

        client = TestClient(main_module.app)
        original = voice_local.whisper_installed
        try:
            voice_local.whisper_installed = lambda: False
            response = client.post(
                "/api/voice/stt", files={"audio": ("t.wav", self._wav([0.1] * 16000), "audio/wav")}
            )
            # 503, not 500: the machine is missing an optional model, which is not
            # the same as Akansha being broken, and not the user's mistake either.
            self.assertEqual(response.status_code, 503)
            self.assertIn("faster-whisper", response.json()["detail"])
        finally:
            voice_local.whisper_installed = original

        self.assertEqual(
            client.post("/api/voice/stt", files={"audio": ("t.wav", b"", "audio/wav")}).status_code,
            400,
        )

    def test_the_capability_probe_never_loads_a_model(self):
        import time

        from fastapi.testclient import TestClient

        import backend.main as main_module

        # The client polls this to decide whether to record audio at all, so it
        # has to answer on a cold process. Loading Whisper here would cost seconds
        # and defeat the point of asking.
        client = TestClient(main_module.app)
        started = time.perf_counter()
        body = client.get("/api/voice/local/capabilities").json()
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertEqual({c["name"] for c in body["capabilities"]}, {"hearing", "turn_taking", "speech"})
        self.assertEqual(body["sample_rate"], 16000)

    def test_the_transcription_hint_carries_names_whisper_would_otherwise_guess(self):
        from backend.main import _stt_hint

        # Measured, and it is the reason the default model is `base`: without the
        # hint `base` heard "Open what's happened message ammo"; with it, the same
        # model returned "Open WhatsApp and message Amma" in a fifth of `small`'s
        # time. The hint is what makes the fast model accurate enough.
        class _Row(list):
            pass

        class _Query:
            def __init__(self, rows):
                self._rows = rows

            def order_by(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def all(self):
                return self._rows

        class _Db:
            def __init__(self):
                self.calls = 0

            def query(self, *_a):
                self.calls += 1
                return _Query([("Amma",), ("Kukatpally",)] if self.calls == 1 else [("Activa service",)])

        hint = _stt_hint(_Db())
        self.assertIn("Akansha", hint)
        self.assertIn("Amma", hint)

    def test_a_database_that_is_down_costs_the_hint_and_not_the_transcript(self):
        from backend.main import _stt_hint

        class _Broken:
            def query(self, *_a):
                raise RuntimeError("database is gone")

        # A hint is an optimisation. Losing it must never lose the words.
        self.assertEqual(_stt_hint(_Broken()), "Akansha")


class SemanticMemoryTests(unittest.TestCase):
    """The index that answers questions the words do not.

    Every test here points its store at a temporary directory. The vector store
    is a real SQLite file, and a test that wrote into the developer's own
    `backend/vector_store/` would silently change what Akansha remembers.
    """

    def setUp(self):
        self._store = tempfile.mkdtemp(prefix="akansha-vectors-")
        self._previous = os.environ.get("AKANSHA_VECTOR_DIR")
        os.environ["AKANSHA_VECTOR_DIR"] = self._store
        from backend import semantic_memory

        # The module read the directory at import time, so point it here too.
        self._previous_dir = semantic_memory.STORE_DIR
        semantic_memory.STORE_DIR = self._store
        semantic_memory._matrix_cache.clear()

    def tearDown(self):
        from backend import semantic_memory

        semantic_memory.STORE_DIR = self._previous_dir
        semantic_memory._matrix_cache.clear()
        if self._previous is None:
            os.environ.pop("AKANSHA_VECTOR_DIR", None)
        else:
            os.environ["AKANSHA_VECTOR_DIR"] = self._previous
        shutil.rmtree(self._store, ignore_errors=True)

    @staticmethod
    def _skip_unless_model_present(test):
        from backend import semantic_memory

        capability = semantic_memory.capability()
        if not capability.usable:
            test.skipTest(f"embedding model not available: {capability.detail}")

    def test_padding_is_masked_by_attention_and_not_by_token_count(self):
        """The bug that made everything similar to everything.

        This tokenizer's config pads every input to 128 tokens, so `len(ids)` is
        128 for a five-word question. Masking on that pools ~120 `[PAD]`
        embeddings into the mean and drags every vector toward the same point:
        measured, "my scooter needs servicing" against "capital of France" scored
        +0.744 instead of -0.007. The guard is that the unrelated pair must stay
        near zero.
        """
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        vectors = semantic_memory.embed(
            [
                "my scooter needs servicing",
                "the Activa is due for a service this month",
                "what is the capital of France",
            ]
        )
        self.assertIsNotNone(vectors)
        self.assertEqual(vectors.shape, (3, semantic_memory.EMBED_DIMENSIONS))
        related = float(vectors[0] @ vectors[1])
        unrelated = float(vectors[0] @ vectors[2])
        self.assertGreater(related, 0.15, "paraphrase should score above the noise floor")
        self.assertLess(unrelated, 0.10, "unrelated text must not score like a match")
        self.assertGreater(related, unrelated + 0.15)

    def test_vectors_are_unit_length_so_a_dot_product_is_the_cosine(self):
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        vectors = semantic_memory.embed(["one sentence", "another entirely different sentence"])
        self.assertIsNotNone(vectors)
        for row in vectors:
            self.assertAlmostEqual(float((row * row).sum()), 1.0, places=4)

    def test_a_paraphrase_is_found_where_like_finds_nothing(self):
        """The whole reason this module exists."""
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        semantic_memory.index(
            [
                {"id": "1", "text": "the Activa is due for a service this month"},
                {"id": "2", "text": "the user is allergic to peanuts"},
            ]
        )
        # No character overlap with the memory at all -- this is exactly 0 hits
        # under `topic.ilike('%my scooter needs servicing%')`.
        hits = semantic_memory.search("my scooter needs servicing", limit=2)
        self.assertTrue(hits, "paraphrase recall returned nothing")
        self.assertEqual(hits[0].id, "1")
        self.assertGreater(hits[0].similarity, semantic_memory.MIN_SIMILARITY)

    def test_an_unrelated_question_returns_nothing_rather_than_the_least_bad_row(self):
        """A vector index always has a nearest neighbour. It must not report it.

        Without the floor, "what is the capital of France" returns whichever
        memory is least unrelated, and Akansha answers a question about France
        with a fact about peanuts.
        """
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        semantic_memory.index([{"id": "1", "text": "the user is allergic to peanuts"}])
        self.assertEqual(semantic_memory.search("what is the capital of France"), [])

    def test_reindexing_unchanged_text_embeds_nothing(self):
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        records = [{"id": str(n), "text": f"memory number {n} about something"} for n in range(5)]
        first = semantic_memory.index(records)
        self.assertEqual(first["indexed"], 5)
        second = semantic_memory.index(records)
        self.assertEqual(second["indexed"], 0)
        self.assertEqual(second["skipped"], 5)

    def test_changed_text_is_re_embedded_and_replaces_the_old_vector(self):
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        semantic_memory.index([{"id": "1", "text": "the meeting is on Monday"}])
        semantic_memory.index([{"id": "1", "text": "the user is allergic to peanuts"}])
        self.assertEqual(semantic_memory.stats()["count"], 1, "upsert must not duplicate an id")
        hits = semantic_memory.search("what can I not eat", limit=1)
        self.assertTrue(hits)
        self.assertIn("peanuts", hits[0].text)

    def test_a_forgotten_memory_stops_answering_immediately(self):
        """Deleting upstream and leaving the vector is worse than no recall.

        The cached matrix is the trap: a delete that does not invalidate it keeps
        serving the memory from memory for the life of the process.
        """
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        semantic_memory.index([{"id": "1", "text": "the user is allergic to peanuts"}])
        self.assertTrue(semantic_memory.search("what can I not eat", limit=1))
        self.assertEqual(semantic_memory.forget(["1"]), 1)
        self.assertEqual(semantic_memory.search("what can I not eat", limit=1), [])

    def test_metadata_survives_the_round_trip(self):
        self._skip_unless_model_present(self)
        from backend import semantic_memory

        semantic_memory.index(
            [{"id": "7", "text": "the user is allergic to peanuts", "metadata": {"topic": "health", "importance": 5}}]
        )
        hits = semantic_memory.search("what can I not eat", limit=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].metadata.get("topic"), "health")
        self.assertEqual(hits[0].metadata.get("importance"), 5)

    def test_search_is_empty_and_silent_when_the_model_is_missing(self):
        """A machine without the model must lose semantic recall, not the request.

        Every caller runs this alongside a `LIKE` query and merges. If this
        raises, the lexical answer the user would have got is lost too.
        """
        from backend import semantic_memory

        with patch.object(semantic_memory, "_encoder", return_value=None):
            self.assertEqual(semantic_memory.search("anything at all"), [])
            self.assertIsNone(semantic_memory.embed(["anything at all"]))
            result = semantic_memory.index([{"id": "1", "text": "something"}])
            self.assertEqual(result["indexed"], 0)

    def test_an_empty_or_whitespace_query_never_reaches_the_model(self):
        from backend import semantic_memory

        with patch.object(semantic_memory, "_encoder") as encoder:
            self.assertEqual(semantic_memory.search("   "), [])
            self.assertEqual(semantic_memory.search(""), [])
            encoder.assert_not_called()

    def test_a_vector_of_the_wrong_width_is_skipped_rather_than_crashing_search(self):
        """A model change leaves 384-dim rows next to whatever comes next.

        `np.vstack` on mixed widths raises, which would take out recall entirely
        for every memory rather than the one stale row.
        """
        from backend import semantic_memory

        with semantic_memory._db() as connection:
            connection.execute(
                "INSERT INTO vectors (collection, doc_id, text, fingerprint, metadata, vector)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("memory", "stale", "old model row", "deadbeef", "{}", b"\x00" * 16),
            )
        semantic_memory._matrix_cache.clear()
        matrix, ids = semantic_memory._matrix("memory")
        self.assertEqual(matrix.shape, (0, semantic_memory.EMBED_DIMENSIONS))
        self.assertEqual(ids, [])

    def test_capability_probe_does_not_download_ninety_megabytes(self):
        """Answering "is this ready?" must not be the thing that fetches the model."""
        from backend import semantic_memory

        # `_session` is patched to None because an earlier test in this class may
        # already have built one, and `capability()` short-circuits on a live
        # session. Without this the test passes or fails depending on which order
        # unittest happened to run the class in, which is not a test.
        with patch.object(semantic_memory, "_session", None), patch.object(
            semantic_memory, "_model_paths"
        ) as paths:
            paths.return_value = None
            report = semantic_memory.capability()
            self.assertFalse(report.ready)
            paths.assert_called_once_with(allow_download=False)


class MemoryIndexWiringTests(unittest.TestCase):
    """That the index is actually reachable from the paths that recall memories.

    `semantic_memory` working in isolation was never the risk -- the prior version
    of this stack had a whole backend nobody called. These tests are about the
    seams.
    """

    def test_a_memory_row_is_indexed_as_topic_plus_insight(self):
        """Either half alone loses half the query.

        The topic is usually where the subject is named ("Activa"); the insight
        carries the predicate ("due for a service"). "my scooter needs servicing"
        needs both to be in the same document.
        """
        from backend import memory_index

        row = SimpleNamespace(id=3, topic="Activa", insight="due for a service this month", importance=4)
        self.assertEqual(memory_index._document(row), "Activa. due for a service this month")
        self.assertEqual(memory_index._document(SimpleNamespace(topic="", insight="only this")), "only this")
        self.assertEqual(memory_index._document(SimpleNamespace(topic="only this", insight=None)), "only this")
        self.assertEqual(memory_index._document(SimpleNamespace(topic=None, insight=None)), "")

    def test_a_row_with_no_text_is_not_indexed(self):
        from backend import memory_index

        with patch.object(memory_index.semantic_memory, "index") as index:
            self.assertEqual(memory_index.index_rows([SimpleNamespace(id=1, topic="", insight="")]), 0)
            index.assert_not_called()

    def test_a_row_with_no_primary_key_is_not_indexed_under_the_id_none(self):
        """An unflushed row has `id is None`, and `str(None)` is a valid doc_id.

        Without the guard the vector is stored under "None" and overwritten by the
        next unflushed row, so one memory answers for another.
        """
        from backend import memory_index

        with patch.object(memory_index.semantic_memory, "index") as index:
            memory_index.index_rows([SimpleNamespace(id=None, topic="a", insight="b")])
            index.assert_not_called()

    def test_recall_returns_an_empty_list_rather_than_raising(self):
        from backend import memory_index

        with patch.object(memory_index.semantic_memory, "search", side_effect=RuntimeError("no model")):
            self.assertEqual(memory_index.recall("anything"), [])

    def test_omni_search_merges_semantic_hits_without_duplicating_lexical_ones(self):
        """The union, not one or the other.

        A memory that both indexes find must appear once, and it must keep the
        lexical score -- an exact word match should not be demoted because the
        vector index also happened to like it.
        """
        from backend import main as main_module

        class _Query:
            def filter(self, *_a):
                return self

            def order_by(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def all(self):
                return [SimpleNamespace(id=1, topic="Activa", insight="due for a service", importance=4)]

        class _Db:
            def query(self, *_a):
                return _Query()

        semantic = [
            {"id": "1", "text": "Activa. due for a service", "similarity": 0.9, "topic": "Activa", "importance": 4},
            {"id": "2", "text": "the user is allergic to peanuts", "similarity": 0.5, "topic": "Diet", "importance": 5},
        ]
        with patch.object(main_module.memory_index, "recall", return_value=semantic):
            results = main_module._omni_memory(_Db(), "Activa")

        self.assertEqual([item["id"] for item in results], ["1", "2"])
        self.assertEqual(results[0]["match"], "lexical")
        self.assertEqual(results[0]["score"], 0.65)
        self.assertEqual(results[1]["match"], "semantic")
        self.assertLess(results[1]["score"], results[0]["score"], "a modest cosine must not outrank an exact match")

    def test_a_dead_semantic_index_leaves_the_lexical_result_intact(self):
        from backend import main as main_module

        class _Query:
            def filter(self, *_a):
                return self

            def order_by(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def all(self):
                return [SimpleNamespace(id=1, topic="Activa", insight="due for a service", importance=4)]

        class _Db:
            def query(self, *_a):
                return _Query()

        with patch.object(main_module.memory_index, "recall", return_value=[]):
            results = main_module._omni_memory(_Db(), "Activa")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "1")

    def test_hermes_recall_takes_the_better_of_word_overlap_and_meaning(self):
        """`max`, not a blend.

        The two signals fail in opposite directions: word overlap is confident on
        shared words and blind otherwise, the embedding is the reverse. A memory
        with lexical 0.0 and vector 0.4 is the case this exists for, and it must
        not be averaged back down to 0.2.
        """
        from backend.hermes.memory import retrieval_engine

        engine = retrieval_engine.MemoryRetrievalEngine.__new__(retrieval_engine.MemoryRetrievalEngine)
        rows = [{"memory_id": "m1", "content": "the Activa is due for a service", "category": "vehicle"}]
        with patch.object(retrieval_engine.semantic_memory, "available", return_value=True), patch.object(
            retrieval_engine.semantic_memory, "index", return_value={"indexed": 1}
        ), patch.object(
            retrieval_engine.semantic_memory,
            "search",
            return_value=[retrieval_engine.semantic_memory.Hit(id="m1", text="x", similarity=0.4)],
        ):
            scores = engine._vector_scores("my scooter needs servicing", rows)
        self.assertAlmostEqual(scores["m1"], 0.4)

    def test_hermes_recall_asks_for_no_similarity_floor(self):
        """It is ranking, not filtering.

        A 0.15 still beats the 0.0 that word overlap gives a correct memory phrased
        differently, so the floor that `search` applies for search must not apply
        here.
        """
        from backend.hermes.memory import retrieval_engine

        engine = retrieval_engine.MemoryRetrievalEngine.__new__(retrieval_engine.MemoryRetrievalEngine)
        rows = [{"memory_id": "m1", "content": "something", "category": ""}]
        with patch.object(retrieval_engine.semantic_memory, "available", return_value=True), patch.object(
            retrieval_engine.semantic_memory, "index", return_value={"indexed": 1}
        ), patch.object(retrieval_engine.semantic_memory, "search", return_value=[]) as search:
            engine._vector_scores("anything", rows)
        self.assertEqual(search.call_args.kwargs["min_similarity"], -1.0)
        self.assertEqual(search.call_args.kwargs["collection"], "hermes")

    def test_hermes_uses_its_own_collection_so_ids_cannot_collide(self):
        """Two stores, two id spaces.

        Hermes keys on a `memory_id` string; the SQLAlchemy table keys on an
        integer. Sharing one collection would let "3" mean two different memories.
        """
        from backend.hermes.memory import retrieval_engine
        from backend import semantic_memory

        self.assertNotEqual(retrieval_engine.COLLECTION, semantic_memory.COLLECTION_MEMORY)

    def test_a_broken_vector_index_does_not_take_out_hermes_recall(self):
        from backend.hermes.memory import retrieval_engine

        engine = retrieval_engine.MemoryRetrievalEngine.__new__(retrieval_engine.MemoryRetrievalEngine)
        with patch.object(retrieval_engine.semantic_memory, "available", return_value=True), patch.object(
            retrieval_engine.semantic_memory, "index", side_effect=RuntimeError("store is gone")
        ):
            self.assertEqual(engine._vector_scores("anything", [{"memory_id": "m1", "content": "x"}]), {})

    def test_the_write_path_flushes_before_indexing(self):
        """A new row has no id until it is flushed.

        Indexing before the flush stores the vector under `None`, so the memory
        the user just gave Akansha is not the memory that answers.
        """
        from backend import ai_engine as engine

        order: list[str] = []

        class _Db:
            def flush(self):
                order.append("flush")

        with patch("backend.memory_index.index_rows", side_effect=lambda rows: order.append("index")):
            engine._index_memories(_Db(), [SimpleNamespace(id=1, topic="a", insight="b")])
        self.assertEqual(order, ["flush", "index"])

    def test_an_index_failure_never_fails_the_reply(self):
        from backend import ai_engine as engine

        class _Db:
            def flush(self):
                raise RuntimeError("transaction is bad")

        # No exception: the memory is still written, it is just not yet searchable
        # by meaning, and the next sync fixes that.
        engine._index_memories(_Db(), [SimpleNamespace(id=1, topic="a", insight="b")])

    def test_upsert_returns_the_row_so_the_caller_can_index_it(self):
        """It used to return None, which is why nothing could be indexed on write."""
        from backend import ai_engine as engine

        added: list = []

        class _Query:
            def filter(self, *_a):
                return self

            def order_by(self, *_a):
                return self

            def first(self):
                return None

        class _Db:
            def query(self, *_a):
                return _Query()

            def add(self, row):
                added.append(row)

        row = engine._upsert_memory(_Db(), "Topic", "Insight", importance=4)
        self.assertIs(row, added[0])
        self.assertEqual(row.topic, "Topic")

    def test_startup_warms_the_embedding_session_after_the_voice_models(self):
        """Order matters: they compete for the same cores.

        The microphone is the one the user is waiting on, so voice warms first and
        semantic memory follows in the same thread rather than racing it.
        """
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        voice_at = source.find("report = voice_local.warm()")
        memory_at = source.find("report = memory_index.warm()")
        self.assertGreater(voice_at, 0, "voice warm call is gone")
        self.assertGreater(memory_at, voice_at, "semantic memory must warm after the voice models")


class _FakeRect:
    def __init__(self, left: int = 0, top: int = 0, right: int = 10, bottom: int = 10) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeInfo:
    def __init__(self, name, kind, automation_id="", enabled=True) -> None:
        self.name = name
        self.control_type = kind
        self.automation_id = automation_id
        self.enabled = enabled
        self.rectangle = _FakeRect()


class _FakeElement:
    """A pywinauto wrapper as far as `desktop_ui` is concerned.

    Only the four things the module actually touches: `element_info`,
    `children()`, whichever action methods the app claims to support, and -- for
    one test -- an attribute that raises on *access*, the way `iface_value` does.
    """

    def __init__(
        self,
        name="",
        kind="Pane",
        *,
        children=(),
        automation_id="",
        enabled=True,
        methods=(),
        raising=(),
        focus=False,
        fails=(),
    ) -> None:
        self.element_info = _FakeInfo(name, kind, automation_id, enabled)
        self._children = list(children)
        self._methods = set(methods)
        self._raising = set(raising)
        self._fails = set(fails)
        self._focus = focus
        self.value = ""
        self.calls: list[tuple[str, tuple]] = []
        self.walks = 0

    def children(self):
        self.walks += 1
        return list(self._children)

    def has_keyboard_focus(self):
        return self._focus

    def texts(self):
        return [self.value] if self.value else []

    def __getattr__(self, attribute):
        if attribute == "iface_value" and "value_pattern" in self.__dict__.get("_methods", ()):

            def set_value(text):
                self.calls.append(("value_pattern", (text,)))
                self.value = str(text)

            return SimpleNamespace(SetValue=set_value)
        if attribute in self.__dict__.get("_raising", ()):
            raise RuntimeError(f"no pattern interface for {attribute}")
        if attribute not in self.__dict__.get("_methods", ()):
            raise AttributeError(attribute)

        def call(*args, **kwargs):
            self.calls.append((attribute, args))
            if attribute in self.__dict__.get("_fails", ()):
                raise RuntimeError(f"{attribute} refused")
            if attribute in ("set_edit_text", "set_text", "type_keys") and args:
                self.value = str(args[0])
            return True

        return call


class _FakeHwnd:
    """What `Desktop(backend="win32").windows()` hands back, minus Windows."""

    def __init__(self, handle, title, class_name="Chrome_WidgetWin_1", pid=42) -> None:
        self.handle = handle
        self._title, self._class, self._pid = title, class_name, pid

    def window_text(self):
        return self._title

    def class_name(self):
        return self._class

    def process_id(self):
        return self._pid


class DesktopAccessibilityTests(unittest.TestCase):
    """`desktop_ui`: naming the control instead of guessing where it is.

    Every test here injects the desktop. Nothing in this class enumerates a real
    window, moves the mouse or sends a keystroke -- the point of the injection
    seams in that module is that its routing and its refusals can be checked on a
    machine with no windows open.
    """

    def setUp(self) -> None:
        from backend import desktop_ui

        desktop_ui.reset_for_tests()
        self.ui = desktop_ui

    # a frame is what a Qt app publishes: caption buttons and nothing else
    def _frame(self, name="Telegram"):
        return _FakeElement(
            name,
            "Window",
            children=[
                _FakeElement("Minimize", "Button", methods=("invoke",)),
                _FakeElement("Restore", "Button", methods=("invoke",)),
                _FakeElement("Close", "Button", methods=("invoke",)),
                _FakeElement("System", "MenuItem", methods=("invoke",)),
                _FakeElement("", "TitleBar"),
            ],
        )

    def _chat(self):
        return _FakeElement(
            "(26) WhatsApp",
            "Window",
            children=[
                _FakeElement("Close", "Button", methods=("invoke",)),
                _FakeElement(
                    "",
                    "Pane",
                    children=[
                        _FakeElement("Search or start a new chat", "Edit", methods=("set_edit_text",)),
                        _FakeElement("Type a message", "Edit", methods=("set_edit_text",), focus=True),
                        _FakeElement("Send", "Button", methods=("invoke", "click_input")),
                        _FakeElement("Send later", "MenuItem", methods=("invoke",)),
                        _FakeElement("Voice message", "Button", methods=("invoke",)),
                    ],
                ),
            ],
        )

    def test_enumeration_drops_the_shell_and_the_untitled(self):
        raw = [
            _FakeHwnd(1, "WhatsApp"),
            _FakeHwnd(1, "WhatsApp"),
            _FakeHwnd(2, ""),
            _FakeHwnd(3, "Start", "Shell_TrayWnd"),
            _FakeHwnd(4, "Program Manager", "Progman"),
            _FakeHwnd(5, "Downloads", "CabinetWClass"),
        ]
        found = self.ui.windows(enumerate_with=lambda: raw)
        self.assertEqual([window.handle for window in found], [1, 5])

    def test_enumeration_survives_a_window_that_closes_mid_read(self):
        class Dying(_FakeHwnd):
            def window_text(self):
                raise RuntimeError("window is gone")

        found = self.ui.windows(enumerate_with=lambda: [Dying(9, "x"), _FakeHwnd(5, "Downloads")])
        self.assertEqual([window.handle for window in found], [5])

    def test_title_scoring_prefers_the_exact_answer(self):
        window = lambda title: self.ui.Window(handle=1, title=title, class_name="")  # noqa: E731
        exact = self.ui._score_window(window("WhatsApp"), "WhatsApp")
        prefix = self.ui._score_window(window("WhatsApp Web"), "WhatsApp")
        inside = self.ui._score_window(window("(26) WhatsApp - Chrome"), "WhatsApp")
        shared = self.ui._score_window(window("WhatsApp"), "whatsapp web")
        self.assertEqual(exact, 1.0)
        self.assertGreater(exact, prefix)
        self.assertGreater(prefix, inside)
        self.assertGreater(inside, shared)
        self.assertEqual(self.ui._score_window(window("WhatsApp"), "  "), 0.0)

    def test_the_window_with_a_ui_in_it_beats_the_better_title(self):
        """The measured failure: "WhatsApp" is the empty frame, "(26) WhatsApp" is the app."""
        pool = [
            self.ui.Window(handle=198072, title="WhatsApp", class_name="Chrome_WidgetWin_1"),
            self.ui.Window(handle=197134, title="(26) WhatsApp", class_name="Chrome_WidgetWin_1"),
        ]
        trees = {
            198072: [self.ui.Control(name=f"frame{i}", kind="Button") for i in range(5)],
            197134: [self.ui.Control(name=f"real{i}", kind="Button") for i in range(200)],
        }
        picked = self.ui.find_window("WhatsApp", candidates=pool, inspect=lambda h: trees[h])
        self.assertEqual(picked.handle, 197134)

    def test_a_unique_title_match_never_pays_for_a_tree_walk(self):
        pool = [
            self.ui.Window(handle=7, title="Downloads", class_name="CabinetWClass"),
            self.ui.Window(handle=8, title="Spotify", class_name="Chrome_WidgetWin_1"),
        ]
        walked: list[int] = []

        def inspect(handle):
            walked.append(handle)
            return []

        picked = self.ui.find_window("Downloads", candidates=pool, inspect=inspect)
        self.assertEqual(picked.handle, 7)
        self.assertEqual(walked, [], "one candidate should not be probed")

    def test_the_walk_is_breadth_first_and_carries_depth(self):
        leaf = _FakeElement("leaf", "Button", methods=("invoke",))
        root = _FakeElement("root", "Window", children=[_FakeElement("mid", "Pane", children=[leaf])])
        live = self.ui._walk_live(1, connect=lambda handle: root)
        self.assertEqual([(control.name, control.depth) for control, _ in live], [("mid", 1), ("leaf", 2)])

    def test_the_walk_stops_at_max_controls(self):
        wide = _FakeElement(
            "root",
            "Window",
            children=[_FakeElement(f"b{i}", "Button") for i in range(self.ui.MAX_CONTROLS + 50)],
        )
        self.assertEqual(len(self.ui._walk_live(2, connect=lambda handle: wide)), self.ui.MAX_CONTROLS)

    def test_an_unreadable_branch_does_not_fail_the_whole_walk(self):
        class Hostile(_FakeElement):
            def children(self):
                raise RuntimeError("the app repainted mid-enumeration")

        root = _FakeElement(
            "root", "Window", children=[Hostile("bad", "Pane"), _FakeElement("ok", "Button")]
        )
        names = [control.name for control, _ in self.ui._walk_live(3, connect=lambda handle: root)]
        self.assertEqual(names, ["bad", "ok"])

    def test_a_window_we_cannot_attach_to_is_empty_not_an_exception(self):
        self.assertEqual(self.ui._walk_live(4, connect=lambda handle: None), [])

    def test_the_tree_cache_is_keyed_by_handle_because_titles_move(self):
        trees = {11: self._chat(), 12: self._chat()}
        opened: list[int] = []

        def connect(handle):
            opened.append(handle)
            return trees[handle]

        self.assertEqual(len(self.ui._walk(11, connect=connect)), 7)
        self.ui._walk(11, connect=connect)
        self.assertEqual(opened, [11], "a second read inside the TTL should not walk again")
        self.ui._walk(12, connect=connect)
        self.assertEqual(opened, [11, 12], "a different handle is a different tree")
        self.ui._invalidate_tree(11)
        self.ui._walk(11, connect=connect)
        self.assertEqual(opened, [11, 12, 11], "an action must invalidate what it changed")

    def test_a_frame_only_first_look_is_retried_once(self):
        """Chromium turns accessibility on when asked, so walk one can be too early."""
        answers = [self._frame(), self._chat()]
        opened: list[int] = []

        def connect(handle):
            opened.append(handle)
            return answers[min(len(opened) - 1, 1)]

        with patch.object(self.ui.time, "sleep", lambda seconds: None):
            live = self.ui._walk_live_warm(21, connect=connect)
        self.assertEqual(len(opened), 2)
        self.assertGreater(sum(1 for control, _ in live if control.name), self.ui.FRAME_ONLY_NAMED)

    def test_a_rich_window_reports_what_is_inside_it(self):
        raw = [_FakeHwnd(11, "(26) WhatsApp"), _FakeHwnd(12, "Telegram", "Qt51519QWindowIcon")]
        report = self.ui.controls(
            "WhatsApp", enumerate_with=lambda: raw, connect=lambda handle: self._chat()
        )
        self.assertTrue(report["ok"])
        self.assertTrue(report["sees_inside"])
        self.assertEqual(report["window"]["handle"], 11)
        self.assertIn("Send", report["actionable"])
        self.assertIn("Type a message", report["editable"])
        self.assertNotIn("", [control["name"] for control in report["controls"]])

    def test_a_qt_window_says_it_cannot_see_inside_rather_than_that_it_is_empty(self):
        raw = [_FakeHwnd(12, "Telegram", "Qt51519QWindowIcon")]
        with patch.object(self.ui.time, "sleep", lambda seconds: None):
            report = self.ui.controls(
                "Telegram", enumerate_with=lambda: raw, connect=lambda handle: self._frame()
            )
        self.assertTrue(report["ok"], "the window was found; only its insides are invisible")
        self.assertFalse(report["sees_inside"])
        self.assertIn("keystroke", report["detail"])

    def test_asking_about_a_window_that_is_not_open(self):
        report = self.ui.controls("Photoshop", enumerate_with=lambda: [], connect=lambda handle: None)
        self.assertFalse(report["ok"])
        self.assertFalse(report["sees_inside"])
        self.assertIn("No open window", report["detail"])

    def test_a_window_can_be_addressed_by_handle(self):
        raw = [_FakeHwnd(11, "(26) WhatsApp")]
        report = self.ui.controls(11, enumerate_with=lambda: raw, connect=lambda handle: self._chat())
        self.assertTrue(report["ok"])
        self.assertEqual(report["window"]["handle"], 11)

    def test_the_exact_name_wins_over_the_one_that_merely_starts_with_it(self):
        resolution = self.ui.resolve(
            11, "Send", want=self.ui.ACTIONABLE_KINDS, connect=lambda handle: self._chat()
        )
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.control.name, "Send")
        self.assertFalse(resolution.ambiguous)

    def test_two_different_labels_that_both_answer_is_a_question_not_a_guess(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[
                _FakeElement("Message body", "Edit", methods=("set_edit_text",)),
                _FakeElement("Message subject", "Edit", methods=("set_edit_text",)),
            ],
        )
        resolution = self.ui.resolve(31, "message", connect=lambda handle: root)
        self.assertFalse(resolution.ok)
        self.assertTrue(resolution.ambiguous)
        self.assertIsNone(resolution.element)
        self.assertIn("Which one", resolution.question)
        self.assertEqual(
            {control.name for control in resolution.candidates},
            {"Message body", "Message subject"},
        )

    def test_the_same_label_twice_is_not_a_question_it_is_a_preference(self):
        """File Explorer's search box is an Edit next to a Text with the same name."""
        root = _FakeElement(
            "Downloads",
            "Window",
            children=[
                _FakeElement("Search Downloads", "Text"),
                _FakeElement("Search Downloads", "Edit", methods=("set_edit_text",)),
            ],
        )
        resolution = self.ui.resolve(32, "Search", connect=lambda handle: root)
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.control.kind, "Edit")

    def test_a_name_that_is_not_there_says_what_is(self):
        resolution = self.ui.resolve(11, "Attach a spreadsheet", connect=lambda handle: self._chat())
        self.assertFalse(resolution.ok)
        self.assertFalse(resolution.ambiguous)
        self.assertIn("Nothing in this window is called", resolution.detail)
        self.assertIn("Send", resolution.detail)
        listed = resolution.detail.split("What I can see: ")[1]
        self.assertEqual(len(listed.split(", ")), len(set(listed.split(", "))), "names repeated")

    def test_a_disabled_control_is_refused_with_the_reason(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[_FakeElement("Send", "Button", enabled=False, methods=("invoke",))],
        )
        resolution = self.ui.resolve(33, "Send", connect=lambda handle: root)
        self.assertFalse(resolution.ok)
        self.assertIn("disabled", resolution.detail)

    def test_a_window_with_no_readable_tree_says_so(self):
        resolution = self.ui.resolve(34, "Send", connect=lambda handle: None)
        self.assertFalse(resolution.ok)
        self.assertIn("readable", resolution.detail)

    def test_the_automation_id_is_matched_when_the_label_is_not(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[_FakeElement("", "Button", automation_id="sendButton", methods=("invoke",))],
        )
        resolution = self.ui.resolve(35, "sendButton", connect=lambda handle: root)
        self.assertTrue(resolution.ok)
        self.assertEqual(resolution.control.automation_id, "sendButton")

    def test_a_window_that_really_is_a_frame_keeps_the_first_answer(self):
        root = self._frame()
        with patch.object(self.ui.time, "sleep", lambda seconds: None):
            live = self.ui._walk_live_warm(22, connect=lambda handle: root)
        self.assertEqual(sum(1 for control, _ in live if control.name), 4)

    def test_no_window_answers_is_none_not_the_nearest_thing(self):
        pool = [self.ui.Window(handle=7, title="Downloads", class_name="")]
        self.assertIsNone(self.ui.find_window("Photoshop", candidates=pool, inspect=lambda h: []))
        self.assertIsNone(self.ui.find_window("Downloads", candidates=[], inspect=lambda h: []))

    def _element(self, root, name):
        stack = [root]
        while stack:
            node = stack.pop()
            if node.element_info.name == name:
                return node
            stack.extend(node.children())
        raise AssertionError(f"no {name!r} in this fake tree")

    def _open(self, root, handle=11, title="(26) WhatsApp"):
        return {
            "enumerate_with": (lambda: [_FakeHwnd(handle, title)]),
            "connect": (lambda hwnd: root),
        }

    def test_clicking_uses_the_pattern_and_never_the_pointer(self):
        chat = self._chat()
        result = self.ui.click("WhatsApp", "Send", **self._open(chat))
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "invoke")
        self.assertEqual([name for name, _ in self._element(chat, "Send").calls], ["invoke"])

    def test_a_control_with_no_way_in_fails_rather_than_driving_the_mouse(self):
        root = _FakeElement("Compose", "Window", children=[_FakeElement("Send", "Button")])
        result = self.ui.click("Compose", "Send", **self._open(root, title="Compose"))
        self.assertFalse(result["ok"])
        self.assertIn("pointer was not allowed", result["detail"])

    def test_the_pointer_is_used_only_when_it_is_asked_for(self):
        root = _FakeElement(
            "Compose", "Window", children=[_FakeElement("Send", "Button", methods=("click_input",))]
        )
        result = self.ui.click(
            "Compose", "Send", allow_pointer=True, **self._open(root, title="Compose")
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "click_input")

    def test_a_failing_pattern_is_tried_past_not_reported_as_success(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[
                _FakeElement("Send", "Button", methods=("invoke", "click"), fails=("invoke",))
            ],
        )
        result = self.ui.click("Compose", "Send", **self._open(root, title="Compose"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "click")

    def test_an_ambiguous_click_touches_nothing(self):
        first = _FakeElement("Message body", "Button", methods=("invoke",))
        second = _FakeElement("Message subject", "Button", methods=("invoke",))
        root = _FakeElement("Compose", "Window", children=[first, second])
        result = self.ui.click("Compose", "message", **self._open(root, title="Compose"))
        self.assertFalse(result["ok"])
        self.assertIn("Which one", result["question"])
        self.assertEqual(first.calls, [])
        self.assertEqual(second.calls, [])

    def test_text_arrives_in_one_call_not_as_a_stream_of_keystrokes(self):
        chat = self._chat()
        result = self.ui.set_text("WhatsApp", "Type a message", "on my way", **self._open(chat))
        field = self._element(chat, "Type a message")
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "set_edit_text")
        self.assertTrue(result["verified"], "the field read the text back")
        self.assertEqual(field.value, "on my way")
        self.assertEqual([name for name, _ in field.calls], ["set_edit_text"])

    def test_the_value_pattern_is_used_when_the_wrapper_has_no_setter(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[_FakeElement("Body", "Document", methods=("value_pattern",))],
        )
        result = self.ui.set_text("Compose", "Body", "hello", **self._open(root, title="Compose"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "value_pattern")

    def test_keystrokes_into_a_field_happen_only_when_allowed(self):
        root = _FakeElement(
            "Compose", "Window", children=[_FakeElement("Body", "Edit", methods=("type_keys",))]
        )
        refused = self.ui.set_text("Compose", "Body", "hi", **self._open(root, title="Compose"))
        self.assertFalse(refused["ok"])
        self.assertIn("would not take a value directly", refused["detail"])
        allowed = self.ui.set_text(
            "Compose", "Body", "hi", allow_keys=True, **self._open(root, title="Compose")
        )
        self.assertTrue(allowed["ok"])
        self.assertEqual(allowed["via"], "type_keys")

    def test_an_attribute_that_raises_on_access_is_not_a_method(self):
        element = _FakeElement("Body", "Edit", raising=("set_text",))
        self.assertIsNone(self.ui._method(element, "set_text"))
        self.assertIsNone(self.ui._method(element, "nothing_like_this"))

    def test_typing_with_no_field_named_follows_the_cursor(self):
        chat = self._chat()
        result = self.ui.type_into("WhatsApp", "see you at six", **self._open(chat))
        self.assertTrue(result["ok"])
        self.assertEqual(self._element(chat, "Type a message").value, "see you at six")
        self.assertEqual(self._element(chat, "Search or start a new chat").value, "")

    def test_typing_into_one_of_two_empty_boxes_is_a_question(self):
        root = _FakeElement(
            "Compose",
            "Window",
            children=[
                _FakeElement("Subject", "Edit", methods=("set_edit_text",)),
                _FakeElement("Body", "Edit", methods=("set_edit_text",)),
            ],
        )
        result = self.ui.type_into("Compose", "hello", **self._open(root, title="Compose"))
        self.assertFalse(result["ok"])
        self.assertIn("Which one", result["question"])
        self.assertEqual(self._element(root, "Subject").value, "")
        self.assertEqual(self._element(root, "Body").value, "")

    def test_typing_into_the_only_field_needs_no_question(self):
        root = _FakeElement(
            "Notepad", "Window", children=[_FakeElement("Text editor", "Edit", methods=("set_text",))]
        )
        result = self.ui.type_into("Notepad", "a note", **self._open(root, title="Notepad"))
        self.assertTrue(result["ok"])
        self.assertEqual(self._element(root, "Text editor").value, "a note")

    def test_typing_into_a_qt_window_defers_to_keystrokes(self):
        with patch.object(self.ui.time, "sleep", lambda seconds: None):
            result = self.ui.type_into("Telegram", "hi", **self._open(self._frame(), title="Telegram"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["sees_inside"])
        self.assertIn("keystrokes are the only way in", result["detail"])

    def test_focus_and_close_act_on_the_window_that_was_scored(self):
        root = _FakeElement("Downloads", "Window", methods=("set_focus", "close"))
        opened = self._open(root, handle=5, title="Downloads")
        self.assertTrue(self.ui.focus("Downloads", **opened)["ok"])
        self.assertTrue(self.ui.close("Downloads", **opened)["ok"])
        self.assertEqual([name for name, _ in root.calls], ["set_focus", "close"])

    def test_windows_refusing_to_bring_a_window_forward_is_reported(self):
        root = _FakeElement("Downloads", "Window", methods=("set_focus",), fails=("set_focus",))
        result = self.ui.focus("Downloads", **self._open(root, handle=5, title="Downloads"))
        self.assertFalse(result["ok"])
        self.assertIn("refused", result["detail"])

    def test_a_verb_aimed_at_a_window_that_is_not_open(self):
        for verb in (self.ui.focus, self.ui.close):
            result = verb("Photoshop", enumerate_with=lambda: [], connect=lambda handle: None)
            self.assertFalse(result["ok"])
            self.assertIn("No open window", result["detail"])

    def test_overview_lists_windows_without_reading_a_single_tree(self):
        raw = [_FakeHwnd(1, "WhatsApp"), _FakeHwnd(3, "Start", "Shell_TrayWnd")]
        with patch.object(self.ui, "capability", lambda: self.ui.Capability(True, True, "ready")):
            report = self.ui.overview(enumerate_with=lambda: raw)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["windows"][0]["title"], "WhatsApp")
        self.assertEqual(report["capability"]["backend_enumerate"], "win32")
        self.assertEqual(report["capability"]["backend_inspect"], "uia")

    def test_a_name_buried_in_a_long_label_is_weak_evidence(self):
        """Measured on WhatsApp: a chat row quoted another chat's name and scored 0.70."""
        quoted = self.ui.Control(
            name=(
                "2 unread messages DSA Webinar 3:18 pm ~Abhishek: forwarded -- "
                "Maybe Heydevops is running a course, registration link inside, "
                "limited seats, use code SAVE50 for 50% off, first 10 students only"
            ),
            kind="ListItem",
        )
        itself = self.ui.Control(name="Maybe Heydevops", kind="Group")
        partial = self.ui.Control(name="Maybe Heydevops (muted)", kind="Group")
        self.assertEqual(self.ui._score_name(itself, "Maybe Heydevops"), 1.0)
        self.assertLess(self.ui._score_name(quoted, "Maybe Heydevops"), self.ui.STRONG_NAME_SCORE)
        self.assertLess(self.ui._score_name(quoted, "Maybe Heydevops"), 0.7)
        # still ranked above a shared word, and still below a real partial name
        self.assertGreater(self.ui._score_name(quoted, "Maybe Heydevops"), 0.3)
        self.assertGreater(
            self.ui._score_name(partial, "Maybe Heydevops"),
            self.ui._score_name(quoted, "Maybe Heydevops"),
        )

    def _deep_tree(self, buried: str, *, quoted=False):
        """A window wider than the cap, with `buried` reachable only past it.

        `quoted=True` also plants the name inside one shallow row's label, which is
        the live WhatsApp case: the chat list holds a forwarded message that happens
        to mention the chat the user actually asked for.
        """
        filler = [
            _FakeElement(f"row {i} at 3:1{i % 9} pm ~someone: a long forwarded message", "ListItem")
            for i in range(self.ui.MAX_CONTROLS + 20)
        ]
        if quoted:
            filler[7] = _FakeElement(
                f"row 7 at 3:17 pm ~someone: forwarded -- ask {buried} about the course, "
                "registration link inside, limited seats, first 10 students only",
                "ListItem",
            )
        filler[0]._children = [_FakeElement(buried, "Group", methods=("invoke",))]
        return _FakeElement("(30) WhatsApp", "Window", children=filler)

    def test_a_capped_walk_looks_again_before_saying_it_cannot_find_something(self):
        root = self._deep_tree("Sudheeshna Kodumuru")
        first = self.ui._resolve_from(
            self.ui._walk_live(30, connect=lambda handle: root), "Sudheeshna Kodumuru"
        )
        self.assertFalse(first.ok)
        found = self.ui.resolve(30, "Sudheeshna Kodumuru", connect=lambda handle: root)
        self.assertTrue(found.ok)
        self.assertEqual(found.control.name, "Sudheeshna Kodumuru")
        self.assertEqual(found.score, 1.0)

    def test_a_weak_match_in_a_capped_walk_is_not_the_final_answer(self):
        """The wrong-conversation failure: the deep exact name must beat the quoted one."""
        root = self._deep_tree("Ruthvik Dsa", quoted=True)
        first = self.ui._resolve_from(
            self.ui._walk_live(30, connect=lambda handle: root), "Ruthvik Dsa"
        )
        self.assertTrue(first.ok)  # the first pass would have clicked the wrong row
        self.assertEqual(first.control.kind, "ListItem")
        self.assertLess(first.score, self.ui.STRONG_NAME_SCORE)
        found = self.ui.resolve(30, "Ruthvik Dsa", connect=lambda handle: root)
        self.assertTrue(found.ok)
        self.assertEqual(found.score, 1.0)
        self.assertEqual(found.control.kind, "Group")

    def test_a_strong_match_is_answered_on_one_walk(self):
        root = self._chat()
        walks: list[int] = []

        def connect(handle):
            walks.append(handle)
            return root

        found = self.ui.resolve(1, "Send", connect=connect)
        self.assertTrue(found.ok)
        self.assertEqual(len(walks), 1)

    def test_a_question_is_not_relitigated_by_walking_deeper(self):
        root = self._deep_tree("Send")
        root._children.insert(0, _FakeElement("Send message", "Button", methods=("invoke",)))
        root._children.insert(1, _FakeElement("Send file", "Button", methods=("invoke",)))
        walks: list[int] = []

        def connect(handle):
            walks.append(handle)
            return root

        found = self.ui.resolve(30, "Send", connect=connect)
        self.assertTrue(found.ambiguous)
        self.assertIsNone(found.element)
        self.assertEqual(len(walks), 1)

    def test_one_thing_seen_twice_is_listed_once(self):
        """Measured: Chromium names a list row and the group inside it identically."""
        root = _FakeElement(
            "(30) WhatsApp",
            "Window",
            children=[
                _FakeElement("Minimize", "Button", methods=("invoke",)),
                _FakeElement("Close", "Button", methods=("invoke",)),
                _FakeElement(
                    "Chats",
                    "ListItem",
                    methods=("invoke",),
                    children=[_FakeElement("Chats", "Group", methods=("invoke",))],
                ),
                _FakeElement(
                    "",
                    "Pane",
                    children=[
                        _FakeElement("Search or start a new chat", "Edit", methods=("set_edit_text",)),
                        _FakeElement("Search or start a new chat", "Text"),
                        _FakeElement("Minimize", "Button", methods=("invoke",)),
                    ],
                ),
            ],
        )
        listed = self.ui.controls(
            "WhatsApp",
            enumerate_with=lambda: [_FakeHwnd(7, "(30) WhatsApp")],
            connect=lambda handle: root,
        )
        self.assertTrue(listed["sees_inside"])
        self.assertEqual(listed["actionable"], ["Minimize", "Close", "Chats"])
        self.assertEqual(listed["editable"], ["Search or start a new chat"])
        # the full tree still reports every control, duplicates included
        self.assertEqual(sum(1 for row in listed["controls"] if row["name"] == "Minimize"), 2)

    def test_a_window_smaller_than_the_cap_is_never_walked_twice(self):
        # more than FRAME_ONLY_NAMED, so the cold-tree retry does not fire either
        root = _FakeElement(
            "Downloads",
            "Window",
            children=[
                _FakeElement(name, "Button")
                for name in ("Cut", "Copy", "Rename", "Share", "Delete", "Sort", "View")
            ],
        )
        walks: list[int] = []

        def connect(handle):
            walks.append(handle)
            return root

        found = self.ui.resolve(5, "Paste", connect=connect)
        self.assertFalse(found.ok)
        self.assertIn("Nothing in this window is called", found.detail)
        self.assertEqual(len(walks), 1)


class DesktopControlRoutingTests(unittest.TestCase):
    """`app_control` tries the named-control path, then falls back honestly.

    The fallback matters as much as the path: an app that publishes no
    accessibility tree still has to be typeable, and an *ambiguous* target still
    has to stop -- retrying a question blind is how the wrong chat gets the message.
    """

    def setUp(self) -> None:
        from backend import app_control, desktop_ui

        self.control = app_control
        self.ui = desktop_ui
        self.usable = lambda: desktop_ui.Capability(True, True, "ready")

    def test_without_pywinauto_the_keystroke_path_is_used(self):
        absent = lambda: self.ui.Capability(False, False, "pywinauto is not importable")  # noqa: E731
        with patch.object(self.ui, "capability", absent):
            self.assertIsNone(
                self.control._accessible_desktop("type_text", label="WhatsApp", text="hi")
            )

    def test_a_typed_field_is_reported_with_how_it_was_typed(self):
        typed = {"ok": True, "detail": "Set \"Type a message\".", "via": "set_edit_text"}
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "type_into", lambda label, text: typed
        ):
            result = self.control._accessible_desktop("type_text", label="WhatsApp", text="hi")
        self.assertEqual(result["via"], "set_edit_text")

    def test_an_ambiguous_field_is_not_retried_blind(self):
        question = {"ok": False, "question": "Which one?", "detail": "Ambiguous field"}
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "type_into", lambda label, text: question
        ):
            result = self.control._accessible_desktop("type_text", label="WhatsApp", text="hi")
        self.assertIsNotNone(result, "a question must not fall through to keystrokes")
        self.assertIn("Which one", result["question"])

    def test_a_window_we_cannot_see_into_falls_through(self):
        blind = {"ok": False, "detail": "only its window frame is published"}
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "type_into", lambda label, text: blind
        ):
            self.assertIsNone(
                self.control._accessible_desktop("type_text", label="Telegram", text="hi")
            )

    def test_an_exception_inside_the_accessibility_path_falls_through(self):
        def explode(label, text):
            raise RuntimeError("COM said no")

        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "type_into", explode
        ):
            self.assertIsNone(
                self.control._accessible_desktop("type_text", label="WhatsApp", text="hi")
            )

    def test_launch_is_never_routed_through_the_control_tree(self):
        with patch.object(self.ui, "capability", self.usable):
            self.assertIsNone(
                self.control._accessible_desktop("launch", label="WhatsApp", text="")
            )

    def test_the_real_driver_tries_names_before_it_imports_pygetwindow(self):
        source = Path(__file__).with_name("app_control.py").read_text(encoding="utf-8")
        body = source.split("def _real_desktop")[1]
        named_at = body.find("_accessible_desktop(")
        blind_at = body.find("import pygetwindow")
        self.assertGreater(named_at, 0, "the accessibility path is gone")
        self.assertGreater(blind_at, 0, "the keystroke fallback is gone and it is still needed")
        self.assertLess(named_at, blind_at)

    def test_a_named_field_is_set_rather_than_typed_at(self):
        calls: list[tuple] = []

        def set_text(label, name, text):
            calls.append((label, name, text))
            return {"ok": True, "detail": f'Set "{name}".', "verified": True}

        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "set_text", set_text
        ), patch.object(self.ui, "type_into", lambda *a, **k: self.fail("should not be reached")):
            result = self.control._accessible_desktop(
                "type_text", label="WhatsApp", text="hi", target="Search"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [("WhatsApp", "Search", "hi")])

    def test_a_click_with_no_tree_says_so_instead_of_using_coordinates(self):
        """`click` has no keystroke fallback, so the refusal is the answer."""
        absent = lambda: self.ui.Capability(False, False, "pywinauto is not importable")  # noqa: E731
        with patch.object(self.ui, "capability", absent):
            result = self.control._accessible_desktop("click", label="Telegram", text="", target="Send")
        self.assertIsNotNone(result, "click must not fall through to a blind pointer")
        self.assertFalse(result["ok"])
        self.assertIn("pywinauto", result["detail"])

    def test_a_failed_click_is_returned_not_swallowed(self):
        missing = {"ok": False, "detail": 'Nothing in this window is called "Send".'}
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "click", lambda label, name: missing
        ):
            result = self.control._accessible_desktop(
                "click", label="WhatsApp", text="", target="Send"
            )
        self.assertEqual(result, missing)

    def test_reading_a_frame_only_window_is_a_failure_not_a_list_of_caption_buttons(self):
        frame = {
            "ok": True,
            "sees_inside": False,
            "controls": [{"name": "Minimize"}, {"name": "Close"}],
            "actionable": ["Minimize", "Close"],
            "editable": [],
            "detail": '"Telegram" publishes only its window frame (5 named controls)',
        }
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "controls", lambda label: frame
        ):
            result = self.control._accessible_desktop("read_window", label="Telegram", text="")
        self.assertFalse(result["ok"], "a frame is not a read")
        self.assertIn("only its window frame", result["detail"])

    def test_reading_a_window_returns_its_names_as_text(self):
        seen = {
            "ok": True,
            "sees_inside": True,
            "controls": [{"name": "Chats"}, {"name": "Search or start a new chat"}, {"name": ""}],
            "actionable": ["Chats"],
            "editable": ["Search or start a new chat"],
            "detail": '191 named controls in "(8) WhatsApp".',
        }
        with patch.object(self.ui, "capability", self.usable), patch.object(
            self.ui, "controls", lambda label: seen
        ):
            result = self.control._accessible_desktop("read_window", label="WhatsApp", text="")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Chats\nSearch or start a new chat")
        self.assertIn("1 of them can be clicked by name", result["detail"])
        self.assertIn("1 can be typed into", result["detail"])


class ModelRouteTests(unittest.TestCase):
    """Two providers, one cascade.

    Ollama is not installed on the machine these were written on, which is the
    normal case and the reason discovery takes its transport as an argument: the
    parsing and the ordering are the parts worth pinning, and neither of them
    needs a 3 GB download to be tested.
    """

    def setUp(self):
        from backend import model_routes

        self.routes = model_routes
        model_routes.reset_discovery_cache()
        self.addCleanup(model_routes.reset_discovery_cache)
        self.local = [
            model_routes.LocalModel(
                name="gemma3:4b", size_bytes=3_338_801_804, parameter_size="4.3B", quantization="Q4_K_M"
            ),
            model_routes.LocalModel(name="llama3.2:3b", size_bytes=2_019_393_189),
        ]

    @staticmethod
    def _tags(*names_and_sizes):
        return lambda url, timeout: {
            "models": [{"model": name, "size": size} for name, size in names_and_sizes]
        }

    def test_a_local_model_keeps_its_provider_in_its_id_and_loses_it_on_the_wire(self):
        # The prefix exists so the cascade knows which server to call. Sending it
        # would be a 404: the local server has never heard of "ollama/".
        self.assertEqual(self.routes.provider_of("ollama/gemma3:4b"), "ollama")
        self.assertEqual(self.routes.provider_of("openai/gpt-4o-mini"), "openrouter")
        self.assertEqual(self.routes.wire_name("ollama/gemma3:4b"), "gemma3:4b")
        self.assertEqual(self.routes.wire_name("openai/gpt-4o-mini"), "openai/gpt-4o-mini")

    def test_discovery_reads_the_servers_own_list(self):
        models, error = self.routes.discover_local_models(
            base_url="http://stub", transport=self._tags(("gemma3:4b", 3_000_000_000))
        )
        self.assertEqual(error, "")
        self.assertEqual([m.model_id for m in models], ["ollama/gemma3:4b"])
        self.assertEqual(models[0].size_label, "3.0 GB")

    def test_an_unreachable_server_and_an_empty_one_say_different_things(self):
        def refuse(url, timeout):
            raise OSError("Connection refused")

        _, unreachable = self.routes.discover_local_models(base_url="http://stub", transport=refuse)
        _, empty = self.routes.discover_local_models(base_url="http://stub", transport=self._tags())
        # Different next steps for the user: install it, versus pull a model.
        self.assertIn("not running", unreachable)
        self.assertIn("no models pulled", empty)
        self.assertNotEqual(unreachable, empty)

    def test_the_cloud_is_tried_before_local_by_default(self):
        # A quantised 4B model is not as good as the hosted routes, so local is
        # the floor under an outage rather than the default answer.
        cascade = self.routes.merge_candidates(["a/free", "b/free"], local=self.local, preferred="")
        self.assertEqual(cascade[:2], ["a/free", "b/free"])
        self.assertEqual(cascade[2:], ["ollama/gemma3:4b", "ollama/llama3.2:3b"])

    def test_local_still_answers_when_every_cloud_route_is_gone(self):
        cascade = self.routes.merge_candidates([], local=self.local, preferred="")
        self.assertEqual(cascade, ["ollama/gemma3:4b", "ollama/llama3.2:3b"])

    def test_what_the_user_picked_goes_first(self):
        cascade = self.routes.merge_candidates(
            ["a/free"], local=self.local, preferred="ollama/llama3.2:3b"
        )
        self.assertEqual(cascade[0], "ollama/llama3.2:3b")
        # And the cloud remains behind it as the failover, not deleted.
        self.assertIn("a/free", cascade)

    def test_a_preference_for_a_deleted_model_is_ignored_rather_than_tried(self):
        # Trying it would spend a turn proving what discovery already knew.
        cascade = self.routes.merge_candidates(
            ["a/free"], local=self.local, preferred="ollama/deleted:70b"
        )
        self.assertNotIn("ollama/deleted:70b", cascade)
        self.assertEqual(cascade[0], "a/free")

    def test_voice_does_not_reach_for_a_local_model_while_a_cloud_route_survives(self):
        # `VOICE_MODELS` membership is decided by what a model emits; no local
        # model has been observed emitting anything, so none has earned a place
        # in front of a route that has.
        cascade = self.routes.merge_candidates(
            ["a/free"], local=self.local, preferred="", voice=True, limit=3
        )
        self.assertEqual(cascade, ["a/free"])

    def test_voice_falls_back_to_local_rather_than_going_silent(self):
        cascade = self.routes.merge_candidates(
            [], local=self.local, preferred="", voice=True, limit=3
        )
        self.assertEqual(cascade[0], "ollama/gemma3:4b")

    def test_a_preference_survives_a_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "model_route.json"
            self.routes.write_preference("ollama/gemma3:4b", path=path)
            self.assertEqual(self.routes.preferred_model(path=path), "ollama/gemma3:4b")
            self.routes.write_preference("", path=path)
            self.assertEqual(self.routes.preferred_model(path=path), "")

    def test_a_corrupt_preference_file_is_no_preference_rather_than_a_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model_route.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(self.routes.preferred_model(path=path), "")

    def test_the_local_probe_uses_the_v4_loopback(self):
        # Measured: `localhost` resolves to two addresses here, so a probe that
        # has to give up pays the connect timeout twice — 628 ms against a 300 ms
        # timeout. Ollama binds the v4 address anyway.
        self.assertIn("127.0.0.1", self.routes.DEFAULT_OLLAMA_BASE)
        connect_timeout = self.routes._PROBE_TIMEOUT[0]
        self.assertLessEqual(connect_timeout, 0.5)
        # And a machine without Ollama must not re-pay that on every reply.
        self.assertGreaterEqual(self.routes._NEGATIVE_TTL_S, 60.0)
        self.assertLess(self.routes._POSITIVE_TTL_S, self.routes._NEGATIVE_TTL_S)

    def test_a_failed_probe_is_remembered_for_longer_than_a_successful_one(self):
        calls = []

        def count(url, timeout):
            calls.append(url)
            raise OSError("Connection refused")

        with patch.object(self.routes, "_default_transport", count):
            self.routes.discover_local_models()
            self.routes.discover_local_models()
        self.assertEqual(len(calls), 1)

    def test_the_cascade_drops_cloud_routes_entirely_when_there_is_no_key(self):
        # Not merely demoted. The cascade stops on an auth failure by design, so
        # a keyless cloud route left in front of Ollama would end the turn before
        # Ollama was reached.
        with patch.object(ai_engine_module, "openrouter_configured", lambda: False), patch.object(
            ai_engine_module.model_routes, "discover_local_models", lambda **_: (self.local, "")
        ):
            cascade = ai_engine_module._openrouter_model_candidates()
        self.assertEqual(cascade, ["ollama/gemma3:4b", "ollama/llama3.2:3b"])

    def test_a_local_model_gets_a_client_pointed_at_the_local_server(self):
        cloud = ai_engine_module._client_for_model("openai/gpt-4o-mini", timeout=9.0)
        local = ai_engine_module._client_for_model("ollama/gemma3:4b")
        self.assertIn("openrouter.ai", str(cloud.base_url))
        self.assertIn("127.0.0.1", str(local.base_url))
        self.assertTrue(str(local.base_url).rstrip("/").endswith("/v1"))

    def test_an_auth_failure_on_one_provider_does_not_end_the_other(self):
        # The old cascade broke out of every loop on an auth error, which was
        # right with one provider and would now skip a local model that needs no
        # key at all.
        source = Path(ai_engine_module.__file__).read_text(encoding="utf-8")
        self.assertIn("dead_providers.add(model_routes.provider_of(model_name))", source)
        self.assertIn("if model_routes.provider_of(model_name) in dead_providers:", source)

    def test_every_provider_call_site_routes_by_model_rather_than_by_constant(self):
        # One client hoisted above the loop was correct while every route lived
        # at the same base URL. Any survivor of that shape would send a local
        # model name to OpenRouter.
        for name in ("ai_engine.py", "voice_executor.py"):
            source = (Path(ai_engine_module.__file__).parent / name).read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in source.splitlines()
                if "_openrouter_client()" in line and "def " not in line and "cache" not in line
            ]
            self.assertEqual(
                offenders,
                ["client = _openrouter_client()"] if name == "ai_engine.py" else [],
                f"{name} still calls the OpenRouter client directly: {offenders}",
            )


if __name__ == "__main__":
    unittest.main()



