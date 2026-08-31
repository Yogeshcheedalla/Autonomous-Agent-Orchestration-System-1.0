"""
voice_engine.py  –  Unified Voice Intelligence Engine for Akansha AI OS
==========================================================================
Replaces and integrates:
  - speech_data_pipeline.py     (phonetic normalization, intent classification)
  - dynamic_jarvis_llm.py       (dynamic LLM response for voice)
  - session_verifier.py         (login/session check before site automation)
  - Scattered voice helpers in main.py

Architecture:
  Raw audio (browser) → /api/voice/stt (transcribe)
                      → VoiceEngine.process(transcript)
                      → VoiceIntent  (QUESTION / TASK / COMMAND / CONTROL)
                      → route to: chat LLM  |  browser automation  |  jarvis session
                      → /api/voice/tts (speak response back)

NLP Pipeline (no external deps beyond standard library + openai already installed):
  1. Noise removal  (fillers: um, uh, like, you know…)
  2. Phonetic correction  (LLM-based, not a hardcoded dict)
  3. Intent classification  (keyword + regex + confidence scoring)
  4. Stop-word detection  (when to stop listening, when to wait)
  5. Entity extraction  (site, action, parameters)
  6. Session context  (login state, active automation, barge-in detection)
"""

from __future__ import annotations

import re
import time
import random
import logging
import asyncio
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .voice_kernel.intent import detect_control_phrase
from .voice_dialog import PendingSend, open_send_dialog

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  NLP CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Acoustic fillers to strip before any processing
FILLER_WORDS = re.compile(
    r"\b(um+|uh+|hmm+|er+|ah+|like|you know|basically|actually|literally|"
    r"kind of|sort of|i mean|so yeah|right\?|okay so)\b",
    re.IGNORECASE,
)

# Control commands that always override and stop current task
STOP_COMMANDS = {
    "stop", "cancel", "abort", "halt", "quit", "never mind", "nevermind",
    "forget it", "stop that", "stop now", "cancel that", "wait", "hold on",
    "pause", "pause that", "put it on hold",
}

RESUME_COMMANDS = {
    "resume", "go ahead", "proceed", "carry on",
    "keep going", "yes continue", "start again",
    # NOTE: bare "continue" is NOT here — it is too common in task phrases
    # like "continue my Python course" or "continue where I left off".
    # Use "yes continue" or "resume" for restart signals instead.
}

# Words that indicate the user is still speaking (wait before processing)
CONTINUATION_INDICATORS = {
    "and then", "also", "additionally", "furthermore", "next",
    "after that", "then", "plus", "as well as",
}

# Intent category signal words
_QUESTION_SIGNALS = re.compile(
    r"\b(what|who|when|where|why|how|is|are|does|did|can|could|should|"
    r"would|tell me|explain|define|show me|enti|ela|em|enti antav)\b",
    re.IGNORECASE,
)

_TASK_SIGNALS = re.compile(
    r"\b(open|go to|navigate|search|find|play|solve|complete|enroll|"
    r"start|download|submit|buy|book|send|message|create|write|run|"
    r"install|update|delete|move|copy|save|upload|login|sign in)\b",
    re.IGNORECASE,
)

_COMMAND_SIGNALS = re.compile(
    r"\b(pause|resume|stop|cancel|scroll|click|type|press|volume|"
    r"brightness|mute|unmute|maximize|minimize|close|switch|tab|"
    r"next|previous|back|forward|refresh|zoom)\b",
    re.IGNORECASE,
)

# Sites that need real browser automation
AUTOMATION_SITES = {
    "youtube", "codechef", "leetcode", "github", "coursera",
    "linkedin learning", "linkedin", "hackerrank", "codeforces",
    "geeksforgeeks", "udemy", "nptel", "google", "instagram",
    "twitter", "facebook", "whatsapp", "telegram",
}

# Language mode info — served to frontend for display
LANGUAGE_INFO = {
    "english": {
        "code": "EN",
        "label": "English",
        "flag": "🇺🇸",
        "description": "Pure English input and output",
        "example": "Open YouTube and search for cooking videos",
        "color": "blue",
    },
    "telugu": {
        "code": "TE",
        "label": "Telugu",
        "flag": "🇮🇳",
        "description": "Telugu script — నేరుగా తెలుగులో మాట్లాడండి",
        "example": "యూట్యూబ్ తెరువు, వంట వీడియోలు వెతుకు",
        "color": "orange",
    },
    "hindi": {
        "code": "HI",
        "label": "Hindi",
        "flag": "🇮🇳",
        "description": "Hindi — Devanagari or Roman Hindi",
        "example": "YouTube kholiye aur cooking videos dhundhiye",
        "color": "green",
    },
    "mixed": {
        "code": "MX",
        "label": "Mixed",
        "flag": "🌐",
        "description": "Telugu-English mix (Telglish) — most natural mode",
        "example": "YouTube lo AR Rahman songs search cheyyi",
        "color": "purple",
    },
}

# Human-like emotion phrases for different situations
EMOTION_PHRASES = {
    "greeting": {
        "english":  ["Hey! What's up? How can I help?", "Hi there! What do you need?", "Hello! Ready to help!"],
        "telugu":   ["Heyy mawa! Em chestunnav ra? Ela help cheyali?", "Cheppu ra! Em kavali?", "Enti mawa, cheppu cheppu!"],
        "hindi":    ["Haan bolo yaar! Kya chahiye?", "Hey! Kya karna hai bhai?", "Bol bhai, kaise madad karun?"],
        "mixed":    ["Hey mawa! Em cheddam ra? What do you need?", "Cheppu ra, what's up?"],
    },
    "thinking": {
        "english":  ["Hmm, let me think about that...", "Give me a second...", "One moment..."],
        "telugu":   ["Ayyo mawa, konchem wait cheyyi ra...", "Oka second ra...", "Chuddam cheppu mawa..."],
        "hindi":    ["Ek second yaar... soch rahi hoon...", "Ruko bhai, dekh rahi hoon...", "Hmm..."],
        "mixed":    ["Wait ra mawa, chesthunna... one sec...", "Thinking ga undi cheppu mawa, wait..."],
    },
    "task_started": {
        "english":  ["On it! Starting now.", "Sure, let me do that!", "Got it, working on it!"],
        "telugu":   ["Chesthunna ra! Ippude start chestha mawa.", "Sare ra, chestha!", "Okay mawa, chesthunna!"],
        "hindi":    ["Kar rahi hoon yaar! Abhi shuru karti hoon.", "Theek hai bhai, karte hain!", "Okay lag gayi!"],
        "mixed":    ["Okay chestunna ra mawa! Starting now.", "Got it, ippude chestha ra!", "Sure mawa, working on it!"],
    },
    "task_done": {
        "english":  ["Done! All finished.", "Completed! Anything else?", "All set!"],
        "telugu":   ["Ayyindi ra mawa! Inkemi kavali?", "Complete ayyindi cheppu mawa!", "Chestha ayyindi ra!"],
        "hindi":    ["Ho gaya yaar! Aur kuch chahiye?", "Complete bhai! Kya aur karein?", "Done!"],
        "mixed":    ["Done ayyindi ra! Anything else mawa?", "Complete chestha! Em cheddam inka ra?"],
    },
    "error": {
        "english":  ["Oops, something went wrong. Try again?", "That didn't work. Want me to retry?", "Hmm, had trouble with that."],
        "telugu":   ["Ayyo mawa, enti jarigindi ra? Retry chestava?", "Pani kavvaledu ra, try chestha mawa?", "Hmm, problem ochindi cheppu mawa."],
        "hindi":    ["Oops yaar, kuch galat hua. Dobara try karun?", "Kaam nahi hua bhai. Phir try karein?", "Hmm, thoda problem hua."],
        "mixed":    ["Ayyo mawa, problem ochindi ra! Try again chestava?", "Kavvaledu ra, retry chestha mawa?"],
    },
    "listening": {
        "english":  ["I'm listening...", "Go ahead...", "Yes, tell me..."],
        "telugu":   ["Vinthunnanu ra...", "Cheppu mawa...", "Ha ra, cheppu..."],
        "hindi":    ["Sun rahi hoon yaar...", "Boliye bhai...", "Haan bolo..."],
        "mixed":    ["Listening ra mawa... cheppu!", "Vinthunnanu, go ahead ra..."],
    },
    "stop": {
        "english":  ["Stopping now. What else?", "Okay, cancelled. Need anything?", "Stopped!"],
        "telugu":   ["Aapesthunna ra mawa. Inkemi kavali?", "Okay cancel chestha ra. Em cheddam?", "Aapitha mawa!"],
        "hindi":    ["Ruk gayi yaar. Aur kuch?", "Cancel kar diya bhai. Kya chahiye?", "Rok diya!"],
        "mixed":    ["Stopping ra mawa! Em cheddam inka?", "Cancel chestha ra, anything else?"],
    },
    "happy": {
        "english":  ["That's great! I love helping with this!", "Awesome! Let's go!", "So cool!"],
        "telugu":   ["Waah mawa baagundhii ra! Idi cheyyadam super fun!", "Superr ra cheppu mawa! Chestham!", "Waah mawa waah!"],
        "hindi":    ["Wah yaar bahut achha! Mujhe ye karna pasand hai!", "Awesome bhai! Chalo!", "Zabardast yaar!"],
        "mixed":    ["Super mawa! Idi cheyyadam love chestha ra!", "Awesome ra, chestham mawa!"],
    },
    "sad": {
        "english":  ["Oh no, I'm sorry to hear that...", "That's tough. I'm here for you.", "Don't worry, I'll help."],
        "telugu":   ["Ayyo mawa, sorry vinataniki ra...", "Kastha ga undi ra. Nenu unnanu mawa.", "Worry avvakku ra, chestha mawa."],
        "hindi":    ["Oh no yaar, bahut bura laga... Sorry.", "Mushkil hai bhai. Main hoon na.", "Tension mat lo yaar, karenge."],
        "mixed":    ["Ayyo mawa, sorry to hear ra... I'm here.", "Kastha undi ra, don't worry I'm here mawa."],
    },
    "confirmation_needed": {
        "english":  ["Just to confirm — shall I go ahead?", "Are you sure? I'll do it.", "Should I proceed?"],
        "telugu":   ["Confirm chestunavaa ra mawa? Cheyali?", "Sure aa ra? Chesthanu mawa.", "Proceed cheyali ra?"],
        "hindi":    ["Pakka yaar? Karoon kya?", "Confirm karo bhai, karta hoon.", "Aage badhun?"],
        "mixed":    ["Confirm aa ra mawa? Shall I go ahead?", "Sure na ra? Chestha mawa!"],
    },
}

# Silence/end-of-utterance signals (ms of silence before processing)
VAD_SILENCE_MS = 1200   # 1.2 seconds of silence = end of utterance
VAD_SHORT_MS   = 600    # 0.6 seconds for short commands


# ─────────────────────────────────────────────────────────────────────────────
# 2.  DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VoiceIntent:
    """Structured output of the NLP pipeline."""
    raw_transcript: str
    cleaned_transcript: str
    intent_category: str          # QUESTION | TASK | COMMAND | CONTROL | UNKNOWN
    target_site: Optional[str]    # e.g. "youtube", "codechef"
    target_action: str            # e.g. "search", "play", "scroll_down"
    parameters: Dict[str, Any]    # e.g. {"query": "AR Rahman", "language": "Telugu"}
    requires_automation: bool     # True = send to browser automation
    requires_login_check: bool    # True = ask user if logged in first
    is_stop_command: bool
    is_resume_command: bool
    is_continuation: bool         # User is mid-sentence; keep listening
    confidence: float             # 0.0–1.0
    language: str                 # "english" | "telugu" | "hindi" | "mixed"
    recommended_silence_ms: int   # How long to wait before processing
    extracted_fillers: List[str]  # Removed filler words (for debug)


@dataclass
class SessionState:
    """Tracks what's currently happening so the voice engine stays in context."""
    active_automation: Optional[str] = None   # session_id of running Jarvis session
    awaiting_login_confirm: Optional[str] = None  # site waiting for login confirmation
    last_intent: Optional[VoiceIntent] = None
    pending_continuation: str = ""            # partial utterance buffer
    login_confirmed_sites: Dict[str, bool] = field(default_factory=dict)
    #: A send request that is still missing slots or still needs a yes. Held per
    #: session because it is a conversation, not a request: the platform arrives
    #: in one utterance and the confirmation in another, and they must be the same
    #: user's. See `voice_dialog`.
    pending_send: Optional["PendingSend"] = None
    turn_count: int = 0
    last_active_at: float = field(default_factory=time.time)

    def mark_login_confirmed(self, site: str) -> None:
        self.login_confirmed_sites[site] = True
        self.awaiting_login_confirm = None

    def is_login_confirmed(self, site: str) -> bool:
        return self.login_confirmed_sites.get(site, False)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  NLP PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

class VoiceNLPPipeline:
    """
    Pure-Python NLP pipeline — no external ML deps beyond what's already installed.
    Stages:
      1. Filler removal
      2. Language detection
      3. Phonetic correction (LLM-based when available, regex fallback)
      4. Intent classification
      5. Entity extraction
      6. VAD silence recommendation
    """

    _use_intelligent_resolver: bool = True

    def __init__(self) -> None:
        self._use_intelligent_resolver: bool = True
        self._intent_resolver = None

    def _get_resolver(self):
        """Lazy-initialize IntelligentIntentResolver; returns None if deps unavailable."""
        if not self._use_intelligent_resolver:
            return None
        if hasattr(self, '_intent_resolver') and self._intent_resolver is not None:
            return self._intent_resolver
        try:
            import os
            from .agent_modules.intelligent_intent_resolver import IntelligentIntentResolver
            from .agent_modules.intelligent_intent_resolver.intent_context_manager import IntentContextManager
            from .agent_modules.nlp_processor import NLPContextProcessor
            from .agent_modules.reasoning_engine import ReasoningEngine
            from .agent_modules.multilingual_engine import MultilingualEngine
            from openai import OpenAI
            api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY", "")
            if not api_key:
                return None
            llm_client = OpenAI(
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1",
            )
            self._intent_resolver = IntelligentIntentResolver(
                nlp_processor=NLPContextProcessor(),
                reasoning_engine=ReasoningEngine(),
                multilingual_engine=MultilingualEngine(),
                llm_client=llm_client,
                context_manager=IntentContextManager(),
                debug=False,
            )
            return self._intent_resolver
        except Exception:
            return None

    # ── Stage 1: Filler removal ───────────────────────────────────────────
    def remove_fillers(self, text: str) -> Tuple[str, List[str]]:
        found: List[str] = FILLER_WORDS.findall(text)
        cleaned = FILLER_WORDS.sub("", text)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, found

    # ── Stage 2: Language detection ───────────────────────────────────────
    def detect_language(self, text: str) -> str:
        telugu_chars = len(re.findall(r"[\u0C00-\u0C7F]", text))
        hindi_chars = len(re.findall(r"[\u0900-\u097F]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        words = set(re.findall(r"[A-Za-z]+", text.lower()))
        telugu_roman = {"naku", "meeru", "ela", "enti", "cheppu", "unnav", "bagundi",
                        "sare", "aithe", "ippudu", "anna", "andi", "ante", "lo", "ki"}
        hindi_roman = {"kya", "kaise", "mujhe", "batao", "hai", "nahi", "aap",
                       "theek", "haan", "yaar", "bhai", "karo", "chahiye"}
        if hindi_chars > 2:
            return "hindi"
        if telugu_chars > 2:
            # Telugu script present — if also significant Latin/English, it's mixed
            if latin > 3:
                return "mixed"
            return "telugu"
        # Telugu-Roman words AND English words together = mixed
        telugu_roman_hits = len(words & telugu_roman)
        if telugu_roman_hits >= 2:
            # If there are also common English words, it's a Telglish mix
            english_words = words - telugu_roman - hindi_roman
            if latin > 3 or len(english_words) >= 2:
                return "mixed"
            return "mixed"
        if len(words & hindi_roman) >= 2:
            return "hindi"
        return "english"

    # ── Stage 3: Phonetic normalization (LLM-based when available, regex fallback) ─
    def _resolve_once(self, text: str):
        """Run the intelligent resolver at most once per utterance.

        `process` used to resolve twice — once inside `normalize_phonetics` and
        again to enrich the entity dict — which meant two blocking LLM round
        trips before Akansha said a single word. One resolution serves both.
        Fillers are stripped before this point, which is also the better input.
        """
        if not self._use_intelligent_resolver:
            return None
        resolver = self._get_resolver()
        if resolver is None:
            return None
        try:
            return resolver.resolve_intent(text)
        except Exception as e:
            logger.warning("Intent resolver failed, using regex fallback: %s", e)
            return None

    def _normalized_from(self, resolution, text: str) -> str:
        """Pick the best transcript out of a resolution, else fall back to regex."""
        if resolution is not None:
            resolved = resolution.resolved_intent or text
            # Use the resolver's reading only when it actually identified something.
            if resolution.target_entity and resolution.confidence > 0.6:
                return getattr(resolution, "cleaned_transcript", None) or resolved
            return text
        return self._apply_legacy_normalization(text)

    def normalize_phonetics(self, text: str) -> str:
        """Smart normalization: LLM-based resolver first, regex fallback."""
        return self._normalized_from(self._resolve_once(text), text)

    def _apply_legacy_normalization(self, text: str) -> str:
        """
        Smart regex-based normalization (legacy fallback).
        Uses pattern rules rather than a fixed dictionary so new words work.
        """
        t = text

        # Space-separated compound names → joined (e.g. "you tube" → "youtube")
        compound_patterns = [
            (r"\byou\s+tube\b", "youtube"),
            (r"\bu\s+tube\b", "youtube"),
            (r"\bgit\s+hub\b", "github"),
            (r"\bget\s+hub\b", "github"),
            (r"\bcode\s+che[fe]f\b", "codechef"),
            (r"\bcode\s+chef\b", "codechef"),
            (r"\blink[e]?d?\s+in\b", "linkedin"),
            (r"\bvs\s+code\b", "vscode"),
            (r"\bopen\s+works?\b", "openwork"),
            (r"\back?ans+h[ae]\b", "akansha"),
            (r"\bjar\s*vis\b", "jarvis"),
        ]
        for pattern, replacement in compound_patterns:
            t = re.sub(pattern, replacement, t, flags=re.IGNORECASE)

        # Normalize common Telugu-English phrases
        t = re.sub(r"\btelugu\s+lo\b", "in Telugu", t, flags=re.IGNORECASE)
        t = re.sub(r"\bhindi\s+me(?:in)?\b", "in Hindi", t, flags=re.IGNORECASE)

        # ── Telugu Unicode → Latin site name mappings ──────────────────
        # Allows pure Telugu-script voice commands to trigger automations
        telugu_site_map = {
            "యూట్యూబ్": "youtube",
            "యూట్యూబ్‌ని": "youtube",
            "గిట్‌హబ్": "github",
            "కోడ్‌చెఫ్": "codechef",
            "కోడ్చెఫ్": "codechef",
            "లీట్‌కోడ్": "leetcode",
            "కోర్సెరా": "coursera",
            "లింక్డ్‌ఇన్": "linkedin",
        }
        for telugu_word, latin_name in telugu_site_map.items():
            if telugu_word in t:
                t = t.replace(telugu_word, latin_name)

        # ── Telugu action word → English normalization ─────────────────
        telugu_action_map = {
            "తెరువు": "open",
            "తెరు": "open",
            "వెతుకు": "search",
            "వెతకు": "search",
            "వెతుకో": "search",
            "ప్లే చేయి": "play",
            "ప్లే చేయ్యి": "play",
            "ఆపు": "stop",
            "పాజ్ చేయి": "pause",
        }
        for telugu_word, english_word in telugu_action_map.items():
            if telugu_word in t:
                t = t.replace(telugu_word, english_word)

        return t.strip()

    # ── Stage 4: Intent classification ───────────────────────────────────
    # Pattern for timed-pause parameters, e.g. "pause after 30 seconds" —
    # these are video-control parameters embedded in task utterances, NOT stop commands.
    _TIMED_PAUSE_RE = re.compile(
        r"\bpause\s+after\s+\d+\s*(?:second|sec|minute|min)s?\b",
        re.IGNORECASE,
    )

    def classify_intent(self, text: str) -> Tuple[str, float]:
        lowered = text.lower()

        # Strip timed-pause phrases before stop-command detection so that
        # "...pause after 30 seconds" inside a task utterance is treated as a
        # parameter rather than a CONTROL signal.
        stripped_for_control = self._TIMED_PAUSE_RE.sub("", lowered)

        # Control always wins — but only when the control word is actually
        # addressed to us. This used to be a substring scan over STOP_COMMANDS,
        # which classified "i am waiting for the build to finish", "that was
        # quite good" and "find the paused video" as CONTROL at 0.98 confidence
        # and threw the user's real request away. `detect_control_phrase` is the
        # voice kernel's tested rule (word boundaries plus position); reusing it
        # keeps both paths agreeing about what a stop word is.
        control = detect_control_phrase(stripped_for_control)
        if control in ("stop", "cancel", "pause"):
            return "CONTROL", 0.98
        if control == "resume":
            return "CONTROL", 0.95

        # Score each category
        task_hits = len(_TASK_SIGNALS.findall(text))
        cmd_hits = len(_COMMAND_SIGNALS.findall(text))
        q_hits = len(_QUESTION_SIGNALS.findall(text))

        # Site names push toward TASK
        site_boost = 2 if any(site in lowered for site in AUTOMATION_SITES) else 0
        task_hits += site_boost

        scores = {"TASK": task_hits, "COMMAND": cmd_hits, "QUESTION": q_hits}
        best_cat = max(scores, key=lambda k: scores[k])
        best_score = scores[best_cat]

        if best_score == 0:
            return "QUESTION", 0.5

        total = sum(scores.values()) or 1
        confidence = min(best_score / total + 0.3, 0.98)
        return best_cat, round(confidence, 2)

    # ── Stage 5: Entity extraction ────────────────────────────────────────
    def extract_entities(self, text: str) -> Dict[str, Any]:
        lowered = text.lower()
        entities: Dict[str, Any] = {}

        # Detect target site
        for site in AUTOMATION_SITES:
            if site in lowered:
                entities["site"] = site
                break

        # Detect language preference
        if "telugu" in lowered:
            entities["language_filter"] = "Telugu"
        elif "hindi" in lowered:
            entities["language_filter"] = "Hindi"

        # Detect pause duration (e.g. "pause after 30 seconds")
        pause_match = re.search(r"pause\s+after\s+(\d+)\s*(?:second|sec)", lowered)
        if pause_match:
            entities["pause_after_seconds"] = int(pause_match.group(1))

        # Detect programming language
        for lang in ["java", "python", "c++", "javascript", "typescript"]:
            if lang in lowered:
                entities["programming_language"] = lang
                break

        # Detect "Easy", "Medium", "Hard" difficulty
        for diff in ["easy", "medium", "hard"]:
            if diff in lowered:
                entities["difficulty"] = diff
                break

        # Detect login confirmation
        if re.search(r"\b(logged in|i am logged|yes i'm logged|already logged|yes logged)\b", lowered):
            entities["login_confirmed"] = True

        # Extract search query after common markers
        for marker in ["search for", "find", "search", "look for", "play"]:
            m = re.search(
                rf"\b{marker}\s+(.+?)(?:\s+and\s+|\s+filter|\s+play|\s+pause|\s+in\s+telugu|\s+in\s+hindi|$)",
                lowered
            )
            if m:
                query = m.group(1).strip(" .,")
                if len(query) > 2:
                    entities["search_query"] = query
                break

        # Detect "most starred", "best", "top"
        if re.search(r"\b(most starred|top result|best result|highest rated)\b", lowered):
            entities["sort"] = "top"

        return entities

    # ── Stage 6: VAD silence recommendation ──────────────────────────────
    def recommend_silence_ms(self, text: str, intent_category: str) -> int:
        """How long to wait in silence before treating input as complete."""
        lowered = text.lower()
        # Commands: act fast
        if intent_category == "COMMAND":
            return VAD_SHORT_MS
        # Continuation in progress: keep listening
        if any(w in lowered for w in CONTINUATION_INDICATORS):
            return VAD_SILENCE_MS + 800
        # Short text: probably done
        if len(text.split()) <= 5:
            return VAD_SHORT_MS
        return VAD_SILENCE_MS

    # ── Full pipeline ─────────────────────────────────────────────────────
    def process(self, raw_transcript: str) -> VoiceIntent:
        if not raw_transcript or not raw_transcript.strip():
            return VoiceIntent(
                raw_transcript="", cleaned_transcript="", intent_category="UNKNOWN",
                target_site=None, target_action="direct_qa", parameters={},
                requires_automation=False, requires_login_check=False,
                is_stop_command=False, is_resume_command=False, is_continuation=False,
                confidence=0.0, language="english", recommended_silence_ms=VAD_SILENCE_MS,
                extracted_fillers=[],
            )

        # Stage 1
        no_fillers, fillers = self.remove_fillers(raw_transcript)
        # Stage 2
        language = self.detect_language(raw_transcript)
        # Stage 3 — one resolution, reused for normalization and enrichment.
        _resolution = self._resolve_once(no_fillers)
        normalized = self._normalized_from(_resolution, no_fillers)
        # Stage 4
        intent_cat, confidence = self.classify_intent(normalized)
        # Stage 5
        entities = self.extract_entities(normalized)
        # Enrich entities dict with IIR resolution fields
        if _resolution is not None:
            entities["resolution_confidence"] = _resolution.confidence
            entities["resolved_by"] = _resolution.resolved_by
            entities["entity_type"] = _resolution.entity_type
            entities["canonical_url"] = _resolution.canonical_url
            entities["alternatives"] = _resolution.alternatives
            entities["requires_user_confirmation"] = _resolution.requires_clarification
        # Stage 6
        silence_ms = self.recommend_silence_ms(normalized, intent_cat)

        lowered = normalized.lower()
        site = entities.get("site")
        # Automation required when a known site is mentioned, regardless of
        # whether it classified as TASK, COMMAND, or QUESTION — all site-targeted
        # utterances need the real Playwright browser, not the chat LLM.
        requires_automation = bool(
            site and intent_cat in ("TASK", "COMMAND", "QUESTION")
        )
        # Login check only needed for sites that typically require auth
        AUTH_SITES = {"codechef", "leetcode", "coursera", "linkedin learning",
                      "linkedin", "hackerrank", "github", "codeforces", "udemy"}
        requires_login = (
            requires_automation
            and site in AUTH_SITES
            and not entities.get("login_confirmed", False)
        )

        # Determine action
        action = "direct_qa"
        if site == "youtube":
            action = "youtube_flow"
        elif site == "codechef":
            action = "codechef_flow"
        elif site == "leetcode":
            action = "leetcode_flow"
        elif site == "github":
            action = "github_flow"
        elif site == "coursera":
            action = "coursera_flow"
        elif site == "linkedin learning":
            action = "linkedin_learning_flow"
        elif intent_cat == "COMMAND":
            if "scroll" in lowered:
                action = "browser_scroll"
            elif re.search(r"\b(pause|resume|play)\b", lowered):
                action = "video_control"
            else:
                action = "desktop_command"
        elif intent_cat == "QUESTION":
            action = "direct_qa"

        # Same rule as classify_intent — these flags drive whether we cancel the
        # user's work, so they must not be substring guesses either.
        _control = detect_control_phrase(lowered) if intent_cat == "CONTROL" else None
        is_stop = _control in ("stop", "cancel", "pause")
        is_resume = _control == "resume"
        is_cont = any(w in lowered for w in CONTINUATION_INDICATORS)

        return VoiceIntent(
            raw_transcript=raw_transcript,
            cleaned_transcript=normalized,
            intent_category=intent_cat,
            target_site=site,
            target_action=action,
            parameters=entities,
            requires_automation=requires_automation,
            requires_login_check=requires_login,
            is_stop_command=is_stop,
            is_resume_command=is_resume,
            is_continuation=is_cont,
            confidence=confidence,
            language=language,
            recommended_silence_ms=silence_ms,
            extracted_fillers=fillers,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4.  SESSION-AWARE VOICE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class VoiceEngine:
    """
    Stateful voice engine that maintains session context across turns.

    Usage:
        engine = get_voice_engine(session_id)
        intent = engine.process("open youtube find AR Rahman songs in Telugu pause after 30 seconds")
        # intent.requires_automation → True → dispatch to site_dispatcher
        # intent.requires_login_check → check session state
        # engine.confirm_login("youtube") → updates state

    One engine per session. `SessionState` holds who is logged into what and how
    many turns have passed, so sharing a single engine between callers leaked
    one user's login state to everybody else.
    """

    def __init__(self) -> None:
        self.nlp = VoiceNLPPipeline()
        self.state = SessionState()

    def process(self, raw_transcript: str) -> VoiceIntent:
        """Main entry point — processes one voice turn."""
        intent = self.nlp.process(raw_transcript)

        # Handle login confirmation pattern across turns.
        if self.state.awaiting_login_confirm:
            verdict = _login_answer(raw_transcript)
            if verdict is True:
                site = self.state.awaiting_login_confirm
                self.state.mark_login_confirmed(site)
                self.state.awaiting_login_confirm = None
                intent.requires_login_check = False
                intent.parameters["login_confirmed"] = True
                logger.info("Login confirmed for site: %s", site)
            elif verdict is False:
                # "not yet" — keep waiting, but do not pretend it was a task.
                logger.info(
                    "Login not confirmed for %s", self.state.awaiting_login_confirm
                )
            elif intent.requires_automation:
                # The user moved on to something else. A stale flag here is how
                # "okay open youtube" used to be read as "yes, I am logged in".
                self.state.awaiting_login_confirm = None

        # Update awaiting state
        if intent.requires_login_check and intent.target_site:
            self.state.awaiting_login_confirm = intent.target_site

        # Handle stop/resume
        if intent.is_stop_command and self.state.active_automation:
            logger.info("STOP command received — suspending automation %s", self.state.active_automation)
        if intent.is_resume_command and self.state.active_automation:
            logger.info("RESUME command received — continuing automation %s", self.state.active_automation)

        self.state.last_intent = intent
        self.state.turn_count += 1
        self.state.last_active_at = time.time()

        logger.info(
            "VoiceEngine: [%s] site=%s action=%s confidence=%.2f lang=%s",
            intent.intent_category, intent.target_site, intent.target_action,
            intent.confidence, intent.language,
        )
        return intent

    def confirm_login(self, site: str) -> None:
        """Call this when user confirms they're logged in."""
        self.state.mark_login_confirmed(site)

    def set_active_automation(self, session_id: Optional[str]) -> None:
        self.state.active_automation = session_id

    def get_stop_word_response(self, language: str) -> str:
        """What Akansha says when she stops."""
        responses = {
            "telugu": "Okay, aapesthunna. Em kavali?",
            "hindi": "Theek hai, ruk gayi. Kya chahiye?",
            "mixed": "Okay, stopping. Em cheddam ippudu?",
            "english": "Okay, stopping. What would you like to do?",
        }
        return responses.get(language, responses["english"])

    def get_listen_prompt(self, language: str) -> str:
        """What Akansha says when she starts listening."""
        responses = {
            "telugu": "Vinthunnanu...",
            "hindi": "Sun rahi hoon...",
            "mixed": "Listening...",
            "english": "I'm listening...",
        }
        return responses.get(language, responses["english"])

    def get_wait_prompt(self, language: str) -> str:
        """What Akansha says while processing."""
        responses = {
            "telugu": "Chesthunna, konchem wait cheyyi...",
            "hindi": "Kar rahi hoon, ek second...",
            "mixed": "On it, one moment...",
            "english": "On it, one moment...",
        }
        return responses.get(language, responses["english"])

    def get_login_prompt(self, site: str, language: str) -> str:
        """Ask user to confirm login before automation."""
        site_label = site.title()
        responses = {
            "telugu": f"{site_label} lo login ayinaava Chrome lo?",
            "hindi": f"Kya aap {site_label} mein Chrome par logged in hain?",
            "mixed": f"Are you logged into {site_label} on Chrome?",
            "english": f"Are you already logged into {site_label} on Chrome?",
        }
        return responses.get(language, responses["english"])


# ─────────────────────────────────────────────────────────────────────────────
# 5.  PER-SESSION ENGINES
# ─────────────────────────────────────────────────────────────────────────────

#: An explicit answer that the user is (or is not) signed in. Matched only
#: against short replies — see `_login_answer`.
_LOGIN_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|done|ready|logged\s*in|log(?:ged)?\s*in\s*already"
    r"|already\s*(?:logged\s*in|signed\s*in|in)|i\s*am\s*(?:logged\s*)?in"
    r"|signed\s*in|ayyindi|ayyanu|ho\s*gaya|kar\s*liya)\b",
    re.IGNORECASE,
)
_LOGIN_NO = re.compile(
    r"\b(no|nope|nah|not\s*yet|not\s*logged\s*in|wait|hold\s*on|kaadu|ledu|nahi)\b",
    re.IGNORECASE,
)


def _login_answer(transcript: str) -> Optional[bool]:
    """Is this utterance an answer to "are you logged in?" — and which one?

    Returns True (yes), False (no) or None (not an answer at all).

    The old test was `\\b(yes|...|ok|okay)\\b` against the whole transcript, so
    "okay open youtube" silently marked the user as logged in and automation ran
    against a signed-out browser. An answer to a yes/no question is short, or it
    says "logged in" outright — anything longer is a new instruction.
    """
    text = (transcript or "").strip().lower()
    if not text:
        return None
    words = re.findall(r"[\w']+", text)
    explicit = re.search(r"\b(logged\s*in|signed\s*in|log\s*in\s*done)\b", text)
    # A yes/no answer is a short utterance; "I'm logged in now, go ahead" is
    # longer but says so explicitly, which is unambiguous either way.
    if len(words) > 4 and not explicit:
        return None
    if _LOGIN_NO.search(text):
        return False
    if _LOGIN_YES.search(text) or explicit:
        return True
    return None


def _apply_send_dialog(
    engine: "VoiceEngine",
    transcript: str,
    intent: VoiceIntent,
    response: Dict[str, Any],
) -> None:
    """Gate anything that would leave the machine behind slots and a yes.

    Mutates `response` in place, which is how the other injections in
    `process_voice_transcript` work.

    Three effects, in decreasing frequency:

    * Nothing. Most utterances are not about sending, so this returns having
      touched nothing.
    * `send_dialog_reply` — she asked for a missing slot, or a confirmation, or
      said she would not send. That reply *is* the turn: the model must not run,
      because given "who should I send it to?" it would answer the question
      itself rather than leave it for the user.
    * `requires_automation` with a rewritten `cleaned_transcript` — confirmed. The
      instruction handed onward names the platform, the recipient and the message,
      every one of which the user said out loud.
    """
    state = engine.state

    # Stop is the escape hatch and outranks everything, including a pending
    # confirmation. A half-built send that survives "stop" is a trap.
    if intent.is_stop_command:
        state.pending_send = None
        return

    turn = open_send_dialog(transcript, state.pending_send, intent.language)
    if turn is None:
        return

    state.pending_send = turn.pending
    dialog: Dict[str, Any] = {
        "prompt": turn.prompt,
        "ready": turn.ready,
        "cancelled": turn.cancelled,
    }
    if turn.pending is not None:
        dialog.update(turn.pending.to_dict())
    response["send_dialog"] = dialog
    response["tts_response"] = turn.prompt

    if turn.ready:
        response["cleaned_transcript"] = turn.instruction
        response["send_instruction"] = turn.instruction
        response["requires_automation"] = True
        response["requires_login_check"] = False
        return

    response["send_dialog_reply"] = turn.prompt
    response["requires_automation"] = False
    response["requires_login_check"] = False
    if turn.pending is not None:
        response["awaiting_send_slot"] = turn.pending.stage


#: Engines are per session: `SessionState` carries login confirmations and turn
#: count, and one shared instance handed every caller the same login state.
_voice_engines: "OrderedDict[str, VoiceEngine]" = OrderedDict()
_ENGINE_LOCK = threading.Lock()
#: Bound on retained sessions. Voice sessions are few and short-lived; this only
#: exists so a long-running server cannot grow without limit.
MAX_VOICE_SESSIONS = 64


def get_voice_engine(session_id: str = "default") -> VoiceEngine:
    """Return the engine for `session_id`, creating it on first use."""
    key = (session_id or "default").strip() or "default"
    with _ENGINE_LOCK:
        engine = _voice_engines.get(key)
        if engine is None:
            engine = VoiceEngine()
            _voice_engines[key] = engine
            while len(_voice_engines) > MAX_VOICE_SESSIONS:
                _voice_engines.popitem(last=False)
        else:
            _voice_engines.move_to_end(key)
        return engine


def drop_voice_engine(session_id: str) -> bool:
    """Forget one session's state. Returns True if there was something to drop."""
    with _ENGINE_LOCK:
        return _voice_engines.pop((session_id or "").strip(), None) is not None


# ─────────────────────────────────────────────────────────────────────────────
# 6.  FastAPI ENDPOINT HELPERS  (called from main.py)
# ─────────────────────────────────────────────────────────────────────────────

def process_voice_transcript(transcript: str, session_id: str = "default") -> Dict[str, Any]:
    """
    Called by /api/voice/process endpoint in main.py.
    Returns a structured dict the frontend uses to decide next action.

    `session_id` selects which conversation's state to use. The endpoints have
    always accepted one; it used to be discarded, so every caller shared a
    single login state and turn count.
    """
    engine = get_voice_engine(session_id)
    intent = engine.process(transcript)

    response: Dict[str, Any] = {
        "intent_category": intent.intent_category,
        "cleaned_transcript": intent.cleaned_transcript,
        "target_action": intent.target_action,
        "target_site": intent.target_site,
        "parameters": intent.parameters,
        "requires_automation": intent.requires_automation,
        "requires_login_check": intent.requires_login_check,
        "is_stop_command": intent.is_stop_command,
        "is_resume_command": intent.is_resume_command,
        "is_continuation": intent.is_continuation,
        "confidence": intent.confidence,
        "language": intent.language,
        "recommended_silence_ms": intent.recommended_silence_ms,
        "extracted_fillers": intent.extracted_fillers,
    }

    # Inject login prompt if needed
    if intent.requires_login_check and intent.target_site:
        response["login_prompt"] = engine.get_login_prompt(
            intent.target_site, intent.language
        )
        response["awaiting_login_confirm"] = True

    # ── Sending something needs slots and a yes ───────────────────────────────
    #
    # Placed after login and before the emotion phrase so it can override
    # routing: while a send is being assembled, the utterance is an answer to her
    # question, not a new instruction, and must not reach the model. A stop
    # command still wins, because that is the escape hatch.
    #
    # Nothing is sent from here. On confirmation the intent is rewritten into a
    # fully-slotted automation goal, which is the difference this makes: the
    # planner stops guessing at a platform and a recipient the user was never
    # asked about.
    _apply_send_dialog(engine, transcript, intent, response)

    # Inject stop/wait prompts
    if intent.is_stop_command:
        response["tts_response"] = engine.get_stop_word_response(intent.language)
    elif intent.is_continuation:
        response["keep_listening"] = True

    # Add human-like contextual phrase
    emotion = "listening"
    if intent.is_stop_command:
        emotion = "stop"
    elif intent.is_continuation:
        emotion = "thinking"
    elif intent.requires_automation:
        emotion = "task_started"
    elif intent.intent_category == "QUESTION":
        emotion = "thinking"
    response["emotion_phrase"] = get_emotion_phrase(emotion, intent.language)
    response["emotion"] = emotion

    return response


def confirm_login_for_site(site: str, session_id: str = "default") -> Dict[str, Any]:
    """Called when user confirms login — updates engine state."""
    get_voice_engine(session_id).confirm_login(site)
    return {"confirmed": True, "site": site, "session_id": session_id}


def reset_voice_session(session_id: str = "default") -> Dict[str, Any]:
    """Reset one session's engine state."""
    dropped = drop_voice_engine(session_id)
    get_voice_engine(session_id)
    return {"reset": True, "session_id": session_id, "had_state": dropped}


def get_language_info() -> Dict[str, Any]:
    """Return all language mode info for frontend display."""
    return LANGUAGE_INFO


def get_emotion_phrase(emotion: str, language: str) -> str:
    """Get a human-like phrase for the given emotion and language."""
    phrases = EMOTION_PHRASES.get(emotion, {})
    lang_key = language if language in phrases else "english"
    options = phrases.get(lang_key, phrases.get("english", ["..."]))
    return random.choice(options)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  BACKWARD COMPATIBILITY SHIM
#     system_prompt_builder.py imports WebSessionVerifier from session_verifier.
#     We define a minimal shim here so session_verifier.py can be deleted.
# ─────────────────────────────────────────────────────────────────────────────

class WebSessionVerifier:
    """
    Backward-compat shim. Real login state is now managed by VoiceEngine.SessionState.
    Kept only to satisfy system_prompt_builder.py's import without changes.
    """

    def verify_target_session(self, prompt: str, target_url_or_domain: str) -> Dict[str, Any]:
        engine = get_voice_engine()
        site = target_url_or_domain.replace("https://", "").replace("www.", "").split("/")[0]
        return {
            "site_domain": site,
            "is_logged_in": engine.state.is_login_confirmed(site),
            "requires_user_confirmation": not engine.state.is_login_confirmed(site),
            "status": "verified" if engine.state.is_login_confirmed(site) else "needs_login_confirmation",
        }

    def system_prompt_section(self) -> str:
        return """
# MODULE: WEB SESSION VERIFIER
- SMART SESSION CHECKING: Verifies whether target site/app is open and authenticated before performing automated actions.
- INTERACTIVE LOGIN PROMPTING: Prompts user when authentication status is unverified ("Is the site logged in on your browser?").
- CONTINUOUS RESUMPTION: Immediately transitions to target interface upon login confirmation and runs task without stopping until target is reached.
"""
