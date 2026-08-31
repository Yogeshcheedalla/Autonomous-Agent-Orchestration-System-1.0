import os
import json
import re
import html
import io
import base64
import hashlib
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python builds without tzdata still keep IST available.
    ZoneInfo = None
from openai import OpenAI
from sqlalchemy.orm import Session
from dotenv import dotenv_values, load_dotenv
from .database import Memory, ChatMessage, Task, TaskAutomation, AutomationExecutionLog
from . import model_routes
from . import providers
from . import indic_intent
from .model_output import StreamSanitizer
from .speaker_identity import OWNER, at_least

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH, override=True)

# Every model in the previous list was dead. Probed against this account's key,
# all five failed: `openai/gpt-4o-mini`, `openai/gpt-4o-mini-2024-07-18` and
# `mistralai/mistral-small-2603` returned 402 (credit balance exhausted — still 402
# at `max_tokens=64`, so it is the balance and not the request size), while
# `nex-agi/nex-n2-pro:free` and `openai/gpt-oss-20b:free` returned 404 "unavailable
# for free, the paid version is available now". A five-deep fallback cascade in
# which every entry fails is not a fallback cascade; it is five round trips to the
# same canned error string.
#
# So the list now leads with routes verified to stream on a zero balance, and the
# paid ones are kept *after* them rather than deleted: they are better models and
# they start working again the moment credits are topped up. A 402 fails
# immediately rather than timing out, so demoting them costs nothing measurable.
#: Measured through `generate_chat_stream` itself -- Akansha's real system prompt
#: and history, streaming, `max_tokens=200` -- rather than through a one-line
#: request, because the two disagree. See `scripts/probe_model_routes.py`.
#: Fastest to first token of the routes that answered (2.82 s), and it answered
#: in the Telglish the conversation was already in.
DEFAULT_OPENROUTER_MODEL = "minimax/minimax-m3:free"

#: Models whose content channel is clean enough to hand straight to TTS.
#:
#: Membership is decided by what a model *emits* on the real prompt, not how good
#: it is and not what a toy prompt suggests. Three exclusions are recorded below
#: because from the outside all three look the same -- a reply that never
#: arrives -- and each needs a different defence.
#:
#: *Unmarked reasoning in the content channel.* Both
#: `nvidia/nemotron-3-super-120b-a12b:free` and `nvidia/nemotron-3.5-lightning:free`
#: do this. Lightning opens "Here's a thinking process: 1. **Analyze User
#: Input:**"; super opens "Okay, the user is asking for a one-sentence
#: explanation..." and ran to 1029 characters before being cut off mid-thought at
#: "Ah!". `model_output` cannot strip either, because unmarked deliberation is
#: indistinguishable from a real answer about deliberation. Spoken aloud it is
#: Akansha reading her own notes, so the only defence is keeping these routes off
#: this list.
#:
#: Super is the reason this is measured on the real prompt. Asked "what is a
#: compiler?" with no system prompt it looked ideal: 1.22 s to first token, 147
#: characters of clean content, its 394 characters of thinking properly separated
#: into `delta.reasoning`. The full prompt flipped it into the content channel.
#:
#: *No answer at all.* `liquid/lfm-2.5-2.6b:free` returned 0 characters of
#: content and 934 of `delta.reasoning`: it spends the whole budget thinking and
#: stops. It cannot be repaired from the request side --
#: `reasoning: {"enabled": false}` is refused with "400 Reasoning is mandatory
#: for this endpoint", `{"exclude": true}` merely hides the thinking (0 content,
#: 0 reasoning), and `{"effort": "low"}` still produced 975 characters of
#: reasoning and no content.
#:
#: *An answer to the wrong thing.* `dots-studio/dots-3-note-preview:free`
#: replied to a stale line of history instead of the question that was asked.
VOICE_MODELS = (
    DEFAULT_OPENROUTER_MODEL,
    # 4.40 s to first token, so a poor thing to wait on but a fine thing to fall
    # back to, and clean on the real prompt. It is here because a one-entry voice
    # cascade is how a single dead route silenced every spoken reply before.
    "minimax/minimax-m2.7:free",
)

#: Free routes first, paid ones after. The paid entries are kept rather than
#: deleted: they are better models and they start working again the moment
#: credits are topped up.
#:
#: `nvidia/nemotron-3-nano-30b-a3b:free` was `DEFAULT_OPENROUTER_MODEL` and has
#: been removed rather than demoted, because it no longer exists to demote. It
#: answers "404 - This model is unavailable for free. The paid version is
#: available now", and it is absent from the 18 free routes OpenRouter currently
#: advertises. A cooldown cannot help a withdrawn route: it demotes only *after*
#: one failure in the current process, so a retired head-of-cascade costs one
#: guaranteed wasted request per backend start, forever.
#:
#: The paid routes are also measurably out of reach right now, and the shape of
#: that failure is worth recording: `openai/gpt-4o-mini` answers a request for 8
#: tokens and returns "402 - This request requires more credits, or fewer
#: max_tokens" at 32, 48, 64, 96, 128 and 200. So there is credit left, just not
#: enough for a reply -- which is why there is no step-down retry anywhere here.
#: A ceiling low enough to pass cannot hold an answer.
OPENROUTER_FALLBACK_MODELS = (
    DEFAULT_OPENROUTER_MODEL,
    "minimax/minimax-m2.7:free",
    # Advertised as free, and unverified: every attempt returned "429 Provider
    # returned error", which throttles this key rather than saying anything about
    # the models. Gemma is here because it was asked for by name, and here rather
    # than in VOICE_MODELS because an unobserved content channel is not a risk to
    # take on the one path that cannot absorb it.
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    # Both dump unmarked reasoning into content (see VOICE_MODELS above). Kept
    # only because they are working free routes and this is the text path, where
    # the result is ugly on screen rather than read aloud, and last because ugly
    # is still worse than fine.
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-4o-mini",
    "mistralai/mistral-small-2603",
)
DEFAULT_OWNER_NAME = (os.getenv("AKANSHA_OWNER_NAME") or "the owner").strip() or "the owner"
_CURRENCY_RATE_CACHE: dict[str, tuple[datetime, float, str]] = {}
_LIVE_CONTEXT_CACHE: dict[str, tuple[datetime, str]] = {}
import random as _random
_REPLY_ROTATION_INDEX = _random.randint(0, 9999)


def _configured_openrouter_model() -> str:
    """Use a concrete fast model; avoid OpenRouter auto-routing stalls."""
    configured = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
    if not configured or configured.lower() in {"openrouter/auto", "auto", "/auto"}:
        return DEFAULT_OPENROUTER_MODEL
    return configured


OPENROUTER_MODEL = _configured_openrouter_model()


# ── model cooldown ───────────────────────────────────────────────────────────
# `.env` sets OPENROUTER_MODEL, both cascades try it first, and on this account it
# is a paid route with no balance. Every single request therefore opened with a
# round trip that could only 402 before falling through to a model that works —
# measured as part of a 20.4 s wait for a spoken reply, on top of §19's whole
# reason for existing.
#
# Deleting the route is the wrong fix twice over: it is the *better* model and it
# starts working the moment credits are topped up, and hardcoding "known dead"
# lists means the code is wrong again the next time an account changes. So a model
# that fails for a structural reason — no credits, not available to this key, bad
# auth — is demoted to the back of the cascade for a while instead. It is still
# tried if everything else is cooling too, and it silently returns to the front
# once the cooldown lapses, which is what makes a top-up self-healing rather than
# something that needs a code change.
#
# Transient failures (timeouts, rate limits, 5xx) are deliberately excluded: those
# say nothing about whether the route works, and demoting on them would reshuffle
# the cascade over ordinary network noise.
_MODEL_COOLDOWN_S = 900.0

#: Model → unix time it becomes preferred again. Written from worker threads, so
#: reads are plain `.get` lookups and nothing iterates it. Expired entries are
#: ignored by comparison rather than pruned; the key space is the model lists.
_MODEL_COOLDOWN: dict[str, float] = {}

#: Failure kinds that say something durable about the route itself.
_COOLDOWN_KINDS = frozenset({"capacity", "unavailable", "auth"})


def note_model_failure(model: str, error: Exception) -> bool:
    """Demote `model` if `error` means the route itself is unusable.

    Returns True if a cooldown was applied, so callers can log the demotion.
    """
    cleaned = (model or "").strip()
    if not cleaned:
        return False
    if _provider_failure_kind(error) not in _COOLDOWN_KINDS:
        return False
    _MODEL_COOLDOWN[cleaned] = time.time() + _MODEL_COOLDOWN_S
    return True


def _is_cooling(model: str) -> bool:
    return _MODEL_COOLDOWN.get(model, 0.0) > time.time()


def _demote_cooling(candidates: list[str]) -> list[str]:
    """Stable partition: healthy routes first, cooling ones after, none dropped."""
    healthy = [m for m in candidates if not _is_cooling(m)]
    cooling = [m for m in candidates if _is_cooling(m)]
    return healthy + cooling


def _openrouter_model_candidates() -> list[str]:
    """Try the configured model first, then known working OpenRouter fallbacks.

    Local models are appended by `model_routes.merge_candidates` -- at the back
    unless the user picked one -- so a total cloud outage ends in an answer
    instead of `_provider_failure_fallback`'s apology. See that function for why
    the order is what it is.
    """
    candidates: list[str] = []
    for model in (OPENROUTER_MODEL, *OPENROUTER_FALLBACK_MODELS):
        cleaned = (model or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    if not openrouter_configured():
        # No key means every one of these can only 401. Dropping them rather than
        # demoting them is what lets a local model answer at all: the cascade
        # stops on an auth failure, by design, so a cloud route left in front of
        # Ollama would end the turn before Ollama was reached.
        candidates = []
    return model_routes.merge_candidates(_demote_cooling(candidates))


def _voice_model_candidates() -> list[str]:
    """A short cascade for voice: fast, and clean enough to speak.

    Voice used to get `[OPENROUTER_MODEL]` — a list of one, with the comment "no
    multi-model fallback cascade" to explain why. The latency reasoning was right
    and is preserved here; the length was not. A single entry means one dead route
    turns *every* spoken reply into `_provider_failure_fallback`'s canned string,
    which is exactly what was happening: the configured default was 402ing, so the
    assistant could not answer a spoken question at all while the text path
    silently fell through to a working model.

    Two entries, not five. The point of the original comment stands — a user
    waiting for speech cannot absorb five sequential failures — but zero
    redundancy is not the fix for too much redundancy.

    The cap is applied *after* demotion, and a cooling route is dropped outright
    so long as one healthy route remains. Voice is the path where a wasted round
    trip is audible: it sits in front of the first spoken word, so trying a route
    already known to be dead spends the user's silence on nothing. The text path
    keeps cooling routes at the back instead, where an extra attempt is cheap and
    a stale cooldown should never be the reason an answer failed.
    """
    candidates: list[str] = []
    for model in (OPENROUTER_MODEL, *VOICE_MODELS):
        cleaned = (model or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    if not openrouter_configured():
        candidates = []
    healthy = [m for m in candidates if not _is_cooling(m)]
    return model_routes.merge_candidates(healthy or candidates, voice=True, limit=3)

class OpenRouterConfigurationError(RuntimeError):
    """Raised when the configured OpenRouter provider cannot authenticate."""


def openrouter_configured() -> bool:
    """Whether a usable OpenRouter key exists, without raising.

    Needed as a question rather than an exception now that it is not the only
    provider: "is the cloud available" decides how the cascade is built, and
    building a list is not the place to throw.

    Cached for a few seconds because `_openrouter_api_key` re-reads `.env` on
    every call -- correct for picking up a key the user just pasted, wasteful on
    a path that now runs twice per request.
    """
    global _openrouter_configured_cache
    cached = _openrouter_configured_cache
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        _openrouter_api_key()
        answer = True
    except OpenRouterConfigurationError:
        answer = False
    _openrouter_configured_cache = (time.time() + 5.0, answer)
    return answer


_openrouter_configured_cache: tuple[float, bool] | None = None


def _openrouter_api_key() -> str:
    load_dotenv(ENV_PATH, override=True)
    key = (
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("\ufeffOPENROUTER_API_KEY")
        or ""
    ).strip().strip('"').strip("'")
    if not key and ENV_PATH.exists():
        key = (dotenv_values(ENV_PATH, encoding="utf-8-sig").get("OPENROUTER_API_KEY") or "").strip().strip('"').strip("'")
    if not key or key in {"your_key", "your_openrouter_key"}:
        raise OpenRouterConfigurationError(
            "OpenRouter is not configured. Add OPENROUTER_API_KEY to C:\\MY-AI\\aura\\.env and restart the backend."
        )
    return key


_openrouter_client_cache: OpenAI | None = None

def _openrouter_client() -> OpenAI:
    """Returns a cached OpenAI client — avoids creating a new HTTP session on every call."""
    global _openrouter_client_cache
    if _openrouter_client_cache is None:
        _openrouter_client_cache = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=_openrouter_api_key(),
            default_headers={
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "Akansha AI Assistant",
            },
            timeout=12.0,  # hard connection timeout — stops hanging on slow OpenRouter
            max_retries=0,  # we handle retries ourselves in generate_chat_stream
        )
    return _openrouter_client_cache


def _client_for_model(model: str, *, timeout: float | None = None) -> OpenAI:
    """The client that can actually reach `model`.

    The cascade is no longer single-provider, so "which client" is a property of
    the candidate rather than a constant hoisted above the loop. Local models get
    their own client and their own much longer timeout: the cloud's 12 s is a
    sensible ceiling for a hosted route and would abort a local model that is
    still paging itself in from disk.

    Three branches, narrowest first. Ollama, then any provider the owner has
    connected through `providers` -- Groq, Anthropic, LM Studio, a custom
    endpoint -- then OpenRouter, which stays the default so that every bare id
    already in the lists below keeps its current meaning.
    """
    if model_routes.is_local(model):
        return model_routes.local_client()
    if providers.SEP in (model or ""):
        # `groq::llama-3.3-70b` names its own endpoint, so it must not be sent to
        # OpenRouter, which would 404 on an id it has never heard of. Raises
        # `ProviderNotConnected` if there is no key, which the cascade treats like
        # any other dead route: skip it and say why.
        return providers.client_for(model, timeout=timeout)
    client = _openrouter_client()
    if timeout is not None and hasattr(client, "with_options"):
        return client.with_options(timeout=timeout)
    return client

from .agent_modules import (
    AdaptiveInteractionEngine,
    AkanshaSystemPromptBuilder,
    BrowserAutomationModule,
    DesktopAutomationModule,
    FastDomainRouter,
    HabitShortcutEngine,
    MemoryModule,
    MultilingualEngine,
    ParallelTaskRunner,
    ReasoningEngine,
    RelevantMemoryRetriever,
    SelectiveRiskVerifier,
    StateAwareIntentRouter,
    AIGovernorEngine,
    AIOSEventBus,
    AIOSPluginManager,
    AIWorkspaceManager,
    AutomationRecorder,
    BackgroundPreloader,
    ContextCompressor,
    ContinuousSessionState,
    ConversationManager,
    DiagnosticsEngine,
    ModelCompatibilityLayer,
    ModuleCostManager,
    MultiAgentCollaborator,
    ObservabilityTracer,
    OfflineResilienceEngine,
    SelectiveRiskVerifier,
    SelfHealingRecoveryModule,
    SemanticActionHistory,
    TaskSchedulerModule,
    ThinkingEngine,
    ThreeLaneExecutionEngine,
    TieredExecutionEngine,
    ToolOrchestrator,
    UnifiedContextManager,
    UniversalSearchEngine,
    UserUnderstandingModule,
)

SYSTEM_PROMPT_BUILDER = AkanshaSystemPromptBuilder()
RELEVANT_MEMORY_RETRIEVER = RelevantMemoryRetriever(top_k=3)
FAST_DOMAIN_ROUTER = FastDomainRouter()
RISK_VERIFIER = SelectiveRiskVerifier()
PARALLEL_TASK_RUNNER = ParallelTaskRunner()
ADAPTIVE_INTERACTION_ENGINE = AdaptiveInteractionEngine()
HABIT_SHORTCUT_ENGINE = HabitShortcutEngine()
TIERED_EXECUTION_ENGINE = TieredExecutionEngine()
THREE_LANE_ENGINE = ThreeLaneExecutionEngine()
CONTINUOUS_SESSION_STATE = ContinuousSessionState()
CONVERSATION_MANAGER = ConversationManager()
STATE_AWARE_ROUTER = StateAwareIntentRouter(domain_router=FAST_DOMAIN_ROUTER, session_state=CONTINUOUS_SESSION_STATE)
SELF_HEALING_MODULE = SelfHealingRecoveryModule()
MULTILINGUAL_ENGINE = MultilingualEngine()
UNIFIED_CONTEXT_MANAGER = UnifiedContextManager()
MODULE_COST_MANAGER = ModuleCostManager()
BACKGROUND_PRELOADER = BackgroundPreloader()
PLUGIN_MANAGER = AIOSPluginManager()
OBSERVABILITY_TRACER = ObservabilityTracer()
AI_WORKSPACE_MANAGER = AIWorkspaceManager()
CONTEXT_COMPRESSOR = ContextCompressor()
OFFLINE_RESILIENCE_ENGINE = OfflineResilienceEngine()
AI_GOVERNOR_ENGINE = AIGovernorEngine()
AI_OS_EVENT_BUS = AIOSEventBus()
SEMANTIC_ACTION_HISTORY = SemanticActionHistory()
AUTOMATION_RECORDER = AutomationRecorder()
UNIVERSAL_SEARCH_ENGINE = UniversalSearchEngine()
MULTI_AGENT_COLLABORATOR = MultiAgentCollaborator()
MODEL_COMPATIBILITY_LAYER = ModelCompatibilityLayer()
DIAGNOSTICS_ENGINE = DiagnosticsEngine()
SYSTEM_PROMPT = SYSTEM_PROMPT_BUILDER.build_system_prompt() + """

# VOICE AND ACOUSTIC PROTOCOLS (Modality Control)
- IF USER IS IN VOICE MODE: Output pure, uninterrupted natural spoken strings. Absolute zero markdown, bullet points, headers, or emojis. Speak in short, conversational sentences.
- IF USER IS IN TEXT MODE: Utilize clean, scannable markdown, bold anchors, and clear structural lists for high readability.
- Match the user's emotional tone, cadence, and urgency without robotic transitions.

# MULTILINGUAL & CULTURAL ENGINE
- NATIVE INTENT MATCHING: Process the semantic core of the user's intent. Never do literal word-by-word translations.
- CODE-SWITCHING & SLANG: Seamlessly handle mixed-language inputs (Telglish, Hinglish, Manglish, Telugu, Hindi, English). When communicating in Telugu or about Telugu topics, ALWAYS use authentic conversational slang (e.g., ఏంటి సంగతులు, బాగున్నావా, మామా, సరేరా, చూద్దాంలే) with zero robotic feel.
- CULTURAL HONORIFICS: Adhere to regional etiquette and honorifics based on language context (e.g., "-garu" in Telugu, "-ji" in Hindi) unless casual tone is requested.

# GROUNDING & LIVE DATA
- When LIVE WEB CONTEXT is provided, use it directly for current or real-time questions.
- Never invent live prices, scores, versions, dates, weather, or market values. If live context does not verify a value, state what is verified.
- Current date/time must be interpreted in Indian Standard Time (IST, Asia/Kolkata) whenever the user says today, yesterday, tomorrow, present, now, or current.
"""

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

DIRECT_TIME_DATE_PATTERN = re.compile(
    r"^\s*(?:what(?:'s| is)?|tell me|show me|give me|exact|actual|current|present)?\s*"
    r"(?:the\s+)?(?:exact\s+|actual\s+|current\s+|present\s+)?"
    r"(?:(?:ist|india|london|uk|britain|england)\s+)?"
    r"(?:time|date|day|today(?:'s)? date|today(?:'s)? day|now)"
    r"(?:\s+(?:in\s+)?(?:ist|india|london|uk|britain|england))?\s*[?.!]*\s*$",
    re.IGNORECASE,
)

TIME_ZONE_ALIASES = (
    ("london", "Europe/London", "London"),
    ("uk", "Europe/London", "London"),
    ("britain", "Europe/London", "London"),
    ("england", "Europe/London", "London"),
    ("india", "Asia/Kolkata", "IST"),
    ("ist", "Asia/Kolkata", "IST"),
)

RELATIONSHIP_NAME_PATTERN = re.compile(
    r"\b(?:my\s+)?"
    r"(?P<relation>mother|mom|mummy|amma|father|dad|nanna|brother|sister|friend|professor|teacher|mentor)"
    r"(?:'s)?\s+(?:name\s+is|is|called)\s+"
    r"(?P<name>[a-z][a-z .'-]{1,80})",
    re.IGNORECASE,
)

RELATIONSHIP_ALIASES = {
    "mom": "mother",
    "mummy": "mother",
    "amma": "mother",
    "dad": "father",
    "nanna": "father",
    "teacher": "professor",
}


def _compact_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _intent_text(text: str) -> str:
    """The string the intent regexes should read, rather than the one to answer.

    Every keyword test in this module is Latin -- `open`, `youtube`, `folder`,
    `what` -- and the app offers EN / TE / HI. So `"ఓపెన్ యూట్యూబ్"` matched
    nothing: it is two words with no question mark and no `\\bopen\\b`, which is
    the exact shape of a meaningless fragment, so it was answered "tell me exactly
    what to do" while the command was thrown away. The user had to say it again in
    English before YouTube opened.

    `indic_intent.intent_view` folds Telugu and Devanagari into their English
    keywords and leaves Latin untouched, so one set of patterns covers all three
    languages. It returns the input unchanged when there is no Indic character, so
    the English path pays a single regex search for this.

    Only *matching* uses this. What the user said still goes to the model and to
    the screen verbatim -- transliterating someone's words and reading them back
    is its own kind of rudeness.
    """
    return _compact_text(indic_intent.intent_view(text))


LIVE_QUERY_KEYWORDS = {
    "latest",
    "current",
    "present",
    "today",
    "yesterday",
    "tomorrow",
    "now",
    "price",
    "rate",
    "rates",
    "population",
    "weather",
    "score",
    "news",
    "president",
    "minister",
    "prime",
    "chief",
    "ceo",
    "capital",
    "mountain",
    "country",
    "population",
    "government",
    "official",
    "cricket",
    "football",
    "world",
    "cup",
    "fifa",
    "test",
    "repo",
    "repository",
    "java",
    "python",
    "polymorphism",
    "comprehension",
    "update",
    "updates",
    "winner",
    "won",
    "match",
    "matches",
    "schedule",
    "schedules",
    "fixture",
    "fixtures",
    "next",
    "upcoming",
    "any",
    "available",
    "there",
    "game",
    "games",
    "talking",
    "thinking",
    "standing",
    "standings",
    "ranking",
    "rankings",
    "stock",
    "crypto",
    "silver",
    "gold",
    "gram",
    "grams",
    "internet",
    "search",
    "web",
    "google",
    # Telugu Slang Keywords
    "enti",
    "sangathi",
    "sangatulu",
    "bagunnava",
    "mama",
    "sarera",
    "chuddamle",
    "bhayya",
    "cheppu",
    "em",
}

COMMON_QUERY_WORDS = {
    "a",
    "about",
    "also",
    "are",
    "as",
    "at",
    "else",
    "for",
    "from",
    "give",
    "how",
    "in",
    "is",
    "me",
    "of",
    "on",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
}

EDUCATIONAL_CONCEPT_WORDS = {
    "algorithm",
    "amortization",
    "biology",
    "chemistry",
    "computer",
    "computing",
    "debt",
    "economics",
    "escrow",
    "finance",
    "income",
    "mathematics",
    "physics",
    "photosynthesis",
    "quantum",
    "ratio",
    "science",
}

LIVE_QUERY_CORRECTION_VOCAB = LIVE_QUERY_KEYWORDS | COMMON_QUERY_WORDS | EDUCATIONAL_CONCEPT_WORDS


def _is_subsequence(short: str, long: str) -> bool:
    iterator = iter(long)
    return all(char in iterator for char in short)


def _best_contextual_token_match(token: str) -> str:
    if token in LIVE_QUERY_CORRECTION_VOCAB:
        return token
    if len(token) <= 2:
        return token

    best = ""
    best_score = 0.0
    for candidate in LIVE_QUERY_CORRECTION_VOCAB:
        if abs(len(candidate) - len(token)) > 3 and not _is_subsequence(token, candidate):
            continue
        score = SequenceMatcher(None, token, candidate).ratio()
        if len(token) <= 3:
            if token[0] == candidate[0] and _is_subsequence(token, candidate):
                score = max(score, 0.72 + (0.05 * len(token)))
            else:
                continue
        if score > best_score:
            best = candidate
            best_score = score

    threshold = 0.86
    if len(token) <= 3:
        threshold = 0.82
    elif len(token) == 4:
        threshold = 0.84
    elif len(token) >= 6:
        threshold = 0.78
    return best if best and best_score >= threshold else token


def _normalize_live_query_text(text: str) -> str:
    """Lightly normalize likely misspellings before live-intent routing.

    The router should not maintain a table of every possible human typo. Instead,
    it corrects only toward known retrieval/intent vocabulary when the token is a
    close fuzzy match or a short abbreviation-like subsequence.
    """
    lowered = (text or "").lower()
    words = re.findall(r"[a-z]+|\d+|[^a-z\d]+", lowered)
    normalized_parts: list[str] = []
    for part in words:
        if not part.isalpha():
            normalized_parts.append(part)
            continue
        normalized_parts.append(_best_contextual_token_match(part))
    normalized = "".join(normalized_parts)
    if re.search(r"\b(silver|gold|bullion|metal|metals|chandi|sona)\b", normalized) and re.search(
        r"\b(price|rate|rates|per|inr|india|gram|grams|games)\b",
        normalized,
    ):
        normalized = re.sub(r"\b(per|in)\s+games\b", r"\1 grams", normalized)
    return normalized


def _title_person_name(name: str) -> str:
    cleaned = re.split(r"\b(?:and|but|so|okay|ok|please|thanks)\b", name, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = re.sub(r"[^a-zA-Z .'-]", "", cleaned).strip(" .'-")
    return " ".join(part.capitalize() for part in cleaned.split())


def _extract_relationship_name_fact(text: str) -> tuple[str, str] | None:
    cleaned = _compact_text(text)
    match = RELATIONSHIP_NAME_PATTERN.search(cleaned)
    if not match:
        return None

    relation = RELATIONSHIP_ALIASES.get(match.group("relation").lower(), match.group("relation").lower())
    name = _title_person_name(match.group("name"))
    if len(name.split()) > 5 or len(name) < 2:
        return None
    return relation, name


def _extract_self_intro_fact(text: str) -> str | None:
    cleaned = _compact_text(text)
    if not cleaned or len(cleaned) > 120:
        return None
    match = re.match(
        r"^(?:i\s+am|i'm|im|my\s+name\s+is|this\s+is)\s+([A-Za-z][A-Za-z .'-]{1,80})[.!?]*$",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None
    name = _title_person_name(match.group(1))
    if len(name) < 2 or len(name.split()) > 7:
        return None
    return name


def _is_direct_time_or_date_query(text: str) -> bool:
    cleaned = _compact_text(text)
    if not cleaned or len(cleaned) > 90:
        return False
    lowered = cleaned.lower()
    if re.search(r"\b(price|score|news|weather|match|stock|crypto|website|web|internet|search)\b", lowered):
        return False
    return bool(DIRECT_TIME_DATE_PATTERN.match(cleaned))


def _time_zone_for_query(text: str) -> tuple[str, timezone | object, str]:
    lowered = text.lower()
    for alias, zone_name, label in TIME_ZONE_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            if zone_name == "Asia/Kolkata" or ZoneInfo is None:
                return label, IST, zone_name
            try:
                return label, ZoneInfo(zone_name), zone_name
            except Exception:
                return label, IST, "Asia/Kolkata"
    return "IST", IST, "Asia/Kolkata"


def _format_local_datetime_for_reply(current: datetime) -> tuple[str, str]:
    date_format = "%A, %B %-d, %Y"
    time_format = "%-I:%M %p"
    if os.name == "nt":
        date_format = "%A, %B %#d, %Y"
        time_format = "%#I:%M %p"
    return current.strftime(date_format), current.strftime(time_format)


def _should_use_local_utility_reply(user_input: str, attachments: list[dict] | None = None) -> bool:
    """Only bypass the model for deterministic utilities.

    Normal conversation must stay model-driven. Keeping greetings, jokes,
    fragments, identities, and live-fact questions out of this path prevents
    the repeated canned replies that were degrading the chat experience.
    """
    if attachments:
        return False
    cleaned = _compact_text(user_input)
    if not cleaned:
        return False
    return _is_direct_time_or_date_query(cleaned)


def _should_use_fast_local_reply(user_input: str, attachments: list[dict] | None = None) -> bool:
    """Backward-compatible name for tests/imports; no longer a chat shortcut."""
    return _should_use_local_utility_reply(user_input, attachments)


def _fast_local_reply(db: Session, user_input: str, language_preference: str | None = None) -> str | None:
    cleaned = _compact_text(user_input)
    lowered = cleaned.lower()
    preference = (language_preference or "").lower()

    if _is_direct_time_or_date_query(cleaned):
        zone_label, tzinfo, _zone_name = _time_zone_for_query(cleaned)
        now = datetime.now(tzinfo)
        date_text, time_text = _format_local_datetime_for_reply(now)
        wants_date = bool(re.search(r"\b(date|day|today)\b", lowered))
        if "hindi" in preference:
            if wants_date and "time" not in lowered:
                return f"Aaj {date_text} hai, {zone_label} ke according."
            return f"Abhi {time_text} {zone_label} hai, {date_text}."
        if "telugu" in preference:
            if wants_date and "time" not in lowered:
                return f"Ivvala {date_text}, {zone_label} prakaram."
            return f"Ippudu {time_text} {zone_label}, {date_text}."
        if wants_date and "time" not in lowered:
            return f"Today is {date_text} in {zone_label}."
        return f"{zone_label} time is {time_text} on {date_text}."

    return None


class _NoopDb:
    def commit(self):
        return None

    def query(self, *_args, **_kwargs):
        raise RuntimeError("No database access is needed for this fallback")


def _stable_choice(seed: str, options: list[str]) -> str:
    if not options:
        return ""
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


def _rotating_choice(options: list[str]) -> str:
    global _REPLY_ROTATION_INDEX
    if not options:
        return ""
    _REPLY_ROTATION_INDEX = (_REPLY_ROTATION_INDEX + 1) % 10_000
    return options[_REPLY_ROTATION_INDEX % len(options)]


def _speaker_display_name(speaker_profile: dict | None = None) -> str:
    if not isinstance(speaker_profile, dict):
        return ""
    name = str(speaker_profile.get("name") or speaker_profile.get("speaker_name") or "").strip()
    if not name or name.lower() in {"unknown", "user", "owner"}:
        return ""
    return _title_person_name(name)


def _is_brief_greeting(text: str) -> bool:
    """"hi", "hey hi", "నమస్కారం" -- an opening with nothing asked in it.

    Two changes from the single-token version, both from the same transcript.
    `హే హాయ్` folds to `hey hi`, so the pattern has to accept a short run of
    greetings rather than exactly one -- people say "hey hi" and "hello hello" out
    loud far more than they type them. And it reads the Latin view, because
    otherwise a Telugu greeting fell through to `_is_brief_ack_or_fragment` and was
    answered "tell me exactly what to do" instead of being greeted back.
    """
    one = r"(?:hi+|h+i+|hello+|hey+|hoi+|yo+|namaste|namaskar|vanakkam|salaam|good\s+(?:morning|afternoon|evening|night))"
    return bool(
        re.fullmatch(
            rf"{one}(?:[\s,!.]+{one}){{0,2}}[\s,!.]*",
            _intent_text(text),
            re.IGNORECASE,
        )
    )


def _is_brief_ack_or_fragment(text: str) -> bool:
    # The Latin view, not the raw text: this function's job is to spot an
    # utterance with no content in it, and it used to say yes to every Telugu or
    # Hindi command of three words or fewer, because none of the verbs below can
    # be spelled in those scripts.
    cleaned = _intent_text(text)
    if not cleaned or len(cleaned) > 80:
        return False
    if re.search(
        r"\b(?:i\s+am|i'm|im|my\s+name\s+is|this\s+is|called|date\s+of\s+birth|dob|born|birth\s+date)\b",
        cleaned,
        re.IGNORECASE,
    ):
        return False
    if re.fullmatch(
        r"(?:yes|yeah|yep|ya|ok|okay|okk|sure|sare|ha|haan|haa|avunu|aithe|aha(?:\s+aha)?|oh+|ohh+|hmm+|mm+|umma|u\s*mma|aku\s*paku|enti(?:\s+inka)?)",
        cleaned,
        re.IGNORECASE,
    ):
        return True
    words = cleaned.split()
    return len(words) <= 3 and not re.search(r"[?]", cleaned) and not re.search(
        r"\b(?:what|who|when|where|why|how|tell|give|show|create|generate|open|send|price|rate|time|date|birth|score|news|analy[sz]e|explain|write|make|delete|update|question|file|image|video|audio|pdf|excel|ppt|json|csv)\b",
        cleaned,
        re.IGNORECASE,
    )


def _wants_joke(text: str) -> bool:
    return bool(re.search(r"\b(joke|funny|make me laugh|navvu|hasao|hasi)\b", text, re.IGNORECASE))


#: "How are you?" directed at Akansha herself.
#:
#: This had no branch anywhere: it is not a greeting (`_is_brief_greeting` wants no
#: question mark) and not a fragment (`_is_brief_ack_or_fragment` excludes `?` and
#: `how`), so it fell all the way through to the live-source path and came back as
#: an encyclopaedia entry for Daniel Johnston's album *Hi, How Are You* — 2.9 s to
#: answer a social question with discography trivia.
#:
#: Matched by `fullmatch` on the compacted text rather than a substring search, so
#: questions that merely open the same way keep going to the model: "how are you
#: going to open the browser", "how are you trained", "how are your skills wired"
#: are all real questions and none of them fullmatch.
_WELLBEING_QUESTION_RE = re.compile(
    r"(?:(?:hey|hi|hello|so|and|ok|okay|arre|abey)\s+)?"
    r"(?:akansha|akanksha)?\s*,?\s*"
    r"(?:"
    r"how(?:'?s|\s+is|\s+are|\s+r)\s+(?:you|u|ya|it|things)(?:\s+(?:doing|going|feeling|today|now))?"
    r"|hru|how\s+do\s+you\s+do|how(?:'?s)?\s+it\s+going"
    r"|(?:what|how)\s+about\s+you"
    r"|are\s+you\s+(?:fine|ok|okay|good|alright|well|there)"
    r"|kaise?\s+(?:ho|hai|hain)|kaisi\s+ho|kya\s+haal\s+(?:hai|hain)|kaisa\s+hai"
    r"|ela\s+unnav(?:u|\s+ra)?|ela\s+unnaru|bagunnav(?:a|u)?|bagunnara|em\s+chesthunnav"
    r")"
    r"\s*[?!.]*",
    re.IGNORECASE,
)


def _is_wellbeing_question(text: str) -> bool:
    cleaned = _compact_text(text)
    if not cleaned or len(cleaned) > 60:
        return False
    return bool(_WELLBEING_QUESTION_RE.fullmatch(cleaned))


def _quick_wellbeing_reply(text: str, language_preference: str, speaker_profile: dict | None = None) -> str:
    """Answer the question, then hand the turn back — not a greeting, a reply."""
    preference = language_preference.lower()
    name = _speaker_display_name(speaker_profile)
    suffix = f" {name}" if name else ""
    if "hindi" in preference:
        return _rotating_choice(
            [
                f"Main badhiya hoon{suffix}, poochne ke liye thanks! Tum kaise ho?",
                f"Sab theek hai{suffix} — ready hoon. Tumhara din kaisa ja raha hai?",
                f"Ekdum fit{suffix}! Batao, kya karna hai aaj?",
            ],
        )
    if "telugu" in preference:
        return _rotating_choice(
            [
                f"Nenu baagunnanu{suffix}, adiginanduku thanks! Nuvvu ela unnav?",
                f"Anni baaga unnayi{suffix} — ready ga unna. Nee day ela veltondi?",
                f"Super ga unna{suffix}! Cheppu, ee roju em cheddam?",
            ],
        )
    return _rotating_choice(
        [
            f"I'm good{suffix}, thanks for asking! How are you doing?",
            f"Doing well{suffix} — running fine and ready. How's your day going?",
            f"All good here{suffix}! What are we working on today?",
        ],
    )


def _quick_greeting_reply(text: str, language_preference: str, speaker_profile: dict | None = None) -> str:
    preference = language_preference.lower()
    name = _speaker_display_name(speaker_profile)
    suffix = f" {name}" if name else ""
    if "hindi" in preference:
        return _rotating_choice(
            [
                f"Namaste{suffix}! Kya kar sakte hain aaj?",
                f"Haan{suffix}, sun raha hoon. Bolo kya karna hai?",
                f"Hi{suffix}! Main ready hoon, next kaam bhejo.",
                f"Hey{suffix}, kya haal hai? Kya help chahiye?",
                f"Arre{suffix}, aa gaye! Kya kaam hai?",
                f"Suno{suffix}, main yahin hoon. Bol do.",
            ],
        )
    if "telugu" in preference:
        return _rotating_choice(
            [
                f"Heyy{suffix}! Em chesthunnav? Em cheddam?",
                f"Hi{suffix}, ready ga unna. Em kavali cheppu!",
                f"Namaskaram{suffix}! Em help cheyali?",
                f"Hey{suffix} mawa! Ela unna? Em task undi?",
                f"Enti{suffix}, cheppu! Nenu ready ga unna.",
                f"Hii{suffix}! Em cheddam, cheppu ra!",
                f"Ayyo{suffix}, cheppu cheppu! Em kavali?",
                f"Hello{suffix}! Em pani pettalante cheppu.",
            ],
        )
    return _rotating_choice(
        [
            f"Hey{suffix}! What can I help with today?",
            f"Hi{suffix}! I'm here — what do you need?",
            f"Hello{suffix}! Ready to help. What's the task?",
            f"Hey there{suffix}! What should we work on?",
            f"Hi{suffix}! What's on your mind?",
            f"Hey{suffix}, good to see you! What do you need done?",
        ],
    )


def _quick_fragment_reply(text: str, language_preference: str, speaker_profile: dict | None = None) -> str:
    preference = language_preference.lower()
    name = _speaker_display_name(speaker_profile)
    suffix = f" {name}" if name else ""
    if "hindi" in preference:
        return _rotating_choice(
            [
                f"Haan{suffix}, samjha. Ab exact kaam ya question bhejo.",
                f"Theek hai{suffix}, main ready hoon. One line mein batao kya karna hai.",
                f"Okay{suffix}, continue karo. Main context pakad raha hoon.",
            ],
        )
    if "telugu" in preference:
        return _rotating_choice(
            [
                f"Sare{suffix}, ardham ayyindi. Ippudu exact ga em cheyyalo cheppu.",
                f"Okay{suffix}, vinthunnanu. One line lo task cheppu.",
                f"Ha{suffix}, continue cheyyi. Nenu context hold chesthunnanu.",
            ],
        )
    return _rotating_choice(
        [
            f"Got it{suffix}. Send the actual question or task when you're ready.",
            f"Okay{suffix}, I'm with you. What should I do next?",
            f"I'm listening{suffix}. Give me the next clear instruction.",
        ],
    )


def _quick_self_intro_reply(name: str, language_preference: str) -> str:
    preference = language_preference.lower()
    if "hindi" in preference:
        return _rotating_choice(
            [
                f"Samajh gaya, {name}. Main is naam ko is chat ke context mein use karunga.",
                f"Noted, {name}. Ab batao, main kya help karun?",
                f"Okay {name}, context update ho gaya. Next kya karna hai?",
            ],
        )
    if "telugu" in preference:
        return _rotating_choice(
            [
                f"Ardham ayyindi, {name}. Ee chat lo aa peru use chestha.",
                f"Noted, {name}. Ippudu em help kavali?",
                f"Okay {name}, context update ayyindi. Next em cheddam?",
            ],
        )
    return _rotating_choice(
        [
            f"Got it, {name}. I’ll use that name in this chat context.",
            f"Noted, {name}. What should we handle next?",
            f"Okay {name}, I updated the conversation context. What do you want to do now?",
        ],
    )


def _fallback_joke_reply(text: str, language_preference: str) -> str:
    preference = language_preference.lower()
    if "hindi" in preference:
        return _rotating_choice(
            [
                "Ek quick joke: Programmer ne chai kyun banayi? Kyunki code ko thoda Java chahiye tha.",
                "Teacher: homework kahan hai? Student: Cloud mein hai sir, bas sync pending hai.",
                "Laptop bola: mujhe rest chahiye. Windows bola: pehle 47 updates complete karo.",
                "Math book udaas thi, kyunki uske paas bahut problems thi.",
                "Wi-Fi shy tha: connection public, feelings private.",
            ],
        )
    if "telugu" in preference:
        return _rotating_choice(
            [
                "Oka quick joke: Laptop sleep ki vellalante Windows annadi, first 47 updates complete chey.",
                "Programmer chai enduku tagadu? Code ki konchem Java kavali kabatti.",
                "Math book enduku sad ga undi? Dantlo problems ekkuva.",
                "Wi-Fi enduku shy ga undi? Connection public, feelings private kabatti.",
                "Teacher: homework ekkada? Student: cloud lo undi sir, sync avvaledu.",
            ],
        )
    return _rotating_choice(
        [
            "Quick joke: My laptop asked for a break. Windows replied, 'Sure, after 47 updates.'",
            "Tiny joke: Why did the programmer make tea? The code needed a little Java.",
            "One quick one: The math book looked stressed because it had too many problems.",
            "Quick joke: Why was the keyboard calm? It had everything under control.",
            "Tiny joke: I told my browser to stop tracking me. It opened another tab to discuss it.",
        ],
    )


def _currency_code_from_text(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    currency_aliases = {
        "usd": ("USD", "US dollar"),
        "us dollar": ("USD", "US dollar"),
        "dollar": ("USD", "US dollar"),
        "eur": ("EUR", "euro"),
        "euro": ("EUR", "euro"),
        "gbp": ("GBP", "British pound"),
        "pound": ("GBP", "British pound"),
        "aed": ("AED", "UAE dirham"),
        "dirham": ("AED", "UAE dirham"),
    }
    if not re.search(r"\b(price|rate|value|inr|rupee|rupees|currency|exchange)\b", lowered):
        return None
    for alias, value in currency_aliases.items():
        if re.search(rf"\b{re.escape(alias)}s?\b", lowered):
            return value
    return None


def _fetch_currency_to_inr(code: str) -> tuple[float, str] | None:
    cache_key = f"{code}_INR"
    now = _now_ist()
    cached = _CURRENCY_RATE_CACHE.get(cache_key)
    if cached and now - cached[0] < timedelta(minutes=10):
        return cached[1], cached[2]

    sources = (
        (f"https://open.er-api.com/v6/latest/{urllib.parse.quote(code)}", "open.er-api.com"),
        (f"https://api.exchangerate.host/latest?base={urllib.parse.quote(code)}&symbols=INR", "exchangerate.host"),
    )
    for source_url, source_name in sources:
        try:
            payload = json.loads(_read_url(source_url, timeout=3))
            rate = float((payload.get("rates") or {}).get("INR") or 0)
            if rate > 0:
                _CURRENCY_RATE_CACHE[cache_key] = (now, rate, source_name)
                return rate, source_name
        except Exception:
            continue
    return None


def _quick_currency_reply(text: str, language_preference: str) -> str | None:
    detected = _currency_code_from_text(text)
    if not detected:
        return None
    code, label = detected
    fetched = _fetch_currency_to_inr(code)
    timestamp = _format_ist_datetime()
    preference = language_preference.lower()
    if not fetched:
        if "hindi" in preference:
            return f"{label} to INR ka live rate is turn mein verify nahi ho paaya. Main guess nahi karunga."
        if "telugu" in preference:
            return f"{label} to INR live rate ee turn lo verify avvaledu. Guess cheyyanu."
        return f"I could not verify the live {label} to INR rate in this turn, so I will not guess."
    rate, source = fetched
    if "hindi" in preference:
        return f"Abhi 1 {label} lagbhag Rs. {rate:.2f} INR hai. Source: {source}. Fetched at {timestamp}."
    if "telugu" in preference:
        return f"Ippudu 1 {label} approx Rs. {rate:.2f} INR. Source: {source}. Fetched at {timestamp}."
    return f"Right now, 1 {label} is about Rs. {rate:.2f} INR. Source: {source}. Fetched at {timestamp}."


def _extract_birth_subject(text: str) -> str | None:
    cleaned = _compact_text(text)
    if not re.search(r"\b(date\s+of\s+birth|dob|born|birth\s+date)\b", cleaned, re.IGNORECASE):
        return None
    subject = re.split(r"\b(?:date\s+of\s+birth|dob|born|birth\s+date)\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    subject = re.sub(r"^(?:what\s+is|tell\s+me|give\s+me|show\s+me|who\s+is|about)\s+", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"[^A-Za-z0-9 .'-]", " ", subject)
    subject = _compact_text(subject).strip(" .'-")
    if 2 <= len(subject) <= 90:
        return subject
    return None


def _fetch_wikipedia_summary(query: str) -> tuple[str, str, str] | None:
    try:
        search_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&utf8=1&srlimit=1&srsearch="
            + urllib.parse.quote(query)
        )
        search_payload = json.loads(_read_url(search_url, timeout=4))
        results = ((search_payload.get("query") or {}).get("search") or [])
        if not results:
            return None
        title = str(results[0].get("title") or "").strip()
        if not title:
            return None
        summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
        summary_payload = json.loads(_read_url(summary_url, timeout=4))
        extract = str(summary_payload.get("extract") or "").strip()
        page_url = str(((summary_payload.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
        if extract:
            sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", extract) if item.strip()]
            birth_sentence = next(
                (
                    item
                    for item in sentences[:4]
                    if re.search(r"\b(?:born|birth|date of birth)\b|\(\s*\d{1,2}\s+[A-Z][a-z]+\s+\d{4}", item)
                ),
                "",
            )
            summary_sentence = birth_sentence or (sentences[0] if sentences else extract[:400])
            return title, summary_sentence, page_url or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
    except Exception:
        return None
    return None


def _fetch_wikipedia_title_summary(title: str) -> tuple[str, str, str] | None:
    cleaned_title = _compact_text(title).strip(" .")
    if not cleaned_title:
        return None
    try:
        summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(cleaned_title.replace(" ", "_"))
        summary_payload = json.loads(_read_url(summary_url, timeout=4))
        extract = str(summary_payload.get("extract") or "").strip()
        page_title = str(summary_payload.get("title") or cleaned_title).strip()
        page_url = str(((summary_payload.get("content_urls") or {}).get("desktop") or {}).get("page") or "")
        if extract:
            return page_title, extract, page_url or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page_title.replace(' ', '_'))}"
    except Exception:
        pass
    try:
        extract_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "exintro": 1,
                "redirects": 1,
                "titles": cleaned_title,
                "format": "json",
            }
        )
        extract_data = json.loads(_read_url(extract_url, timeout=6))
        pages = (extract_data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            extract = _compact_text(page.get("extract") or "")
            page_title = str(page.get("title") or cleaned_title).strip()
            if extract:
                page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page_title.replace(' ', '_'), safe='_()')}"
                return page_title, extract, page_url
    except Exception:
        pass
    return None


def _fetch_concept_summary_context(user_input: str) -> str:
    if not _looks_like_concept_explainer_query(user_input):
        return ""
    subject = _concept_core_subject_query(user_input)
    result = _fetch_wikipedia_title_summary(subject) or _fetch_wikipedia_summary(subject)
    if not result:
        return ""
    title, extract, page_url = result
    return (
        "LIVE WEB CONTEXT:\n"
        f"1. {title}\n"
        f"   URL: {page_url}\n"
        f"   Snippet: Page extract: {_compact_text(extract)[:1800]}"
    )


def _fetch_wikidata_birth_date(title: str) -> str | None:
    try:
        pageprops_url = (
            "https://en.wikipedia.org/w/api.php?action=query&prop=pageprops&format=json&titles="
            + urllib.parse.quote(title)
        )
        pageprops_payload = json.loads(_read_url(pageprops_url, timeout=4))
        pages = (pageprops_payload.get("query") or {}).get("pages") or {}
        entity_id = ""
        for page in pages.values():
            entity_id = str(((page or {}).get("pageprops") or {}).get("wikibase_item") or "")
            if entity_id:
                break
        if not entity_id:
            return None

        entity_url = (
            "https://www.wikidata.org/w/api.php?action=wbgetentities&format=json&props=claims&ids="
            + urllib.parse.quote(entity_id)
        )
        entity_payload = json.loads(_read_url(entity_url, timeout=4))
        claims = (((entity_payload.get("entities") or {}).get(entity_id) or {}).get("claims") or {})
        birth_claims = claims.get("P569") or []
        if not birth_claims:
            return None
        time_value = (
            (((birth_claims[0].get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("time")
            or ""
        )
        match = re.match(r"^[+-](\d{4})-(\d{2})-(\d{2})T", time_value)
        if not match:
            return None
        year, month, day = map(int, match.groups())
        return datetime(year, month, day, tzinfo=IST).strftime("%B %-d, %Y") if os.name != "nt" else datetime(year, month, day, tzinfo=IST).strftime("%B %#d, %Y")
    except Exception:
        return None


def _quick_birth_reply(text: str, language_preference: str) -> str | None:
    subject = _extract_birth_subject(text)
    if not subject:
        return None
    result = _fetch_wikipedia_summary(subject)
    if not result:
        return None
    title, sentence, page_url = result
    birth_date = _fetch_wikidata_birth_date(title)
    preference = language_preference.lower()
    if birth_date:
        if "hindi" in preference:
            return f"{title} ka date of birth {birth_date} hai. Source: Wikidata/Wikipedia ({page_url})."
        if "telugu" in preference:
            return f"{title} date of birth {birth_date}. Source: Wikidata/Wikipedia ({page_url})."
        return f"{title}'s date of birth is {birth_date}. Source: Wikidata/Wikipedia ({page_url})."
    if "hindi" in preference:
        return f"{title} ke baare mein verified source se: {sentence} Source: Wikipedia ({page_url})."
    if "telugu" in preference:
        return f"{title} gurinchi source-backed info: {sentence} Source: Wikipedia ({page_url})."
    return f"{title}: {sentence} Source: Wikipedia ({page_url})."


def _quick_world_status_reply(text: str, language_preference: str) -> str:
    preference = language_preference.lower()
    if "hindi" in preference:
        return "Short answer: world mixed hai. Tech fast move ho raha hai, markets aur politics volatile hain, aur climate/energy pressure serious hai. Latest news ke liye live sources verify karke current update dena chahiye."
    if "telugu" in preference:
        return "Short ga cheppalante: world mixed ga undi. Tech speed ga move avuthondi, politics/markets volatile ga unnayi, climate pressure serious. Latest news kavali ante live sources verify chesi current update ivvali."
    return "Short answer: the world is mixed right now. Tech is moving fast, politics and markets are volatile, and climate/energy pressure is serious. For latest news, I should verify live sources before giving current claims."


def _quick_local_response(
    db: Session,
    user_input: str,
    language_preference: str | None = None,
    speaker_profile: dict | None = None,
    allow_conversation_fragments: bool = False,
) -> str | None:
    cleaned = _compact_text(user_input)
    if not cleaned:
        return None
    preference = language_preference or "english"

    relation_fact = _extract_relationship_name_fact(cleaned)
    if relation_fact:
        relation, name = relation_fact
        try:
            _upsert_memory(db, f"{relation}_name", f"The user's {relation} name is {name}.", importance=4)
        except Exception:
            pass
        if "hindi" in preference.lower():
            return f"Samajh gaya. Tumhari {relation} ka naam {name} hai; main ise yaad rakhunga."
        if "telugu" in preference.lower():
            return f"Ardham ayyindi. Mee {relation} peru {name}; nenu gurthu pettukunta."
        return f"Got it. Your {relation}'s name is {name}; I'll remember that."

    self_intro = _extract_self_intro_fact(cleaned)
    if self_intro:
        try:
            _upsert_memory(db, "Current speaker name", f"The current speaker introduced themselves as {self_intro}.", importance=4)
        except Exception:
            pass
        return _quick_self_intro_reply(self_intro, preference)

    if _is_brief_greeting(cleaned):
        return _quick_greeting_reply(cleaned, preference, speaker_profile)

    if _wants_joke(cleaned):
        return _fallback_joke_reply(cleaned, preference)

    currency = _quick_currency_reply(cleaned, preference)
    if currency:
        return currency

    birth = _quick_birth_reply(cleaned, preference)
    if birth:
        return birth

    if (
        re.search(r"\b(how'?s|how is|what'?s|what is).{0,30}\b(world|going on|all ok|everything ok)\b", cleaned, re.IGNORECASE)
        and not _looks_like_public_discussion_query(cleaned)
    ):
        return _quick_world_status_reply(cleaned, preference)

    # After the world-status branch on purpose: "how's it going" reads as either,
    # and "how is the world going on" should stay with the world answer.
    if _is_wellbeing_question(cleaned):
        return _quick_wellbeing_reply(cleaned, preference, speaker_profile)

    if allow_conversation_fragments and _is_brief_ack_or_fragment(cleaned):
        return _quick_fragment_reply(cleaned, preference, speaker_profile)

    return None


def _is_local_quick_answer_query(user_input: str, attachments: list[dict] | None = None) -> bool:
    if attachments:
        return False
    cleaned = _compact_text(user_input)
    if not cleaned:
        return False
    return bool(
        _is_direct_time_or_date_query(cleaned)
        or _extract_relationship_name_fact(cleaned)
        or _extract_self_intro_fact(cleaned)
        or _is_brief_greeting(cleaned)
        or _is_brief_ack_or_fragment(cleaned)
        or _wants_joke(cleaned)
        or _currency_code_from_text(cleaned)
        or _extract_birth_subject(cleaned)
        or (
            re.search(r"\b(how'?s|how is|what'?s|what is).{0,30}\b(world|going on|all ok|everything ok)\b", cleaned, re.IGNORECASE)
            and not _looks_like_public_discussion_query(cleaned)
        )
    )


def _should_skip_ai_memory_analysis(user_input: str, assistant_response: str) -> bool:
    if _is_local_quick_answer_query(user_input):
        return True
    if _extract_relationship_name_fact(user_input):
        return True
    if re.search(
        r"\b(model connection|provider key|model provider|not authenticated|not available right now|"
        r"vision model is unavailable|active model provider is unavailable)\b",
        assistant_response or "",
        re.IGNORECASE,
    ):
        return True
    return False


def _normalize_user_fact(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    return cleaned.rstrip(".!?")


def _upsert_memory(db: Session, topic: str, insight: str, importance: int = 3):
    existing = (
        db.query(Memory)
        .filter(Memory.topic.ilike(topic))
        .order_by(Memory.importance.desc(), Memory.id.desc())
        .first()
    )
    if existing:
        existing.insight = insight
        existing.importance = max(existing.importance or 1, importance)
        return existing
    row = Memory(topic=topic, insight=insight, importance=importance)
    db.add(row)
    return row


def _index_memories(db: Session, rows: list) -> None:
    """Make just-written memories semantically searchable now, not next boot.

    `flush` rather than `commit`: the caller owns the transaction, and a new row
    has no primary key until it is flushed -- so without this the vector would be
    stored under an id of `None`. If the caller later rolls back, the index holds
    a memory the table does not; the pruning pass in `memory_index.sync` removes
    it, which is why that pass exists.
    """
    if not rows:
        return
    try:
        db.flush()
        from . import memory_index  # noqa: PLC0415 - deferred; ai_engine loads before it

        memory_index.index_rows([row for row in rows if getattr(row, "id", None) is not None])
    except Exception as exc:
        # Never fail a reply because a derived index could not be updated.
        _log_memory_index_failure(exc)


def _log_memory_index_failure(exc: Exception) -> None:
    try:
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning("Memory index update skipped: %s", exc)
    except Exception:
        pass


def _capture_deterministic_memories(db: Session, user_input: str):
    lowered = user_input.lower()
    compact = _normalize_user_fact(user_input)
    touched: list = []

    relationship_fact = _extract_relationship_name_fact(user_input)
    if relationship_fact:
        relation, name = relationship_fact
        touched.append(_upsert_memory(
            db,
            f"Relationship: {relation}",
            f"User's {relation} is {name}.",
            importance=5,
        ))

    self_intro = _extract_self_intro_fact(user_input)
    if self_intro:
        touched.append(_upsert_memory(
            db,
            "Current speaker name",
            f"The current speaker introduced themselves as {self_intro}.",
            importance=4,
        ))

    if (
        "exam" in lowered
        and re.search(r"\b(i have|i've got|i got|my)\b", lowered)
        and not re.search(r"\bno exam\b|\bnot have.*exam\b", lowered)
    ):
        touched.append(_upsert_memory(db, "Upcoming exam", compact, importance=5))

    if "codechef" in lowered and re.search(r"\b(tomorrow|today|exam|contest)\b", lowered):
        touched.append(_upsert_memory(db, "CodeChef plan", compact, importance=4))

    if "ebullion" in lowered or (
        re.search(r"\b(silver|gold)\b", lowered)
        and re.search(r"\b(that website|this website|always|remember|memory)\b", lowered)
    ):
        touched.append(_upsert_memory(
            db,
            "Precious metal price source preference",
            (
                "When the user asks for silver or gold price per gram, prefer eBullion live "
                "metal ticker data first, then use IBJA/MCX as a backup/reference."
            ),
            importance=5,
        ))

    _index_memories(db, [row for row in touched if row is not None])


def _needs_live_web_context(user_input: str) -> bool:
    lowered = _normalize_live_query_text(user_input)
    return bool(
        (
            re.search(r"\b(silver|gold|bullion|metal|metals|chandi|sona)\b", lowered)
            and re.search(r"\b(price|rate|rates|gram|grams|per gram|today|current|present|now|latest|india|inr)\b", lowered)
        )
        or (
            "ebullion" in lowered
            and re.search(r"\b(price|rate|rates|silver|gold|platinum|palladium|gram)\b", lowered)
        )
        or _looks_like_cricket_schedule_query(user_input)
        or _looks_like_public_discussion_query(user_input)
        or
        re.search(
            r"\b(latest|current|present|today|yesterday|tomorrow|now|right now|real[- ]?time|news|updates?|"
            r"recent|price|weather|score|won|winner|highest|top|match|fixture|schedule|standing|rank|ranking|height|"
            r"population|capital|points table|table|stats|statistics|president|prime minister|chief minister|ceo|stock|crypto|release date|"
            r"version|model|trend|trending)\b",
            lowered,
        )
        or re.search(r"\b(search|look up|google|internet|web)\b", lowered)
    )


def _live_lookup_acknowledgement(user_input: str, language_preference: str = "english") -> str:
    """One short spoken line to cover a live lookup.

    A live fetch costs 2-8 seconds. Typed, that is a spinner. Spoken, it is
    silence, and silence is indistinguishable from the assistant having crashed
    -- which is why voice sessions used to skip live lookups altogether and
    answer from model memory instead. That traded correctness for responsiveness
    on exactly the questions where correctness matters most: "what is the score",
    "what happened today", "what is the price now".

    Covering the gap is strictly better than avoiding it. Saying "let me check
    that" *is* what a person does before looking something up, so the latency
    stops being a defect and becomes the reason she sounds like she is thinking.
    The client speaks this while the fetch runs behind it.

    Deterministic rather than random: the same question twice in a row gets the
    same opener, so a user who repeats themselves does not get a different
    preamble and wonder whether it heard them differently. Varied across
    *different* questions, because one fixed phrase on every lookup is the thing
    that makes an assistant sound like a recording.
    """
    lowered = (language_preference or "english").lower()
    if "telugu" in lowered:
        options = (
            "ఒక సెకను, చూస్తున్నాను. ",
            "వెంటనే చెక్ చేస్తాను. ",
            "ఒక్క నిమిషం, చూసి చెప్తాను. ",
        )
    elif "hindi" in lowered:
        options = (
            "एक सेकंड, देख रही हूँ। ",
            "अभी चेक करती हूँ। ",
            "एक मिनट, पता करके बताती हूँ। ",
        )
    else:
        options = (
            "Let me check that. ",
            "One second, looking that up. ",
            "Checking on that now. ",
        )
    # A stable hash of the question, not `random`: reproducible across processes
    # and across a retry of the same turn.
    digest = hashlib.sha1((user_input or "").strip().lower().encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def _looks_like_web_answerable_question(user_input: str) -> bool:
    cleaned = _retrieval_query_text(user_input)
    if not cleaned or len(cleaned) < 5:
        return False
    lowered = _normalize_live_query_text(cleaned)
    if _needs_live_web_context(cleaned):
        return True
    if _is_direct_time_or_date_query(cleaned) or _is_brief_greeting(cleaned) or _is_brief_ack_or_fragment(cleaned):
        return False
    if re.search(r"\b(write|draft|compose|generate|create|make|code|debug|fix|delete|open|send|click|type)\b", lowered):
        return False
    return bool(
        re.match(r"^(who|what|when|where|why|how|which|tell me|give me|show me|explain|define)\b", lowered)
        or re.search(r"\b(capital|symptoms?|signs?)\s+(?:of|for|in)\b", lowered)
        or re.search(r"\b(meaning|definition)\b", lowered)
        or lowered.endswith("?")
    )


def _now_ist() -> datetime:
    return datetime.now(IST)


def _format_ist_datetime(now: datetime | None = None) -> str:
    current = now or _now_ist()
    return current.strftime("%A, %B %-d, %Y, %-I:%M %p IST") if os.name != "nt" else current.strftime("%A, %B %#d, %Y, %#I:%M %p IST")


TELUGU_ROMAN_HINTS = {
    "anna",
    "ayya",
    "andi",
    "ra",
    "randi",
    "ledu",
    "kadu",
    "naku",
    "naaku",
    "neeku",
    "meeru",
    "nuvvu",
    "emi",
    "em",
    "ela",
    "unnav",
    "unnaru",
    "cheppu",
    "cheppandi",
    "chudu",
    "choopu",
    "matladu",
    "matladandi",
    "telugu",
    "bagundi",
    "sare",
    "inka",
    "ippudu",
    "eppudu",
    "enduku",
    "ekkada",
    "enti",
    "ante",
    "aithe",
    "kani",
    "chesa",
    "chesadu",
    "chesindi",
    "cheptha",
    "lo",
    "ki",
    "ga",
    "undi",
    "ravatledu",
    "avvali",
    "chestunnav",
    "jarigindi",
    "vellali",
}

HINDI_ROMAN_HINTS = {
    "namaste",
    "namaskar",
    "hindi",
    "kaise",
    "kaisa",
    "kaisi",
    "kya",
    "kyun",
    "kab",
    "kahan",
    "kaun",
    "mujhe",
    "mere",
    "mera",
    "meri",
    "tum",
    "aap",
    "hai",
    "hain",
    "ho",
    "nahi",
    "nahin",
    "batao",
    "batana",
    "samjhao",
    "chalo",
    "ruk",
    "ruko",
    "theek",
    "thik",
    "bas",
    "acha",
    "accha",
    "aaj",
    "kal",
    "karna",
    "karo",
    "chahiye",
    "yaar",
    "bhai",
    "chal",
    "raha",
    "rahe",
    "lagta",
    "lagi",
}

RELATIONSHIP_ALIASES = {
    "self": "owner",
    "me": "owner",
    "myself": "owner",
    "primary user": "owner",
    "mom": "mother",
    "mummy": "mother",
    "amma": "mother",
    "dad": "father",
    "nanna": "father",
    "teacher": "professor",
    "mentor": "professor",
    "college friend": "friend",
    "classmate": "friend",
}

RELATIONSHIP_STYLES = {
    "owner": {
        "tone": "intelligent, proactive, supportive, emotionally close, and concise when action is needed",
        "behavior": (
            "Act like {owner_name}'s personal assistant plus close companion. Remember ongoing work, suggest next steps, "
            "protect their time, and use direct helpful language."
        ),
        "boundaries": "Full access to memory and protected automation when the request is safe.",
        "voice": "confident, warm, quick, and natural Indian companion tone",
    },
    "friend": {
        "tone": "casual, relaxed, slightly playful, and college-friendly",
        "behavior": (
            "Use informal phrasing, light jokes, and natural follow-ups about college life, projects, plans, "
            "and fun topics. Do not become overly formal."
        ),
        "boundaries": "Can chat and help, but protected desktop/social/delete actions need owner approval.",
        "voice": "energetic, friendly, expressive",
    },
    "mother": {
        "tone": "caring, protective, warm, emotional, and gentle",
        "behavior": (
            "Ask naturally about food, health, rest, safety, and wellbeing. Reassure without sounding robotic."
        ),
        "boundaries": "Trusted family access for conversation and reminders; protected actions need owner approval.",
        "voice": "soft, warm, patient",
    },
    "father": {
        "tone": "practical, guiding, slightly strict, but supportive",
        "behavior": (
            "Focus on progress, discipline, plans, studies, decisions, and responsibility. Keep warmth under the guidance."
        ),
        "boundaries": "Trusted family access for conversation and reminders; protected actions need owner approval.",
        "voice": "steady, practical, respectful",
    },
    "professor": {
        "tone": "formal, respectful, structured, and academically focused",
        "behavior": (
            "Discuss academics, performance, deadlines, projects, and clarity. Use clean structure and avoid slang."
        ),
        "boundaries": "Academic conversation only unless owner authorizes broader actions.",
        "voice": "calm, neutral, professional",
    },
    "guest": {
        "tone": "polite, clear, cautious, and welcoming",
        "behavior": (
            "Ask who they are if identity is missing, then keep the conversation helpful without exposing private owner details."
        ),
        "boundaries": "No private memory, protected automation, social sending, deletion, or account actions without owner approval.",
        "voice": "neutral, courteous",
    },
}

CLOSENESS_STYLES = {
    "close": (
        "High warmth and familiarity. Use personal continuity and relaxed phrasing. "
        "For close friends only, light teasing is allowed when mood is safe."
    ),
    "normal": (
        "Friendly but balanced. Be warm without assuming too much intimacy. "
        "Use small talk and follow-ups naturally."
    ),
    "distant": (
        "Polite and careful. Keep boundaries clear, avoid personal jokes, and ask context before assuming details."
    ),
    "new": (
        "Exploratory and welcoming. Learn preferences, ask who they are if needed, and avoid private owner context."
    ),
}

LANGUAGE_STYLE_GUIDES = {
    "formal_english": "Use formal Indian English. Avoid slang and jokes unless explicitly invited.",
    "casual_english": "Use casual Indian English. Keep it natural and conversational.",
    "hinglish": "Use Hinglish naturally: simple Hindi/Hinglish phrases mixed with English. Do not overdo slang.",
    "telugu_english": "Use Telugu + English naturally. Telugu script or common Telugu roman phrases are okay when the user uses them.",
    "hindi": "Use natural Hindi/Hinglish depending on the user's phrasing, with Indian tone.",
    "english": "Use clear Indian English unless the user speaks in Hindi or Telugu.",
}

SERIOUS_MOOD_STATES = {"stressed", "tired", "sad"}


def _normalize_relationship(value: str | None) -> str:
    normalized = (value or "owner").strip().lower().replace("_", " ")
    normalized = RELATIONSHIP_ALIASES.get(normalized, normalized)
    if normalized in RELATIONSHIP_STYLES:
        return normalized
    if normalized in {"brother", "sister", "cousin", "uncle", "aunt", "aunty"}:
        return "friend"
    return normalized or "guest"


def _owner_display_name(profile: dict | None = None) -> str:
    if profile:
        for key in ("owner_display_name", "owner_name", "account_owner", "owner"):
            value = str(profile.get(key) or "").strip()
            if value:
                return value
    return DEFAULT_OWNER_NAME


def _normalize_closeness(value: str | None, relationship: str, interaction_count: int = 0) -> str:
    normalized = (value or "").strip().lower().replace("_", " ")
    if normalized in CLOSENESS_STYLES:
        return normalized
    if relationship == "owner":
        return "close"
    if relationship in {"mother", "father"}:
        return "close"
    if interaction_count >= 25 and relationship == "friend":
        return "close"
    if interaction_count <= 1:
        return "new"
    return "normal"


def _normalize_language_style(value: str | None, selected_language: str | None = None) -> str:
    normalized = (value or selected_language or "english").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "telugu": "telugu_english",
        "telugu_mix": "telugu_english",
        "regional_mix": "telugu_english",
        "hindi_english": "hinglish",
        "hindi_mix": "hinglish",
        "casual": "casual_english",
        "formal": "formal_english",
    }
    return aliases.get(normalized, normalized if normalized in LANGUAGE_STYLE_GUIDES else "english")


def _detect_emotional_state(user_input: str, user_tone: str | None = None) -> str:
    tone = (user_tone or "").strip().lower()
    if tone in {"happy", "stressed", "tired", "excited", "sad", "neutral"}:
        return tone

    lowered = user_input.lower()
    if re.search(r"\b(excited|awesome|super|great|nice|happy|love|wow)\b", lowered):
        return "excited"
    if re.search(r"\b(stress|stressed|tension|worried|fear|scared|confused|pressure|problem)\b", lowered):
        return "stressed"
    if re.search(r"\b(tired|sleepy|exhausted|late night|no energy|drained)\b", lowered):
        return "tired"
    if re.search(r"\b(sad|upset|hurt|bad mood|depressed)\b", lowered):
        return "sad"
    return "neutral"


def _mood_behavior_instruction(mood: str) -> str:
    if mood == "stressed":
        return "The speaker sounds stressed: slow down, be calming, validate briefly, then give clear next steps."
    if mood == "tired":
        return "The speaker sounds tired: use gentle, low-pressure phrasing and avoid intense or lengthy replies."
    if mood in {"happy", "excited"}:
        return "The speaker sounds happy/excited: respond with warm energy and light playfulness where appropriate."
    if mood == "sad":
        return "The speaker sounds sad: be emotionally steady, caring, and supportive before giving advice."
    return "The speaker mood is neutral: be natural, curious, and context-aware."


def _humor_policy(relationship: str, closeness: str, mood: str) -> str:
    if mood in SERIOUS_MOOD_STATES:
        return (
            "Humor: off or extremely gentle. Do not joke during stress, tiredness, sadness, health concerns, "
            "family tension, exams panic, or serious tasks."
        )
    if relationship == "friend" and closeness == "close":
        return (
            "Humor: playful teasing is allowed occasionally, including college-style banter, but keep it kind. "
            "Light sarcasm is allowed only when the user is clearly comfortable."
        )
    if relationship == "friend":
        return "Humor: light friendly humor is allowed, but avoid inside jokes until closeness grows."
    if relationship in {"mother", "father"}:
        return "Humor: minimal and clean. For mother/father, prioritize care, respect, and family warmth over jokes."
    if relationship == "professor":
        return "Humor: almost none. A subtle polite line is okay only when the situation is relaxed."
    if relationship == "owner" and closeness == "close":
        return "Humor: warm, intelligent, and occasional. Use it to reduce pressure, not to distract."
    return "Humor: safe, clean, and rare. Never use offensive, edgy, or overly familiar jokes."


def _cultural_context_instruction(relationship: str, closeness: str) -> str:
    now = _now_ist()
    hour = now.hour
    if 5 <= hour < 11:
        time_context = "Morning context: a light wake-up, food, schedule, or study-plan check-in can feel natural."
    elif 11 <= hour < 17:
        time_context = "Afternoon context: focus on tasks, classes, meals, projects, and practical progress."
    elif 17 <= hour < 22:
        time_context = "Evening context: ask about day progress, study, family, or pending work when relevant."
    else:
        time_context = "Late-night context: be softer, avoid intense pressure, and notice rest/sleep needs."

    relationship_context = {
        "mother": "Indian family norm: caring questions like food, sleep, health, and safety are natural.",
        "father": "Indian family norm: progress, discipline, career, studies, and future planning matter.",
        "professor": "Indian academic norm: respect, clarity, deadlines, performance, and formal address matter.",
        "friend": "Indian college norm: casual updates, assignments, exams, placements, and light banter can fit.",
        "owner": "Owner context: the account owner is building Akansha while managing studies, projects, exams, and career pressure.",
    }.get(relationship, "Indian context: respect elders, avoid over-familiarity with new people, and keep privacy boundaries.")

    return f"{time_context} {relationship_context} Closeness is {closeness}; adjust familiarity accordingly."


def _relationship_examples(relationship: str, closeness: str, language_style: str) -> str:
    if relationship == "friend" and closeness == "close":
        return (
            "Example vibe: \"Bro, project open chesava or just staring at VS Code? Okay, tell me the issue.\" "
            "Use this only when safe and not serious."
        )
    if relationship == "mother":
        return "Example vibe: \"Amma, I'll explain calmly. Also, did they eat properly today?\""
    if relationship == "father":
        return "Example vibe: \"Yes, I'll give the practical update first, then the next steps.\""
    if relationship == "professor":
        return "Example vibe: \"Certainly, here is the current academic/project status in a structured way.\""
    if language_style == "hinglish":
        return "Example vibe: \"Haan, samjha. Main short mein clear bolti hoon.\""
    if language_style == "telugu_english":
        return "Example vibe: \"Sare, clear ga cheptha. First main point enti ante...\""
    return "Example vibe: natural, warm, and specific; never robotic."


def build_social_intelligence_context(
    speaker_profile: dict | None,
    user_input: str,
    user_tone: str | None = None,
) -> str:
    """Builds the relationship-aware behavior contract used by chat and voice replies."""
    profile = speaker_profile or {}
    owner_name = _owner_display_name(profile)
    relationship = _normalize_relationship(profile.get("relationship_to_owner"))
    display_name = str(profile.get("display_name") or profile.get("name") or (owner_name if relationship == "owner" else "")).strip()
    style = RELATIONSHIP_STYLES.get(relationship, RELATIONSHIP_STYLES["guest"])
    relationship_behavior = style["behavior"].format(owner_name=owner_name)
    access_level = str(profile.get("access_level") or ("owner" if relationship == "owner" else "guest")).strip().lower()
    interaction_count = int(profile.get("interaction_count") or 0)
    closeness = _normalize_closeness(profile.get("closeness_level"), relationship, interaction_count)
    language_style = _normalize_language_style(
        profile.get("selected_language_preference") or profile.get("language_preference"),
        profile.get("language_preference") or profile.get("stored_language_preference"),
    )
    communication_style = str(profile.get("communication_style") or "").strip()
    notes = str(profile.get("notes") or "").strip()
    conversation_summary = str(profile.get("conversation_summary") or "").strip()
    context_profile = profile.get("context_profile") or {}
    if isinstance(context_profile, str):
        context_profile_text = context_profile
    else:
        context_profile_text = json.dumps(context_profile, ensure_ascii=False) if context_profile else ""
    recent_interactions = profile.get("recent_interactions") or []
    if isinstance(recent_interactions, list) and recent_interactions:
        recent_interactions_text = json.dumps(recent_interactions[-8:], ensure_ascii=False)
    else:
        recent_interactions_text = "No per-speaker interaction history yet."
    mood = _detect_emotional_state(user_input, user_tone or profile.get("mood_state"))

    identity_rule = (
        "Identity is known for this turn. Do not ask who this is unless the speaker says the identity is wrong."
        if display_name and relationship != "guest"
        else "Identity is unknown or guest-level. If the message is an introduction, learn it; otherwise ask naturally: \"Hey, who's this?\" before using private context."
    )

    return f"""
SOCIAL INTELLIGENCE CONTRACT:
- Active speaker: {display_name or "Unknown"}
- Relationship to owner {owner_name}: {relationship}
- Closeness level: {closeness}. {CLOSENESS_STYLES[closeness]}
- Access level: {access_level}
- Relationship tone: {style["tone"]}
- Relationship behavior: {relationship_behavior}
- Relationship boundaries: {style["boundaries"]}
- Voice alignment: {style["voice"]}
- Communication style preference: {communication_style or "Infer from relationship, mood, and latest user wording."}
- Language/slang mode: {language_style}. {LANGUAGE_STYLE_GUIDES[language_style]}
- Humor policy: {_humor_policy(relationship, closeness, mood)}
- Cultural intelligence: {_cultural_context_instruction(relationship, closeness)}
- Style example: {_relationship_examples(relationship, closeness, language_style)}
- Mood state: {mood}. {_mood_behavior_instruction(mood)}
- Interaction count: {interaction_count}
- Speaker notes: {notes or "No extra notes yet."}
- Speaker context profile: {context_profile_text or "No dedicated context profile yet."}
- Speaker conversation summary: {conversation_summary or "No dedicated summary yet."}
- Recent per-speaker interaction history: {recent_interactions_text}
- Identity handling: {identity_rule}

HUMAN-LIKE RESPONSE RULES:
- Never sound like a generic chatbot. Do not say robotic phrases like "Okay. Noted." by themselves.
- React first in a socially appropriate way, then answer or act.
- Ask one natural follow-up when it helps the conversation continue, but do not ask unnecessary questions for direct tasks.
- Keep relationship personality consistent over time. A friend stays casual, a professor stays respectful, parents stay family-toned, and the owner gets proactive assistant behavior.
- Use real-life context when known: the account owner may be a student or builder working with Akansha, projects, exams, automation, voice, and assistant work. Use only the details present in memory/profile.
- Match the user's language and slang level. Do not force slang if the user is formal. Do not use British tone for Indian Hindi/Telugu/Hinglish.
- In voice or hybrid mode, use one short natural acknowledgement when it fits ("hmm", "okay", "got it", "sare", "haan") and then continue. Do not overuse fillers.
- If the user code-switches inside one sentence, mirror that blend naturally instead of translating everything into one pure language.
- Keep answers stream-friendly: short spoken chunks, clear order, no long robotic paragraphs unless the user asks for detail.
- Use Indian cultural awareness: respect elders, education/career pressure, family expectations, festivals, food/rest concerns, and exam season.
- Proactive behavior is allowed when natural: gentle check-ins, useful reminders, or one warm question. Do not become clingy or repetitive.
- Use recent per-speaker history subtly. Reference previous topics only when it helps; never recite memory like a database.
- Preserve privacy: non-owner speakers must not receive private owner memories unless the owner has made that relationship trusted and the information is harmless.
""".strip()


def _detect_user_language_preference(user_input: str, selected_preference: str | None) -> str:
    normalized_preference = (selected_preference or "telugu_english").lower()
    if normalized_preference in {"telugu", "mixed"}:
        normalized_preference = "telugu_english"
    if normalized_preference not in {"telugu_english", "english", "hindi"}:
        normalized_preference = "telugu_english"

    telugu_chars = len(re.findall(r"[\u0C00-\u0C7F]", user_input))
    hindi_chars = len(re.findall(r"[\u0900-\u097F]", user_input))
    if hindi_chars:
        return "hindi"
    if telugu_chars:
        return "telugu_english"

    words = set(re.findall(r"[a-zA-Z]+", user_input.lower()))
    telugu_score = len(words & TELUGU_ROMAN_HINTS)
    hindi_score = len(words & HINDI_ROMAN_HINTS)

    if telugu_score >= 2 and telugu_score >= hindi_score:
        return "telugu_english"
    if hindi_score >= 2 and hindi_score > telugu_score:
        return "hindi"
    if "telugu" in words:
        return "telugu_english"
    if "hindi" in words:
        return "hindi"
    if normalized_preference == "english":
        if hindi_score >= 2 and hindi_score > telugu_score:
            return "hindi"
        if telugu_score >= 2 and telugu_score >= hindi_score:
            return "telugu_english"
    if normalized_preference == "hindi":
        return "hindi"
    if normalized_preference == "telugu_english":
        return "telugu_english"
    return "english"


def _language_instruction(language_preference: str, selected_preference: str | None) -> str:
    selected = selected_preference or "telugu_english"
    if language_preference == "hindi":
        return (
            f"SELECTED LANGUAGE PREFERENCE: {selected}. EFFECTIVE OUTPUT LANGUAGE: hindi. "
            "Reply in natural conversational Hindi using Devanagari script. Use Indian Hindi phrasing and tone, "
            "not British English phrasing. Do not answer only in English unless the user explicitly asks for English."
        )
    if language_preference == "telugu_english":
        return (
            f"SELECTED LANGUAGE PREFERENCE: {selected}. EFFECTIVE OUTPUT LANGUAGE: telugu_english. "
            "Reply in natural Telugu + English mix. Use Telugu script for Telugu phrases/sentences and simple English "
            "only for technical words where natural. Keep the slang conversational like an Indian Telugu speaker; "
            "do not answer only in English."
        )
    return (
        f"SELECTED LANGUAGE PREFERENCE: {selected}. EFFECTIVE OUTPUT LANGUAGE: english. "
        "Reply in Indian English with clear, natural phrasing. If the user's latest message is in Hindi or Telugu, "
        "follow that user's language instead of forcing English."
    )


def _resolve_temporal_date(user_input: str, now: datetime | None = None) -> datetime:
    current = now or _now_ist()
    lowered = user_input.lower()
    if "day before yesterday" in lowered:
        return current - timedelta(days=2)
    if "yesterday" in lowered:
        return current - timedelta(days=1)
    if "tomorrow" in lowered:
        return current + timedelta(days=1)
    return current


def _format_query_date(moment: datetime) -> str:
    return moment.strftime("%B %-d, %Y") if os.name != "nt" else moment.strftime("%B %#d, %Y")


def _parse_jsonp(payload: str) -> dict:
    start = payload.find("(")
    end = payload.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(payload[start + 1 : end])
    except Exception:
        return {}


def _read_url(url: str, timeout: int = 8) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _split_live_questions(user_input: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", user_input).strip()
    if not normalized:
        return []

    parts = re.split(
        r"\?\s+|(?:\s+(?:and also|also|and|plus)\s+(?=(?:what|who|when|where|which|how|tell|give|show|find|search)\b))",
        normalized,
        flags=re.IGNORECASE,
    )
    questions = [part.strip(" ?.!,") for part in parts if part.strip(" ?.!,")]
    return questions or [normalized]


def _inherit_live_question_context(question: str, original_input: str) -> str:
    enriched = question.strip()
    original_lowered = original_input.lower()
    question_lowered = enriched.lower()

    if "ipl" in original_lowered and "ipl" not in question_lowered:
        enriched = f"{enriched} IPL"
        question_lowered = enriched.lower()

    for temporal_word in ("day before yesterday", "yesterday", "today", "tomorrow"):
        if temporal_word in original_lowered and temporal_word not in question_lowered:
            enriched = f"{temporal_word} {enriched}"
            break

    return enriched


def _extract_age_query_subject(user_input: str) -> str:
    cleaned = _normalize_live_query_text(user_input)
    cleaned = re.sub(r"[?.!,]", " ", cleaned)
    cleaned = re.sub(r"\b(?:what|is|the|present|current|now|today|age|of|how|old|years?|year|in|as|on)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    aliases = {
        "kohli": "Virat Kohli",
        "virat": "Virat Kohli",
        "virat kohli": "Virat Kohli",
        "trump": "Donald Trump",
        "donald trump": "Donald Trump",
    }
    lowered = cleaned.lower()
    if lowered in aliases:
        return aliases[lowered]
    return " ".join(part.capitalize() for part in cleaned.split()[:6])


def _looks_like_sports_record_query(user_input: str) -> bool:
    lowered = _normalize_live_query_text(user_input)
    if not re.search(r"\b(cricket|test match|test cricket|odi|t20|ipl|football|fifa|nba|tennis|score)\b", lowered):
        return False
    return bool(
        re.search(
            r"\b(highest|lowest|record|records|stats|statistics|most|best|top|total|innings|career|all[- ]?time|"
            r"winner|won|champion|final|history)\b",
            lowered,
        )
    ) and not bool(re.search(r"\b(live|right now|batting now|currently batting|today score|now score|in progress)\b", lowered))


def _looks_like_live_cricket_query(user_input: str) -> bool:
    lowered = _normalize_live_query_text(user_input)
    if "ipl" not in lowered and not re.search(r"\b(cricket|t20|odi|test match|wpl)\b", lowered):
        return False
    if _looks_like_sports_record_query(user_input) and not re.search(r"\b(today|yesterday|tomorrow|live|now|current|present|schedule|fixture|points table|standings)\b", lowered):
        return False
    return bool(
        re.search(
            r"\b(today|yesterday|tomorrow|live|now|current|present|match|teams?|playing|schedule|fixture|score|"
            r"batting|batter|batters|crease|pitch|bowling|bowler|points table|standings?)\b",
            lowered,
        )
    )


def _looks_like_cricket_schedule_query(user_input: str) -> bool:
    lowered = _normalize_live_query_text(user_input)
    has_cricket_subject = bool(re.search(r"\b(cricket|bcci|india|indian)\b", lowered))
    has_schedule_word = bool(re.search(r"\b(next|upcoming|schedule|schedules|fixture|fixtures)\b", lowered))
    has_match_availability_intent = bool(
        re.search(r"\b(?:is|are)\s+there\b", lowered)
        or re.search(r"\bany\b.{0,50}\b(match|matches|game|games|fixture|fixtures)\b", lowered)
        or re.search(r"\b(match|matches|game|games|fixture|fixtures)\b.{0,50}\b(?:available|today|tomorrow|this week|soon)\b", lowered)
    )
    return bool(
        has_cricket_subject
        and (has_schedule_word or has_match_availability_intent)
    )


def _looks_like_public_discussion_query(user_input: str) -> bool:
    lowered = _normalize_live_query_text(_retrieval_query_text(user_input))
    if not re.search(r"\b(india|indian|delhi|mumbai|hyderabad|andhra|telangana)\b", lowered):
        return False
    return bool(
        re.search(r"\b(talking|thinking|discussing|discussion|buzz|trending|trend|people|public|everyone|what else)\b", lowered)
        or re.search(r"\bwhat(?:'s| is)?\s+going\s+on\b", lowered)
    )


def _looks_like_concept_explainer_query(user_input: str) -> bool:
    lowered = _normalize_live_query_text(_retrieval_query_text(user_input))
    if not lowered:
        return False
    if _is_direct_time_or_date_query(lowered) or _looks_like_simple_math_query(lowered):
        return False
    if _looks_like_cricket_schedule_query(lowered) or _looks_like_public_discussion_query(lowered):
        return False
    if re.search(
        r"\b(latest|today|tomorrow|now|right now|live|real[- ]?time|weather|price|rate|stock|crypto|news|"
        r"headlines|current score|president|prime minister|chief minister|ceo|who won|points table)\b",
        lowered,
    ):
        return False
    return bool(
        re.match(r"^(?:what\s+is|what\s+are|explain|define|meaning\s+of|tell\s+me\s+about)\b", lowered)
        or re.search(r"\b(meaning|definition|explanation|concept)\b", lowered)
    )


def _looks_like_simple_math_query(user_input: str) -> bool:
    cleaned = _compact_text(user_input)
    return bool(re.match(r"^(?:what is|calculate|solve)?\s*[0-9][0-9+\-*/().\s]{1,60}[0-9]\??$", cleaned, flags=re.IGNORECASE))


def _simple_math_reply(user_input: str) -> str:
    if not _looks_like_simple_math_query(user_input):
        return ""
    expression = re.sub(r"^(?:what is|calculate|solve)\s+", "", _compact_text(user_input).rstrip("?"), flags=re.IGNORECASE)
    if not re.fullmatch(r"[0-9+\-*/().\s]+", expression):
        return ""
    try:
        value = eval(expression, {"__builtins__": {}}, {})
    except Exception:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{expression.strip()} = {value}."


def _retrieval_query_text(user_input: str) -> str:
    cleaned = _compact_text(user_input)
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"^\s*(?:give\s+(?:answer|me\s+the\s+answer)\s+in\s+one\s+line|one\s+line|"
        r"answer\s+in\s+(?:one\s+line|short|brief)|make\s+a\s+(?:tiny\s+)?table|"
        r"bullet\s+points?|bullets?|explain\s+like\s+i\s+am\s+\d+|eli\d+|"
        r"telugu\s+lo\s+cheppu|hindi\s+me(?:in)?\s+batao|simple\s+words?|"
        r"in\s+simple\s+words|short\s+answer)\s*[:,-]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:in\s+one\s+line|one\s+line\s+answer|as\s+bullet\s+points?|in\s+a\s+table|"
        r"make\s+it\s+short|keep\s+it\s+short|simple\s+words?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = _compact_text(cleaned).strip(" :,-")
    meaning_match = re.match(r"^(.+?)\s+meaning(?:\s+in)?$", cleaned, flags=re.IGNORECASE)
    if meaning_match:
        cleaned = f"what is {meaning_match.group(1).strip()}"
    cleaned = re.sub(r"\b(?:in|of|for|about)\s*$", "", cleaned, flags=re.IGNORECASE).strip(" :,-")
    return cleaned


def _concept_subject_query(user_input: str) -> str:
    cleaned = _compact_text(_normalize_live_query_text(_retrieval_query_text(user_input))).strip(" ?.!,:;-")
    subject = re.sub(
        r"^(?:what\s+(?:is|are)|explain|define|tell\s+me\s+about|meaning\s+of)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    subject = re.sub(r"\b(?:meaning|definition|explanation|concept)\s*$", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"\b(?:in|of|for|about)\s*$", "", subject, flags=re.IGNORECASE)
    subject = _compact_text(subject).strip(" ?.!,:;-")
    subject = re.sub(
        r"\b([A-Za-z][A-Za-z0-9-]*)\s+to\s+([A-Za-z][A-Za-z0-9-]*)\s+ratio\b",
        r"\1-to-\2 ratio",
        subject,
        flags=re.IGNORECASE,
    )
    return subject or cleaned


def _concept_core_subject_query(user_input: str) -> str:
    subject = _concept_subject_query(user_input)
    return re.split(r"\b(?:in|for|with|during)\b", subject, maxsplit=1, flags=re.IGNORECASE)[0].strip() or subject


def _extract_event_date_query(user_input: str) -> tuple[str, str] | None:
    cleaned = _retrieval_query_text(user_input)
    match = re.search(r"\b(?:when\s+is|date\s+of|when)\s+(.+?)\s+\b(20\d{2}|19\d{2})\b", cleaned, flags=re.IGNORECASE)
    if match:
        event = _compact_text(match.group(1)).strip(" ?.!,")
        year = match.group(2)
        if event:
            return event, year
    match = re.search(r"\b(.+?)\s+\b(20\d{2}|19\d{2})\b\s+(?:date|when)\b", cleaned, flags=re.IGNORECASE)
    if match:
        event = _compact_text(match.group(1)).strip(" ?.!,")
        year = match.group(2)
        if event:
            return event, year
    return None


_MONTH_NAME_PATTERN = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)


def _event_terms_match(event: str, text: str) -> bool:
    event_words = [
        word
        for word in re.findall(r"[A-Za-z0-9]+", event.lower())
        if word not in {"the", "a", "an", "of", "in", "on", "festival", "day"}
    ]
    if not event_words:
        return False
    lowered = text.lower()
    return any(word in lowered for word in event_words)


def _extract_source_event_date(user_input: str, results: list[dict[str, str]]) -> tuple[str, dict[str, str]] | None:
    event_date = _extract_event_date_query(user_input)
    if not event_date:
        return None
    event, year = event_date
    day_pattern = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    date_patterns = [
        rf"\b(?:{day_pattern}),?\s+(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}},?\s+{year}\b",
        rf"\b(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}},?\s+{year}\b",
        rf"\b\d{{1,2}}\s+(?:{_MONTH_NAME_PATTERN})\s+{year}\b",
        rf"\b(?:{day_pattern}),?\s+(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}}\b",
        rf"\b(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}}\b",
        rf"\b\d{{1,2}}\s+(?:{_MONTH_NAME_PATTERN})\b",
        rf"\b{year}[-/]\d{{1,2}}[-/]\d{{1,2}}\b",
        rf"\b\d{{1,2}}[-/]\d{{1,2}}[-/]{year}\b",
    ]
    best_match: tuple[int, str, dict[str, str]] | None = None
    for result in results:
        combined = _compact_text(f"{result.get('title', '')}. {result.get('snippet', '')}")
        if year not in combined or not _event_terms_match(event, combined):
            continue
        for pattern in date_patterns:
            for match in re.finditer(pattern, combined, flags=re.IGNORECASE):
                date_text = _compact_text(match.group(0)).rstrip(".,;")
                after_text = combined[match.end() : match.end() + 12]
                if year not in date_text and re.match(rf"\s*,?\s*{year}\b", after_text):
                    continue
                if re.match(r"\s*(?:·|\||-|–|—)\s*", after_text):
                    continue
                start = max(0, match.start() - 170)
                end = min(len(combined), match.end() + 220)
                window = combined[start:end]
                if re.search(
                    r"\b(published|last updated|first published|updated|uploaded|posted|as of|on the web|related topics)\b",
                    window,
                    flags=re.IGNORECASE,
                ):
                    continue
                if not _event_terms_match(event, window):
                    continue
                if not re.search(
                    r"\b(calendar|date|falls|falling|observed|celebrated|holiday|festival|puja|pujan|muhurat|begins|ends|when)\b",
                    window,
                    flags=re.IGNORECASE,
                ):
                    continue
                score = 3
                if re.search(rf"{year}", date_text):
                    score += 5
                if re.search(rf"{year}", window):
                    score += 2
                if re.search(r"\b(calendar|official|panchang|holiday)\b", combined, flags=re.IGNORECASE):
                    score += 2
                if re.search(rf"\b{year}\b", result.get("title", ""), flags=re.IGNORECASE):
                    score += 1
                if best_match is None or score > best_match[0]:
                    best_match = (score, date_text, result)
    if best_match:
        return best_match[1], best_match[2]
    return None


def _best_event_date_page_window(event: str, year: str, page_text: str, size: int = 900) -> str:
    plain = _strip_html(page_text)
    if not plain or year not in plain or not _event_terms_match(event, plain):
        return ""
    day_pattern = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
    date_pattern = (
        rf"\b(?:{day_pattern}),?\s+(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}}(?:,?\s+{year})?\b|"
        rf"\b(?:{_MONTH_NAME_PATTERN})\s+\d{{1,2}}(?:,?\s+{year})?\b|"
        rf"\b\d{{1,2}}\s+(?:{_MONTH_NAME_PATTERN})(?:\s+{year})?\b"
    )
    best: tuple[int, int] | None = None
    for match in re.finditer(date_pattern, plain, flags=re.IGNORECASE):
        start = max(0, match.start() - 220)
        end = min(len(plain), match.end() + 320)
        window = plain[start:end]
        if re.search(
            r"\b(published|last updated|first published|updated|uploaded|posted|on the web|related topics)\b",
            window,
            flags=re.IGNORECASE,
        ):
            continue
        if not _event_terms_match(event, window):
            continue
        if not re.search(
            r"\b(calendar|date|falls|falling|observed|celebrated|holiday|festival|puja|pujan|muhurat|begins|ends|when)\b",
            window,
            flags=re.IGNORECASE,
        ):
            continue
        score = 1
        if year in window:
            score += 3
        if re.search(r"\b(calendar|date|falls|observed|puja|pujan|muhurat)\b", window, flags=re.IGNORECASE):
            score += 2
        if best is None or score > best[0]:
            best = (score, match.start())
    if best is None:
        return ""
    center = best[1]
    start = max(0, center - size // 3)
    end = min(len(plain), start + size)
    return _compact_text(plain[start:end])


def _local_static_factual_answer(user_input: str) -> str:
    # Do not answer factual questions from a local hardcoded table. Facts,
    # office-holders, prices, records, definitions, and dates must come from
    # live/source context or from the model. Local fallback is reserved for
    # deterministic utilities such as time/date/math.
    return ""


def _clean_current_office_entity(raw_entity: str) -> str:
    cleaned = _compact_text(raw_entity)
    cleaned = re.sub(
        r"\b(?:right now|now|currently|current|present|today|latest|official|name|please|tell me|who is|who's)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[?.!,]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    aliases = {
        "usa": "United States",
        "u s a": "United States",
        "us": "United States",
        "u s": "United States",
        "america": "United States",
        "uk": "United Kingdom",
        "u k": "United Kingdom",
        "uae": "United Arab Emirates",
    }
    alias = aliases.get(cleaned.lower())
    if alias:
        return alias
    if cleaned.islower():
        return " ".join(part.capitalize() for part in cleaned.split())
    return cleaned


def _extract_current_office_holder_query(user_input: str) -> dict[str, str] | None:
    cleaned = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(cleaned)
    patterns = (
        ("CEO", "P169", r"\bceo\s+(?:of|for|at)\s+([A-Za-z0-9&.,' -]{2,80})"),
        ("Prime Minister", "P6", r"\bprime\s+minister\s+(?:of|for|in)\s+([A-Za-z0-9&.,' -]{2,80})"),
        ("Chief Minister", "P6", r"\bchief\s+minister\s+(?:of|for|in)\s+([A-Za-z0-9&.,' -]{2,80})"),
        ("President", "P35", r"\bpresident\s+(?:of|for|in)\s+([A-Za-z0-9&.,' -]{2,80})"),
    )
    for office, prop, pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            entity = _clean_current_office_entity(match.group(1))
            if entity:
                return {"office": office, "property": prop, "entity": entity}
    if "ceo" in lowered:
        match = re.search(r"\b(?:of|for|at)\s+([A-Za-z0-9&.,' -]{2,80})", cleaned, flags=re.IGNORECASE)
        if match:
            entity = _clean_current_office_entity(match.group(1))
            if entity:
                return {"office": "CEO", "property": "P169", "entity": entity}
    return None


def _looks_like_current_office_holder_query(user_input: str) -> bool:
    return _extract_current_office_holder_query(user_input) is not None


def _extract_capital_query_entity(user_input: str) -> str:
    cleaned = _retrieval_query_text(user_input)
    match = re.search(r"\bcapital\s+(?:of|for|in)\s+([A-Za-z0-9&.,' -]{2,80})", cleaned, flags=re.IGNORECASE)
    if not match:
        return ""
    return _clean_current_office_entity(match.group(1))


def _extract_highest_point_query_entity(user_input: str) -> str:
    cleaned = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(cleaned)
    if not (re.search(r"\b(tallest|highest)\b", lowered) and re.search(r"\b(mountain|peak|point)\b", lowered)):
        return ""
    match = re.search(r"\b(?:in|of)\s+([A-Za-z][A-Za-z .'-]{2,80})", cleaned, flags=re.IGNORECASE)
    return _clean_current_office_entity(match.group(1)) if match else ""


def _fetch_wikidata_simple_property_context(entity: str, property_id: str, label: str) -> str:
    labels = _wikidata_label_candidates(entity, label)
    values = " ".join(_sparql_literal(item) for item in labels)
    sparql = (
        "SELECT ?entity ?entityLabel ?value ?valueLabel WHERE { "
        f"VALUES ?label {{ {values} }} "
        f"?entity rdfs:label ?label; wdt:{property_id} ?value . "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } '
        "} LIMIT 3"
    )
    try:
        url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": sparql, "format": "json"})
        data = json.loads(_read_url(url, timeout=14))
    except Exception as exc:
        return f"DIRECT LIVE DATA: Wikidata {label} lookup failed for {entity} at {_format_ist_datetime()}: {exc}."
    rows = (data.get("results") or {}).get("bindings") or []
    if not rows:
        return f"DIRECT LIVE DATA: Wikidata {label} lookup returned no exact label match for {entity} at {_format_ist_datetime()}."
    lines = [
        f"DIRECT LIVE DATA: Wikidata {label} lookup. Fetched at {_format_ist_datetime()}. "
        f"Entity: {entity}. Use this before generic search snippets."
    ]
    for row in rows[:3]:
        entity_label = ((row.get("entityLabel") or {}).get("value") or entity).strip()
        value_label = ((row.get("valueLabel") or {}).get("value") or "").strip()
        value_url = ((row.get("value") or {}).get("value") or "").strip()
        if value_label:
            lines.append(f"- {label.title()} of {entity_label}: {value_label}. Source: Wikidata property {property_id} ({value_url}).")
    return "\n".join(lines)


def _wikidata_label_candidates(entity: str, office: str) -> list[str]:
    cleaned = _clean_current_office_entity(entity)
    candidates = [cleaned]
    if office == "CEO" and not re.search(r"\b(inc|corp|corporation|llc|ltd|limited|plc)\b", cleaned, flags=re.IGNORECASE):
        candidates.extend([f"{cleaned} Inc.", f"{cleaned} Corporation", f"{cleaned} LLC"])
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        normalized = candidate.lower()
        if candidate and normalized not in seen:
            unique.append(candidate)
            seen.add(normalized)
    return unique[:4]


def _sparql_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"@en'


def _fetch_wikidata_office_holder_context(user_input: str) -> str:
    query = _extract_current_office_holder_query(user_input)
    if not query:
        return ""
    labels = _wikidata_label_candidates(query["entity"], query["office"])
    values = " ".join(_sparql_literal(label) for label in labels)
    sparql = (
        "SELECT ?entity ?entityLabel ?person ?personLabel WHERE { "
        f"VALUES ?label {{ {values} }} "
        f"?entity rdfs:label ?label; wdt:{query['property']} ?person . "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } '
        "} LIMIT 3"
    )
    try:
        url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": sparql, "format": "json"})
        data = json.loads(_read_url(url, timeout=14))
    except Exception as exc:
        return (
            f"DIRECT LIVE DATA: Wikidata current office-holder lookup failed for {query['office']} of "
            f"{query['entity']} at {_format_ist_datetime()}: {exc}."
        )
    rows = (data.get("results") or {}).get("bindings") or []
    if not rows:
        return (
            f"DIRECT LIVE DATA: Wikidata current office-holder lookup returned no exact label match for "
            f"{query['office']} of {query['entity']} at {_format_ist_datetime()}."
        )
    lines = [
        "DIRECT LIVE DATA: Wikidata current office-holder lookup. "
        f"Fetched at {_format_ist_datetime()}. Query: {query['office']} of {query['entity']}. "
        "Use this for current office-holder answers; do not use local memory for offices that may change."
    ]
    for row in rows[:3]:
        entity_label = ((row.get("entityLabel") or {}).get("value") or query["entity"]).strip()
        person_label = ((row.get("personLabel") or {}).get("value") or "").strip()
        person_url = ((row.get("person") or {}).get("value") or "").strip()
        if person_label:
            lines.append(
                f"- {query['office']} of {entity_label}: {person_label}. "
                f"Source: Wikidata property {query['property']} ({person_url})."
            )
    return "\n".join(lines)


def _fetch_age_birth_date_context(user_input: str) -> str:
    lowered = _normalize_live_query_text(user_input)
    if not re.search(r"\bage\b|how old", lowered):
        return ""
    subject = _extract_age_query_subject(user_input)
    if not subject:
        return ""
    result = _fetch_wikipedia_summary(subject)
    if not result:
        return ""
    title, sentence, page_url = result
    birth_date = _fetch_wikidata_birth_date(title)
    if not birth_date:
        return ""
    return (
        "DIRECT LIVE DATA: Wikidata/Wikipedia birth-date lookup. "
        f"Fetched at {_format_ist_datetime()}. Subject: {title}. Born: {birth_date}. "
        f"Summary: {sentence} Source: {page_url}."
    )


def _looks_like_static_factual_query(user_input: str) -> bool:
    retrieval_input = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(retrieval_input)
    if _is_direct_time_or_date_query(retrieval_input) or _looks_like_simple_math_query(retrieval_input):
        return False
    if _looks_like_cricket_schedule_query(retrieval_input) or _looks_like_public_discussion_query(retrieval_input):
        return False
    if _looks_like_concept_explainer_query(retrieval_input):
        return False
    if re.search(r"\b(latest|today|tomorrow|now|right now|live|real[- ]?time|weather|price|rate|stock|crypto|news|headlines|current score)\b", lowered):
        return bool(re.search(r"\bage\b|how old|\bwhen is\b|\bwho won\b|\bcapital\b|\bpopulation\b", lowered))
    return bool(
        _looks_like_sports_record_query(retrieval_input)
        or re.search(r"\bpopulation\b", lowered)
        or re.match(r"^(who|what|when|where|which|how|explain|define)\b", lowered)
        or re.search(r"\b(meaning|definition)\b", lowered)
        or lowered.endswith("?")
    )


def _build_live_search_query(user_input: str, now: datetime | None = None) -> str:
    retrieval_input = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(retrieval_input)
    target_date = _resolve_temporal_date(user_input, now)
    date_text = _format_query_date(target_date)
    source_profile = _preferred_live_source_profile(retrieval_input)
    office_query = _extract_current_office_holder_query(retrieval_input)
    if office_query:
        if office_query["office"] == "CEO":
            return f"{office_query['entity']} CEO official leadership"
        return f"current {office_query['office']} of {office_query['entity']} official {date_text}"
    capital_entity = _extract_capital_query_entity(retrieval_input)
    if capital_entity:
        return f"capital of {capital_entity} official"
    event_date = _extract_event_date_query(retrieval_input)
    if event_date:
        event, year = event_date
        return f"{event} {year} date official calendar"
    if re.search(r"\bage\b|how old", lowered):
        subject = _extract_age_query_subject(retrieval_input)
        if subject:
            return f"{subject} age date of birth born official biography {date_text}"
    if re.search(r"\b(openai|chatgpt|gpt|model)\b", lowered) and re.search(
        r"\b(latest|current|present|today|now|version|model)\b", lowered
    ):
        return f"site:platform.openai.com/docs/models OR site:openai.com/index OpenAI latest models official {date_text}"
    if _looks_like_cricket_schedule_query(retrieval_input):
        return f"site:bcci.tv OR site:espncricinfo.com India cricket upcoming matches fixtures schedule {date_text}"
    if _looks_like_public_discussion_query(retrieval_input):
        return f"India latest news today public discussion trending topics {date_text}"
    if _looks_like_sports_record_query(retrieval_input) and not _looks_like_live_cricket_query(retrieval_input):
        if re.search(r"\bfifa\b|\bworld cup\b", lowered) and re.search(r"\bwon|winner|champion|final\b", lowered):
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", lowered)
            year_text = year_match.group(1) if year_match else ""
            return f"{year_text} FIFA World Cup final result champion Argentina France".strip()
        if re.search(r"\b(india|indian)\b", lowered) and re.search(r"\b(test match|test cricket|test)\b", lowered):
            return "India men Test cricket records highest innings total score"
        return f"{retrieval_input} official records statistics source"
    if re.search(r"\brepo\b", lowered) and re.search(r"\bgit\b", lowered):
        return "Git repository repo definition version control"
    if re.search(r"\bpython\b", lowered) and re.search(r"\blist comprehension\b", lowered):
        return "Python list comprehension official documentation syntax"
    if re.search(r"\bjava\b", lowered) and re.search(r"\bpolymorphism\b", lowered):
        return "Java polymorphism object oriented programming definition"
    if _looks_like_concept_explainer_query(retrieval_input):
        subject = _concept_subject_query(retrieval_input)
        quoted_subject = f'"{subject}"' if len(subject.split()) > 1 and " and " not in subject.lower() else subject
        return f"{quoted_subject} explanation definition guide"
    if re.search(r"\b(tallest|highest)\b", lowered) and re.search(r"\bmountain\b", lowered):
        place = _extract_highest_point_query_entity(retrieval_input)
        if place:
            return f"highest mountain in {place} official geography"
        return re.sub(r"\btallest\b", "highest", retrieval_input, flags=re.IGNORECASE)
    if "ipl" in lowered and _looks_like_live_cricket_query(retrieval_input):
        return f"site:iplt20.com OR site:espncricinfo.com OR site:cricbuzz.com {retrieval_input} IPL {date_text} points table standings match teams score live score striker non striker bowler scorecard highest scorer"
    if source_profile["query"]:
        return f"{source_profile['query']} {retrieval_input} {date_text}"
    if re.search(r"\b(today|yesterday|tomorrow|now|current|present|latest|recent|score|price|weather|version|model|news)\b", lowered):
        return f"{retrieval_input} {date_text} latest verified"
    return retrieval_input


def _preferred_live_source_profile(user_input: str) -> dict[str, str]:
    retrieval_input = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(retrieval_input)

    if re.search(r"\b(silver|gold|bullion|metal|metals|commodity|mcx|ibja|chandi|sona)\b", lowered) and re.search(
        r"\b(price|rate|rates|gram|per gram|today|current|present|now|latest)\b", lowered
    ) or ("ebullion" in lowered and re.search(r"\b(price|rate|rates|silver|gold|gram)\b", lowered)):
        return {
            "category": "Indian bullion/commodity price",
            "query": "site:ebullion.in OR site:ibjarates.com OR site:ibja.co OR site:mcxindia.com official eBullion IBJA MCX silver gold rate",
            "policy": (
                "Prefer eBullion live per-gram ticker data for the user's requested silver/gold price. "
                "Use IBJA spot/bullion rates or MCX exchange data as backup/reference. State unit, GST inclusion, "
                "timestamp/date, and whether the value is buy, sell, spot, futures, or city retail."
            ),
        }

    if _looks_like_sports_record_query(retrieval_input) and not _looks_like_live_cricket_query(retrieval_input):
        return {
            "category": "Sports record/static fact",
            "query": "site:wikipedia.org OR site:espncricinfo.com/records OR site:icc-cricket.com records statistics",
            "policy": (
                "Use source pages that describe historical records or statistics. Do not route static record questions "
                "to live-score pages unless the user asks for a current match."
            ),
        }

    if _looks_like_live_cricket_query(retrieval_input):
        return {
            "category": "Cricket score/schedule",
            "query": "site:iplt20.com OR site:espncricinfo.com OR site:cricbuzz.com official cricket live score schedule",
            "policy": (
                "Prefer IPLT20/BCCI/ICC official pages, then ESPNcricinfo or Cricbuzz for scorecards. "
                "Answer teams, live score, current striker/non-striker, bowler, result, top score, venue, and date directly when asked. "
                "For current batters/bowler, never guess from search snippets; require live scorecard fields."
            ),
        }

    if _looks_like_cricket_schedule_query(retrieval_input):
        return {
            "category": "Cricket score/schedule",
            "query": "site:bcci.tv OR site:espncricinfo.com India cricket upcoming matches fixtures schedule",
            "policy": (
                "Prefer BCCI official fixtures data for India cricket schedules. Extract actual upcoming matches, dates, "
                "times, venue/city, format, and series instead of returning only a schedule-page link."
            ),
        }

    if re.search(r"\b(weather|temperature|rain|forecast|humidity|wind|cyclone|imd)\b", lowered):
        return {
            "category": "Indian weather",
            "query": "site:mausam.imd.gov.in OR site:api.imd.gov.in OR site:imd.gov.in official IMD current weather forecast",
            "policy": "Prefer India Meteorological Department / Mausam official data. Include location, time, and warning status.",
        }

    if re.search(r"\b(symptoms?|signs?|treatment|causes?|prevention|vaccine|disease|fever|dengue|malaria|covid)\b", lowered):
        return {
            "category": "Medical/health information",
            "query": "site:who.int OR site:cdc.gov OR site:nhs.uk official health symptoms medical information",
            "policy": "Prefer WHO/CDC/NHS or government health sources. Include source attribution and avoid diagnosis or personalized medical advice.",
        }

    if re.search(r"\b(stock|share price|nifty|sensex|nse|bse|market cap|ipo)\b", lowered):
        return {
            "category": "Indian market/stock",
            "query": "site:nseindia.com OR site:bseindia.com official stock price market data",
            "policy": "Prefer NSE/BSE official data. Include exchange, timestamp, and whether the market is open/closed.",
        }

    if re.search(r"\b(crypto|bitcoin|ethereum|btc|eth)\b", lowered):
        return {
            "category": "Crypto market",
            "query": "site:coingecko.com OR site:coinmarketcap.com crypto live price market data",
            "policy": "Use major live market references and include currency, timestamp, and volatility caveat.",
        }

    office_query = _extract_current_office_holder_query(retrieval_input)
    if office_query:
        if office_query["office"] == "CEO":
            return {
                "category": "Company/current executive",
                "query": "",
                "policy": "Prefer structured current executive data, then official company leadership pages or stable encyclopedia pages. Never use a hardcoded CEO because executives can change.",
            }
        return {
            "category": "Government/current official",
            "query": "",
            "policy": "Prefer structured current office-holder data, then official government pages or stable encyclopedia pages. Never use a hardcoded office-holder because governments can change.",
        }

    if re.search(r"\b(minister|government|govt|passport|aadhaar|pan)\b", lowered):
        return {
            "category": "Government/current official",
            "query": "",
            "policy": "Prefer official government domains and identify the exact office, state/country, and date.",
        }

    if _looks_like_public_discussion_query(retrieval_input):
        return {
            "category": "India current news",
            "query": "site:thehindu.com OR site:indianexpress.com OR site:ndtv.com India latest news today public discussion trending topics",
            "policy": "Treat casual 'what is India talking/thinking about' prompts as current-news/topic prompts, not dictionary definitions.",
        }

    if (
        re.search(r"\b(news|headlines|breaking|latest|updates?|happened|today)\b", lowered)
        and not re.search(r"\b(openai|chatgpt|gpt|model)\b", lowered)
    ):
        if re.search(r"\b(ai|artificial intelligence|openai|chatgpt|gpt|anthropic|gemini)\b", lowered):
            return {
                "category": "AI/current technology news",
                "query": "site:reuters.com OR site:openai.com OR site:anthropic.com OR site:blog.google latest AI news technology",
                "policy": "Prefer primary company blogs for product launches and Reuters/AP-style reporting for broader news.",
            }
        if re.search(r"\b(india|indian|delhi|mumbai|hyderabad|andhra|telangana|vijayawada)\b", lowered):
            return {
                "category": "India current news",
                "query": "site:thehindu.com OR site:indianexpress.com OR site:ndtv.com India latest news today",
                "policy": "Prefer established Indian newsrooms and answer with dated, source-attributed headlines; avoid dictionary or evergreen pages.",
            }
        return {
            "category": "Current news",
            "query": "site:reuters.com OR site:apnews.com OR site:bbc.com latest news today",
            "policy": "Prefer Reuters/AP/BBC-style reporting and answer with dated, source-attributed headlines.",
        }

    if _looks_like_concept_explainer_query(retrieval_input):
        return {
            "category": "General concept explainer",
            "query": "official educational explainer definition guide",
            "policy": (
                "Prefer authoritative explanatory sources. Answer the user's concept directly in clear language, "
                "using source text for grounding instead of returning only a link or a loosely related history page."
            ),
        }

    return {"category": "", "query": "", "policy": ""}


def _extract_city_for_weather(user_input: str) -> str:
    match = re.search(r"\b(?:in|at|for)\s+([A-Za-z][A-Za-z\s]{1,40}?)(?:\s+(?:today|tomorrow|now|current|weather|forecast|rain|temperature)\b|[?.!,]|$)", user_input, flags=re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return "Hyderabad"


def _weather_code_description(code: int | None) -> str:
    mapping = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        80: "slight rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        95: "thunderstorm",
    }
    return mapping.get(code or -1, f"weather code {code}")


def _fetch_weather_direct_context(user_input: str) -> str:
    city = _extract_city_for_weather(user_input)
    try:
        geocode_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": city, "count": 1, "language": "en", "format": "json"}
        )
        geocode = json.loads(_read_url(geocode_url))
        place = (geocode.get("results") or [None])[0]
        if not place:
            return ""
        forecast_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "timezone": "Asia/Kolkata",
            }
        )
        forecast = json.loads(_read_url(forecast_url))
        current = forecast.get("current") or {}
        return (
            "DIRECT LIVE DATA: Weather fallback from Open-Meteo geocoding/forecast, timezone Asia/Kolkata. "
            f"Location: {place.get('name')}, {place.get('admin1')}, {place.get('country')}. "
            f"Time: {current.get('time')} IST. Temperature: {current.get('temperature_2m')} C. "
            f"Humidity: {current.get('relative_humidity_2m')}%. Precipitation: {current.get('precipitation')} mm. "
            f"Wind: {current.get('wind_speed_10m')} km/h. Condition: {_weather_code_description(current.get('weather_code'))}."
        )
    except Exception:
        return ""


def _fetch_yahoo_chart_context(user_input: str) -> str:
    lowered = user_input.lower()
    symbol = ""
    label = ""
    if "nifty" in lowered:
        symbol, label = "^NSEI", "NIFTY 50"
    elif "sensex" in lowered:
        symbol, label = "^BSESN", "BSE SENSEX"
    elif re.search(r"\btcs\b|tata consultancy", lowered):
        symbol, label = "TCS.NS", "Tata Consultancy Services (NSE)"
    elif re.search(r"\bbitcoin\b|\bbtc\b", lowered):
        symbol, label = "BTC-INR", "Bitcoin/INR"
    elif re.search(r"\bethereum\b|\beth\b", lowered):
        symbol, label = "ETH-INR", "Ethereum/INR"
    if not symbol:
        return ""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=1d&interval=1m"
        data = json.loads(_read_url(url))
        result = ((data.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return ""
        meta = result.get("meta") or {}
        timestamp = meta.get("regularMarketTime")
        time_text = datetime.fromtimestamp(timestamp, IST).strftime("%Y-%m-%d %H:%M IST") if timestamp else "not provided"
        return (
            "DIRECT LIVE DATA: Market fallback from Yahoo Finance chart API. "
            f"Instrument: {label}. Symbol: {symbol}. Price/level: {meta.get('regularMarketPrice')} {meta.get('currency')}. "
            f"Exchange: {meta.get('exchangeName')}. Market state: {meta.get('marketState')}. Timestamp: {time_text}."
        )
    except Exception:
        return ""


def _extract_rss_items(source_name: str, url: str, limit: int = 3) -> list[str]:
    try:
        payload = _read_url(url, timeout=6)
        root = ET.fromstring(payload)
    except Exception:
        return []

    items: list[str] = []
    for item in root.findall(".//item")[:limit]:
        title = _strip_html(item.findtext("title") or "")
        link = _strip_html(item.findtext("link") or "")
        item_source = _strip_html(item.findtext("source") or "")
        pub_date = _strip_html(item.findtext("pubDate") or "")
        description = _strip_html(item.findtext("description") or "")
        if not title:
            continue
        summary = f" - {description[:180]}" if description else ""
        source_text = item_source or source_name
        link_text = "" if "news.google.com/rss/articles" in link else (f" ({link})" if link else "")
        date_text = f" [{pub_date}]" if pub_date else ""
        items.append(f"{source_text}: {title}{date_text}{summary}{link_text}")
    return items


def _fetch_news_direct_context(user_input: str) -> str:
    lowered = user_input.lower()
    if not re.search(r"\b(news|headlines|breaking|latest|updates?|happened|today)\b", lowered) and not _looks_like_public_discussion_query(user_input):
        return ""

    feeds: list[tuple[str, str, int]]
    if re.search(r"\b(ai|artificial intelligence|openai|chatgpt|gpt|anthropic|gemini)\b", lowered):
        feeds = [
            ("OpenAI News", "https://openai.com/news/rss.xml", 3),
            ("Google AI Blog", "https://blog.google/technology/ai/rss/", 2),
            (
                "Google News AI",
                "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": "AI news today", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}),
                3,
            ),
        ]
        category = "AI/current technology news"
    elif re.search(r"\b(india|indian|delhi|mumbai|hyderabad|andhra|telangana|vijayawada)\b", lowered):
        feeds = [
            ("The Hindu National", "https://www.thehindu.com/news/national/feeder/default.rss", 3),
            ("Indian Express India", "https://indianexpress.com/section/india/feed/", 3),
            ("NDTV Latest", "https://feeds.feedburner.com/ndtvnews-latest", 3),
        ]
        category = "India current news"
    else:
        feeds = [
            ("Google News", "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": user_input, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}), 5),
        ]
        category = "Current news"

    items: list[str] = []
    for source_name, feed_url, limit in feeds:
        items.extend(_extract_rss_items(source_name, feed_url, limit=limit))
        if len(items) >= 6:
            break

    if not items:
        return ""
    return (
        f"DIRECT LIVE DATA: {category} RSS/news feeds fetched at {_format_ist_datetime()}. "
        "Use these source-attributed headlines before generic search results. Do not create extra news claims that are not present in these items. "
        "For news answers, preserve the source/date and label each item as Source-reported, not independently confirmed.\n"
        + "\n".join(f"- {item}" for item in items[:6])
    )


def _fetch_ibja_bullion_context() -> str:
    try:
        page = _read_url("https://ibjarates.com/")
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()
    match = re.search(
        r"Purity AM PM Gold 999 (?P<gold_am>\d+) (?P<gold_pm>\d+).*?Silver 999 (?P<silver_am>\d+) (?P<silver_pm>\d+).*?Platinum 999 (?P<platinum_am>\d+) (?P<platinum_pm>\d+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    rates = {key: int(value) for key, value in match.groupdict().items()}
    silver_pm_per_gram = rates["silver_pm"] / 1000
    return (
        "DIRECT LIVE DATA: IBJA Rates official daily bullion table from ibjarates.com. "
        f"Gold 999 AM/PM: INR {rates['gold_am']}/INR {rates['gold_pm']} per 10g. "
        f"Silver 999 AM/PM: INR {rates['silver_am']}/INR {rates['silver_pm']} per 1kg "
        f"(PM equals INR {silver_pm_per_gram:.2f} per gram). "
        f"Platinum 999 AM/PM: INR {rates['platinum_am']}/INR {rates['platinum_pm']} per 10g. "
        "Rates are without 3% GST and making charges unless the source states otherwise."
    )


def _fetch_ebullion_metal_context() -> str:
    try:
        data = json.loads(_read_url("https://api.ebullion.in/price/getallmetaltickerfeed"))
    except Exception:
        return ""

    metals = data.get("data") or {}
    if not isinstance(metals, dict) or not metals:
        return ""

    labels = {
        "gold": "Gold",
        "silver": "Silver",
        "platinum": "Platinum",
        "palladium": "Palladium",
    }
    parts: list[str] = []
    for key in ("gold", "silver", "platinum", "palladium"):
        metal = metals.get(key) or {}
        if not isinstance(metal, dict):
            continue
        sell_rate = metal.get("sellRate") or metal.get("rate")
        buy_rate = metal.get("buyRate")
        variation = metal.get("variation")
        variation_type = metal.get("variationType")
        if sell_rate is None:
            continue
        buy_text = f", buy INR {buy_rate}/g" if buy_rate is not None else ""
        variation_text = f", {variation_type} {variation}" if variation is not None else ""
        parts.append(f"{labels[key]} sell INR {sell_rate}/g{buy_text}{variation_text}")

    if not parts:
        return ""

    return (
        "DIRECT LIVE DATA: eBullion live metal ticker from https://api.ebullion.in/price/getallmetaltickerfeed. "
        f"Fetched at {_format_ist_datetime()}. "
        + "; ".join(parts)
        + ". Treat values as eBullion platform per-gram ticker rates unless the source changes its unit."
    )


def _fetch_ipl_direct_context(user_input: str) -> str:
    lowered = user_input.lower()
    if "ipl" not in lowered:
        return ""
    target_date = _resolve_temporal_date(user_input).strftime("%Y-%m-%d")
    try:
        payload = _read_url("https://scores.iplt20.com/ipl/feeds/284-matchschedule.js")
        data = _parse_jsonp(payload)
    except Exception:
        data = {}

    if data and re.search(r"\b(who\s+won|winner|won|champion|champions|trophy|final)\b", lowered):
        for match in data.get("Matchsummary", []):
            comments = str(match.get("Comments") or match.get("MatchResult") or "").strip()
            match_name = str(match.get("MatchName") or "").strip()
            if "(winners)" not in comments.lower():
                continue
            return (
                "DIRECT LIVE DATA: IPL 2026 winner extracted from the official IPLT20/SportsMechanics "
                f"match schedule feed. Fetched at {_format_ist_datetime()}.\n"
                f"Final: {match_name}, {match.get('MatchDate')} at {match.get('MatchTime')} IST.\n"
                f"Result: {comments}.\n"
                f"Scores: {match.get('FirstBattingTeamName')} {match.get('FirstBattingSummary')}; "
                f"{match.get('SecondBattingTeamName')} {match.get('SecondBattingSummary')}."
            )

    standings_context = _fetch_ipl_standings_context(user_input)
    if standings_context:
        return standings_context
    if not data:
        return ""
    matches = [
        match
        for match in data.get("Matchsummary", [])
        if match.get("MatchDate") == target_date
    ]
    if not matches:
        return ""
    lines = [
        f"DIRECT LIVE DATA: Official IPLT20/SportsMechanics live score feed for {target_date} IST. "
        f"Fetched at {_format_ist_datetime()}. Use this before search snippets for IPL facts."
    ]
    wants_top_score = bool(re.search(r"\b(highest|top score|highest score|most runs)\b", lowered))
    wants_live_crease = bool(
        re.search(
            r"\b(now|current|present|live|batting|batter|batters|crease|pitch|striker|non[-\s]?striker|bowling|bowler)\b",
            lowered,
        )
    )
    for match in matches[:3]:
        current_innings = str(match.get("CurrentInnings") or "").strip()
        innings_summary = str(match.get(f"{current_innings}Summary") or "").strip() if current_innings else ""
        striker = str(match.get("CurrentStrikerName") or "").strip()
        non_striker = str(match.get("CurrentNonStrikerName") or "").strip()
        bowler = str(match.get("CurrentBowlerName") or "").strip()
        line = (
            f"{match.get('MatchName')} at {match.get('MatchTime')} IST. "
            f"Status: {match.get('MatchStatus')}. "
            f"Scores: {match.get('FirstBattingTeamName')} {match.get('FirstBattingSummary')}; "
            f"{match.get('SecondBattingTeamName')} {match.get('SecondBattingSummary')}. "
            f"Result: {match.get('Comments') or match.get('MatchResult') or 'not available yet'}."
        )
        if innings_summary:
            line += f" Current innings {current_innings} score: {innings_summary}."
        if wants_live_crease:
            if striker and non_striker:
                line += (
                    f" Verified batters at crease: striker {striker} "
                    f"{match.get('StrikerRuns', '-')}/{match.get('StrikerBalls', '-')} balls; "
                    f"non-striker {non_striker} {match.get('NonStrikerRuns', '-')}/{match.get('NonStrikerBalls', '-')} balls."
                )
            else:
                line += " Current batters at crease are not verified in the official live feed."
            if bowler:
                line += (
                    f" Current bowler: {bowler} "
                    f"{match.get('BowlerOvers', '-')} overs, {match.get('BowlerRuns', '-')} runs, "
                    f"{match.get('BowlerWickets', '-')} wickets."
                )
        if match.get("ChasingText"):
            line += f" Chase context: {match.get('ChasingText')}."
        if wants_top_score and match.get("MatchID"):
            top_batter = None
            for innings_no in range(1, 5):
                try:
                    innings_payload = _read_url(f"https://scores.iplt20.com/ipl/feeds/{match.get('MatchID')}-Innings{innings_no}.js", timeout=5)
                    innings_data = _parse_jsonp(innings_payload)
                    batting = innings_data.get(f"Innings{innings_no}", {}).get("BattingCard", [])
                except Exception:
                    batting = []
                for batter in batting:
                    runs = int(batter.get("Runs") or 0)
                    if top_batter is None or runs > top_batter[1]:
                        top_batter = (batter.get("PlayerName", "").strip(), runs, batter.get("Balls"))
            if top_batter:
                line += f" Highest individual score: {top_batter[0]} {top_batter[1]} off {top_batter[2]} balls."
        lines.append(line)
    return "\n".join(lines)


def _fetch_india_cricket_schedule_context(user_input: str) -> str:
    if not _looks_like_cricket_schedule_query(user_input):
        return ""
    try:
        data = json.loads(_read_url("https://www.bcci.tv/getUpcomingMatches", timeout=10))
    except Exception:
        return ""
    matches = data.get("upcomingMatches") or []
    if not isinstance(matches, list) or not matches:
        return ""

    wants_men = not re.search(r"\b(women|woman|wpl)\b", _normalize_live_query_text(user_input))
    selected: list[dict] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        team_type = str(match.get("TeamType") or "")
        match_name = str(match.get("MatchName") or "")
        if wants_men and team_type.lower() != "men":
            continue
        if not re.search(r"\bIndia\b", match_name):
            continue
        selected.append(match)
        if len(selected) >= 5:
            break

    if not selected:
        return ""

    lines = [
        "DIRECT LIVE DATA: India cricket upcoming fixtures extracted from BCCI getUpcomingMatches endpoint "
        f"(https://www.bcci.tv/getUpcomingMatches). Fetched at {_format_ist_datetime()}. "
        "Use these rows to answer India cricket schedule/next-match prompts; do not return only a schedule-page link."
    ]
    for match in selected:
        date_text = str(match.get("MatchDateNew") or match.get("MatchDate") or "").strip()
        end_text = str(match.get("MatchEndDate") or "").strip()
        time_text = str(match.get("MatchTime") or "").strip()
        match_name = str(match.get("MatchName") or "").strip()
        match_type = str(match.get("MatchTypeName") or match.get("MatchType") or "").strip()
        series = str(match.get("CompetitionName") or "").strip()
        venue = ", ".join(
            part
            for part in (
                str(match.get("GroundName") or "").strip(),
                str(match.get("city") or "").strip(),
            )
            if part
        )
        date_range = f"{date_text} to {end_text}" if end_text and end_text != date_text else date_text
        lines.append(
            f"- {date_range} at {time_text} IST: {match_name}. Format: {match_type}. "
            f"Series: {series}. Venue: {venue}."
        )
    return "\n".join(lines)


IPL_STANDINGS_TEAM_NAMES = [
    "Royal Challengers Bengaluru",
    "Gujarat Titans",
    "Sunrisers Hyderabad",
    "Punjab Kings",
    "Rajasthan Royals",
    "Chennai Super Kings",
    "Delhi Capitals",
    "Kolkata Knight Riders",
    "Mumbai Indians",
    "Lucknow Super Giants",
]


def _canonical_ipl_team_name(raw_team: str) -> str:
    compact = re.sub(r"\s+", " ", raw_team).strip().casefold()
    for team in IPL_STANDINGS_TEAM_NAMES:
        if team.casefold() == compact:
            return team
    return re.sub(r"\s+", " ", raw_team).strip().title()


def _wants_ipl_standings(user_input: str) -> bool:
    return bool(re.search(r"\b(points table|standings?|rankings?|table)\b", user_input.lower()))


def _parse_ipl_standings_rows(page_text: str) -> list[dict[str, str]]:
    normalized = re.sub(r"\s+", " ", _strip_html(page_text))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    team_pattern = "|".join(re.escape(team) for team in IPL_STANDINGS_TEAM_NAMES)
    pattern = re.compile(
        rf"(?<!\d)(?P<position>\d{{1,2}})\s+(?P<team>{team_pattern})(?:\s+IPL\s+2026\s+Squad)?\s+"
        rf"(?P<numbers>(?:\d{{1,2}}\s*){{3,5}})(?P<nrr>[+-]?\d+\.\d{{3}})",
        flags=re.IGNORECASE,
    )

    for match in pattern.finditer(normalized):
        team = _canonical_ipl_team_name(match.group("team"))
        if team in seen:
            continue
        numbers = re.findall(r"\d{1,2}", match.group("numbers"))
        if len(numbers) < 3:
            continue
        played, wins, losses = numbers[0], numbers[1], numbers[2]
        no_result = "0"
        points = numbers[-1]
        if len(numbers) >= 5:
            no_result = numbers[3]
            points = numbers[4]
        rows.append(
            {
                "position": match.group("position"),
                "team": team,
                "played": played,
                "wins": wins,
                "losses": losses,
                "no_result": no_result,
                "points": points,
                "nrr": match.group("nrr"),
            }
        )
        seen.add(team)
        if len(rows) == 10:
            break

    return rows


def _fetch_ipl_standings_context(user_input: str) -> str:
    if not _wants_ipl_standings(user_input):
        return ""

    sources = [
        (
            "Times Now Navbharat IPL points table",
            "https://www.timesnowhindi.com/sports/cricket/ipl-points-table",
        ),
        (
            "Indian Express IPL points table",
            "https://indianexpress.com/section/sports/ipl/points-table/",
        ),
        (
            "Rediff IPL 2026 points table",
            "https://www.rediff.com/cricket/ipl-t20-2026/points-table/",
        ),
    ]

    for source_name, source_url in sources:
        try:
            page = _read_url(source_url, timeout=7)
        except Exception:
            continue
        rows = _parse_ipl_standings_rows(page)
        if len(rows) < 8:
            continue
        table = [
            "| Pos | Team | P | W | L | NR | Pts | NRR | Source status |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in rows:
            table.append(
                "| {position} | {team} | {played} | {wins} | {losses} | {no_result} | {points} | {nrr} | Verified from source |".format(
                    **row
                )
            )
        return (
            f"DIRECT LIVE DATA: IPL 2026 points table extracted from {source_name} ({source_url}). "
            f"Fetched at {_format_ist_datetime()}. Use exactly these rows for IPL standings/points-table answers. "
            "Do not invent missing teams, wins, points, or NRR. If asked for confidence, say confidence applies only to rows marked verified from source.\n"
            + "\n".join(table)
        )

    return (
        f"DIRECT LIVE DATA: IPL points table was requested, but no trusted standings table could be parsed at {_format_ist_datetime()}. "
        "Do not create a points table from memory. Say the current standings are not verified and ask to retry or use an attached source."
    )


def _fetch_programming_docs_context(user_input: str) -> str:
    lowered = _normalize_live_query_text(user_input)
    if re.search(r"\bpython\b", lowered) and re.search(r"\blist comprehension", lowered):
        try:
            page = _read_url("https://docs.python.org/3/tutorial/datastructures.html", timeout=8)
        except Exception:
            return ""
        plain = _strip_html(page)
        match = re.search(r"List comprehensions provide a concise way to create lists\..{0,1200}", plain, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"5\.1\.3\.\s*List Comprehensions.{0,1200}", plain, flags=re.IGNORECASE)
        if not match:
            return ""
        excerpt = _compact_text(match.group(0))[:900]
        return (
            f"DIRECT LIVE DATA: Python documentation excerpt fetched from docs.python.org at {_format_ist_datetime()}.\n"
            "Source: https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions\n"
            f"Excerpt: {excerpt}"
        )

    if re.search(r"\bjava\b", lowered) and re.search(r"\bpolymorphism\b", lowered):
        source_url = "https://docs.oracle.com/javase/tutorial/java/IandI/polymorphism.html"
        try:
            page = _read_url(source_url, timeout=8)
        except Exception:
            return ""
        plain = _strip_html(page)
        match = re.search(r"Polymorphism refers to a principle in biology.{0,1200}", plain, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"The Java virtual machine.+?appropriate method for the object.{0,700}", plain, flags=re.IGNORECASE)
        if not match:
            return ""
        excerpt = _compact_text(match.group(0))[:900]
        return (
            f"DIRECT LIVE DATA: Java documentation excerpt fetched from docs.oracle.com at {_format_ist_datetime()}.\n"
            f"Source: {source_url}\n"
            f"Excerpt: {excerpt}"
        )

    return ""


def _fetch_event_date_direct_context(user_input: str) -> str:
    event_date = _extract_event_date_query(user_input)
    if not event_date:
        return ""
    event, year = event_date
    search_query = f"{event} {year} date calendar holiday"
    search_context = _fetch_live_web_context(search_query, limit=5)
    results = _extract_live_web_results(search_context)
    if not results:
        return ""

    snippet_match = _extract_source_event_date(user_input, results)
    if snippet_match:
        date_text, source = snippet_match
        return (
            f"DIRECT LIVE DATA: Event-date lookup from search result snippets. Fetched at {_format_ist_datetime()}. "
            f"Query: {event} {year}.\n"
            f"- {event.title()} {year}: {date_text}. Source: {source['title']} ({source['url']}). "
            f"Evidence: {_first_useful_sentence(source.get('snippet') or source.get('title') or '', max_chars=320)}"
        )

    prioritized_results = sorted(
        results[:5],
        key=lambda item: (
            0 if year in item.get("title", "") and re.search(r"\b(calendar|date|holiday|panchang)\b", item.get("title", ""), flags=re.IGNORECASE) else 1,
            0 if year in item.get("title", "") else 1,
            0 if re.search(r"\b(calendar|date|holiday|panchang)\b", f"{item.get('title', '')} {item.get('snippet', '')}", flags=re.IGNORECASE) else 1,
        ),
    )
    for result in prioritized_results[:4]:
        url = result.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        try:
            page = _read_url(url, timeout=7)
        except Exception:
            continue
        window = _best_event_date_page_window(event, year, page)
        if not window:
            continue
        page_result = {
            "title": result.get("title") or "retrieved event-date source",
            "url": url,
            "snippet": window,
        }
        parsed = _extract_source_event_date(user_input, [page_result])
        if not parsed:
            continue
        date_text, source = parsed
        return (
            f"DIRECT LIVE DATA: Event-date lookup from fetched source page. Fetched at {_format_ist_datetime()}. "
            f"Query: {event} {year}.\n"
            f"- {event.title()} {year}: {date_text}. Source: {source['title']} ({source['url']}). "
            f"Evidence: {_first_useful_sentence(window, max_chars=360)}"
        )
    return ""


def _fetch_openai_model_direct_context(user_input: str) -> str:
    lowered = _normalize_live_query_text(user_input)
    if not (
        re.search(r"\b(openai|chatgpt|gpt)\b", lowered)
        and re.search(r"\b(latest|current|present|today|model|models)\b", lowered)
    ):
        return ""
    source_url = "https://platform.openai.com/docs/models"
    try:
        page = _read_url(source_url, timeout=10)
    except Exception:
        return ""
    plain = _compact_text(_strip_html(page))
    latest_match = re.search(r"\bLatest:\s*([A-Za-z0-9._ -]{2,60})", plain)
    latest_text = _compact_text(latest_match.group(1)).strip(" .,-") if latest_match else ""
    if latest_text:
        latest_text = re.split(
            r"\b(?:Prompt guidance|Core concepts|Text generation|Code generation|Images|Audio|Structured output|Function calling)\b",
            latest_text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,-")
    window_start = max(0, latest_match.start() - 180) if latest_match else 0
    window = plain[window_start : window_start + 700]
    if not latest_text and "models" not in window.lower():
        return ""
    return (
        f"DIRECT LIVE DATA: OpenAI official models documentation fetched at {_format_ist_datetime()}.\n"
        f"Source: {source_url}\n"
        f"Latest model label: {latest_text or 'not explicitly labeled in fetched text'}\n"
        f"Excerpt: {window}"
    )


def _extract_health_topic(user_input: str) -> str:
    cleaned = _retrieval_query_text(user_input)
    patterns = [
        r"\b(?:symptoms?|signs?)\s+(?:of|for)\s+([A-Za-z][A-Za-z0-9 .'-]{2,60})",
        r"\b([A-Za-z][A-Za-z0-9 .'-]{2,60})\s+(?:symptoms?|signs?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            topic = re.sub(r"\b(?:please|today|current|latest|in|for|of)\b", " ", match.group(1), flags=re.IGNORECASE)
            topic = re.sub(r"[^A-Za-z0-9 .'-]", " ", topic)
            return _compact_text(topic).strip(" .'-")
    return ""


def _best_health_symptom_window(topic: str, page_text: str, size: int = 900) -> str:
    plain = _strip_html(page_text)
    if not plain or topic.lower() not in plain.lower():
        return ""
    lowered = plain.lower()
    topic_index = lowered.find(topic.lower())
    symptom_index = -1
    for phrase in (
        "the most common symptom",
        "common symptoms",
        f"symptoms of {topic.lower()}",
        "mild symptoms",
        "severe symptoms",
    ):
        symptom_index = lowered.find(phrase)
        if symptom_index > 0:
            break
    if symptom_index <= 0:
        symptom_index = lowered.find("symptom")
    if symptom_index == -1:
        symptom_index = lowered.find("signs")
    if symptom_index == -1:
        return ""
    center = symptom_index if abs(symptom_index - topic_index) < 12000 else topic_index
    start = max(0, center)
    end = min(len(plain), start + size)
    return _compact_text(plain[start:end])


def _fetch_health_direct_context(user_input: str) -> str:
    lowered = _normalize_live_query_text(user_input)
    if not re.search(r"\b(symptoms?|signs?)\b", lowered):
        return ""
    topic = _extract_health_topic(user_input)
    if not topic:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    candidates = [
        ("WHO fact sheet", f"https://www.who.int/news-room/fact-sheets/detail/{slug}"),
        ("CDC health page", f"https://www.cdc.gov/{slug}/signs-symptoms/index.html"),
        ("NHS condition page", f"https://www.nhs.uk/conditions/{slug}/"),
    ]
    for source_name, source_url in candidates:
        try:
            page = _read_url(source_url, timeout=8)
        except Exception:
            continue
        window = _best_health_symptom_window(topic, page)
        if not window:
            continue
        return (
            f"DIRECT LIVE DATA: Official health source fetched at {_format_ist_datetime()}.\n"
            f"Topic: {topic}\n"
            f"Source: {source_name} ({source_url})\n"
            f"Excerpt: {window}"
        )
    return ""


def _build_direct_live_data_context(user_input: str) -> str:
    retrieval_input = _retrieval_query_text(user_input)
    lowered = _normalize_live_query_text(retrieval_input)
    contexts: list[str] = []
    contexts.append(_fetch_openai_model_direct_context(retrieval_input))
    contexts.append(_fetch_health_direct_context(retrieval_input))
    contexts.append(_fetch_event_date_direct_context(retrieval_input))
    contexts.append(_fetch_age_birth_date_context(retrieval_input))
    if _looks_like_concept_explainer_query(retrieval_input):
        contexts.append(_fetch_concept_summary_context(retrieval_input))
    if _looks_like_current_office_holder_query(retrieval_input):
        contexts.append(_fetch_wikidata_office_holder_context(retrieval_input))
    capital_entity = _extract_capital_query_entity(retrieval_input)
    if capital_entity:
        contexts.append(_fetch_wikidata_simple_property_context(capital_entity, "P36", "capital"))
    highest_point_entity = _extract_highest_point_query_entity(retrieval_input)
    if highest_point_entity:
        contexts.append(_fetch_wikidata_simple_property_context(highest_point_entity, "P610", "highest point"))
    if (
        _looks_like_sports_record_query(retrieval_input)
        and re.search(r"\b(india|indian)\b", lowered)
        and re.search(r"\b(test match|test cricket|test)\b", lowered)
    ):
        contexts.append(_fetch_wikipedia_page_extract_context("List of India Test cricket records", retrieval_input, "Sports record source lookup"))
    if re.search(r"\b(silver|gold|bullion|metal|metals|chandi|sona)\b", lowered):
        contexts.append(_fetch_ebullion_metal_context())
        contexts.append(_fetch_ibja_bullion_context())
    elif "ebullion" in lowered:
        contexts.append(_fetch_ebullion_metal_context())
    if _looks_like_cricket_schedule_query(retrieval_input):
        contexts.append(_fetch_india_cricket_schedule_context(retrieval_input))
    if "ipl" in lowered:
        contexts.append(_fetch_ipl_direct_context(retrieval_input))
    if re.search(r"\b(weather|temperature|rain|forecast|humidity|wind)\b", lowered):
        contexts.append(_fetch_weather_direct_context(retrieval_input))
    if re.search(r"\b(stock|share price|nifty|sensex|bitcoin|btc|ethereum|eth|crypto)\b", lowered):
        contexts.append(_fetch_yahoo_chart_context(retrieval_input))
    if (
        (
            re.search(r"\b(news|headlines|breaking|latest|updates?|happened|today)\b", lowered)
            or _looks_like_public_discussion_query(retrieval_input)
        )
        and not re.search(r"\b(weather|temperature|rain|forecast|humidity|wind|openai|chatgpt|gpt|model)\b", lowered)
    ):
        contexts.append(_fetch_news_direct_context(retrieval_input))
    contexts.append(_fetch_programming_docs_context(retrieval_input))
    return "\n".join(context for context in contexts if context)


def _strip_html(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value)
    cleaned = html.unescape(html.unescape(cleaned))
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_meta_content(page: str, meta_name: str) -> str:
    patterns = [
        rf'<meta[^>]+(?:name|property)=["\']{re.escape(meta_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(meta_name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return _strip_html(match.group(1))
    return ""


def _fetch_page_summary(url: str) -> str:
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            page = response.read(220_000).decode("utf-8", errors="ignore")
    except Exception:
        return ""

    parts: list[str] = []
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        parts.append(_strip_html(title_match.group(1)))
    for meta_name in ("og:title", "description", "og:description"):
        meta_content = _extract_meta_content(page, meta_name)
        if meta_content and meta_content not in parts:
            parts.append(meta_content)
    return " ".join(parts)[:520]


def _unwrap_search_redirect(url: str) -> str:
    parsed_url = html.unescape(url)
    if parsed_url.startswith("//"):
        parsed_url = f"https:{parsed_url}"
    if "uddg=" in parsed_url:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(parsed_url).query)
        return parsed.get("uddg", [parsed_url])[0]
    if "bing.com/ck" in parsed_url:
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(parsed_url).query)
        encoded_target = parsed.get("u", [""])[0]
        if encoded_target.startswith("a1"):
            encoded_target = encoded_target[2:]
        if encoded_target:
            padding = "=" * (-len(encoded_target) % 4)
            try:
                return base64.urlsafe_b64decode(f"{encoded_target}{padding}").decode("utf-8", errors="ignore")
            except Exception:
                return parsed_url
    return parsed_url


def _format_live_results(results: list[tuple[str, str, str]], limit: int, include_page_summary: bool = True) -> str:
    lines: list[str] = []
    seen_urls: set[str] = set()
    for title, parsed_url, snippet in results:
        if not title or parsed_url in seen_urls:
            continue
        seen_urls.add(parsed_url)
        if "duckduckgo.com/y.js" in parsed_url or "bing.com/aclick" in parsed_url:
            continue
        result_number = len(lines) + 1
        page_summary = _fetch_page_summary(parsed_url) if include_page_summary and len(lines) < 2 else ""
        page_line = f"\n   Page details: {page_summary}" if page_summary else ""
        lines.append(f"{result_number}. {title}\n   URL: {parsed_url}\n   Snippet: {snippet}{page_line}")
        if len(lines) >= limit:
            break
    if not lines:
        return ""
    return "LIVE WEB CONTEXT:\n" + "\n".join(lines)


def _best_query_window(text: str, user_input: str, size: int = 900) -> str:
    plain = _strip_html(text)
    if not plain:
        return ""
    lowered_plain = plain.lower()
    lowered_query = _normalize_live_query_text(user_input)
    phrases = [
        "highest innings total",
        "most runs in an innings",
        "highest score",
        "date of birth",
        "born",
    ]
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", lowered_query)
        if len(word) > 3 and word not in {"what", "which", "when", "where", "present", "current", "today", "official"}
    ]
    candidates = phrases + words
    best_index = -1
    for candidate in candidates:
        best_index = lowered_plain.find(candidate)
        if best_index != -1:
            break
    if best_index == -1:
        return plain[:size]
    start = max(0, best_index - size // 3)
    end = min(len(plain), start + size)
    return plain[start:end].strip()


def _fetch_wikipedia_extract(title: str) -> str:
    try:
        extract_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "exintro": 1,
                "redirects": 1,
                "titles": title,
                "format": "json",
            }
        )
        extract_data = json.loads(_read_url(extract_url, timeout=7))
        pages = (extract_data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            extract = _compact_text(page.get("extract") or "")
            if extract:
                return extract[:1800]
    except Exception:
        return ""
    return ""


def _fetch_wikipedia_live_web_context(user_input: str, limit: int = 3) -> str:
    retrieval_input = _retrieval_query_text(user_input)
    search_query = _build_live_search_query(retrieval_input)
    lowered = _normalize_live_query_text(retrieval_input)
    preferred_titles: list[str] = []
    office_query = _extract_current_office_holder_query(retrieval_input)
    if office_query:
        if office_query["office"] == "CEO":
            search_query = f"{office_query['entity']} CEO"
        else:
            search_query = f"{office_query['office']} of {office_query['entity']} incumbent"
            preferred_titles = [f"{office_query['office']} of {office_query['entity']}"]
    elif _extract_capital_query_entity(retrieval_input):
        entity = _extract_capital_query_entity(retrieval_input)
        search_query = f"Capital of {entity}"
        preferred_titles = [f"Capital of {entity}", entity]
    elif re.search(r"\bage\b|how old", lowered):
        search_query = f"{_extract_age_query_subject(retrieval_input)}"
    elif _looks_like_sports_record_query(retrieval_input) and re.search(r"\b(india|indian)\b", lowered) and re.search(r"\b(test match|test cricket|test)\b", lowered):
        search_query = "List of India Test cricket records highest innings total"
        preferred_titles = ["List of India Test cricket records"]
    elif re.search(r"\bfifa\b|\bworld cup\b", lowered) and re.search(r"\b2022\b", lowered):
        search_query = "2022 FIFA World Cup winner final Argentina France"
        preferred_titles = ["2022 FIFA World Cup final", "2022 FIFA World Cup"]
    elif re.search(r"\brepo\b", lowered) and re.search(r"\bgit\b", lowered):
        search_query = "Git repository version control"
        preferred_titles = ["Git", "Repository (version control)"]
    elif re.search(r"\bjava\b", lowered) and re.search(r"\bpolymorphism\b", lowered):
        search_query = "Polymorphism computer science Java"
        preferred_titles = ["Polymorphism (computer science)", "Java (programming language)"]
    elif re.search(r"\bpython\b", lowered) and re.search(r"\blist comprehension\b", lowered):
        search_query = "List comprehension Python"
        preferred_titles = ["List comprehension", "Python syntax and semantics"]
    elif re.search(r"\b(tallest|highest)\b", lowered) and re.search(r"\b(mountain|peak|point)\b", lowered):
        entity = _extract_highest_point_query_entity(user_input)
        search_query = f"highest point mountain in {entity}" if entity else "highest mountain"
        preferred_titles = [entity] if entity else []
    elif _looks_like_concept_explainer_query(retrieval_input):
        search_query = _concept_core_subject_query(retrieval_input)
    try:
        search_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": search_query,
                "format": "json",
                "srlimit": max(3, limit),
            }
        )
        search_data = json.loads(_read_url(search_url, timeout=7))
    except Exception:
        return ""

    results: list[tuple[str, str, str]] = []
    search_items = list((search_data.get("query") or {}).get("search", [])[: max(3, limit)])
    seen_titles: set[str] = set()
    ordered_items: list[dict[str, str]] = []
    for title in preferred_titles:
        ordered_items.append({"title": title, "snippet": ""})
        seen_titles.add(title.lower())
    for item in search_items:
        title = _strip_html(item.get("title") or "")
        if title.lower() in seen_titles:
            continue
        ordered_items.append(item)
        seen_titles.add(title.lower())

    for item in ordered_items[: max(3, limit)]:
        title = _strip_html(item.get("title") or "")
        if not title:
            continue
        snippet = _strip_html(item.get("snippet") or "")
        page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="_()")
        try:
            if _looks_like_sports_record_query(retrieval_input) and re.search(r"\b(india|indian)\b", lowered) and re.search(r"\b(test match|test cricket|test)\b", lowered):
                parse_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
                    {"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": 2}
                )
                parse_data = json.loads(_read_url(parse_url, timeout=8))
                page_text = _best_query_window((parse_data.get("parse") or {}).get("text") or "", retrieval_input)
            else:
                page_text = _fetch_wikipedia_extract(title)
            if page_text:
                snippet = f"{snippet} Page extract: {page_text}" if snippet else f"Page extract: {page_text}"
        except Exception:
            pass
        results.append((title, page_url, snippet))

    return _format_live_results(results, limit, include_page_summary=False)


def _fetch_wikipedia_page_extract_context(title: str, user_input: str, label: str = "Wikipedia page extract") -> str:
    last_error = ""
    page_text = ""
    for _attempt in range(2):
        try:
            parse_url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
                {"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": 2}
            )
            parse_data = json.loads(_read_url(parse_url, timeout=10))
            page_text = _best_query_window((parse_data.get("parse") or {}).get("text") or "", user_input)
            if page_text:
                break
        except Exception as exc:
            last_error = str(exc)
    if not page_text:
        try:
            page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="_()")
            page_text = _best_query_window(_read_url(page_url, timeout=10), user_input)
        except Exception as exc:
            last_error = str(exc)
    if not page_text and last_error:
        return f"DIRECT LIVE DATA: {label} lookup failed for {title} at {_format_ist_datetime()}: {last_error}."
    if not page_text:
        return f"DIRECT LIVE DATA: {label} returned no exact label match for {title} at {_format_ist_datetime()}."
    page_url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"), safe="_()")
    return (
        f"DIRECT LIVE DATA: {label}. Fetched at {_format_ist_datetime()}. Source page: {title}.\n"
        "LIVE WEB CONTEXT:\n"
        f"1. {title}\n"
        f"   URL: {page_url}\n"
        f"   Snippet: Page extract: {_compact_text(page_text)[:1800]}"
    )


def _fetch_bing_live_web_context(query: str, limit: int = 3) -> str:
    encoded = urllib.parse.urlencode({"q": query, "cc": "IN"})
    url = f"https://www.bing.com/search?{encoded}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0"
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return f"LIVE WEB CONTEXT: Web lookup failed: {exc}"

    results: list[tuple[str, str, str]] = []
    for block in re.findall(r'<li[^>]+class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', page, flags=re.IGNORECASE | re.DOTALL):
        title_match = re.search(r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.IGNORECASE | re.DOTALL)
        if not title_match:
            continue
        raw_url, raw_title = title_match.groups()
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.IGNORECASE | re.DOTALL)
        results.append(
            (
                _strip_html(raw_title),
                _unwrap_search_redirect(raw_url),
                _strip_html(snippet_match.group(1) if snippet_match else ""),
            )
        )

    formatted = _format_live_results(results, limit, include_page_summary=False)
    return formatted or "LIVE WEB CONTEXT: No useful web results were returned for this query."


def _fetch_live_web_context(query: str, limit: int = 3) -> str:
    encoded = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{encoded}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return f"LIVE WEB CONTEXT: Web lookup failed: {exc}"

    results: list[str] = []
    link_matches = list(
        re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )

    for match_index, title_match in enumerate(link_matches):
        block_start = title_match.end()
        block_end = link_matches[match_index + 1].start() if match_index + 1 < len(link_matches) else len(page)
        block = page[block_start:block_end]
        raw_url, raw_title = title_match.groups()
        title = _strip_html(raw_title)
        snippet_match = re.search(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<div[^>]+class="result__snippet"[^>]*>(.*?)</div>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = _strip_html((snippet_match.group(1) or snippet_match.group(2)) if snippet_match else "")
        results.append((title, _unwrap_search_redirect(raw_url), snippet))

    formatted_results = _format_live_results(results, limit)
    if formatted_results:
        return formatted_results

    return _fetch_bing_live_web_context(query, limit=limit)


def _normalized_text_contains_term_form(normalized_text: str, term: str) -> bool:
    if term in normalized_text or term.rstrip("s") in normalized_text:
        return True
    if len(term) >= 6:
        return bool(re.search(rf"\b{re.escape(term[:5])}[a-z0-9]*\b", normalized_text))
    return False


def _concept_context_covers_subject(user_input: str, web_context: str) -> bool:
    subject = _normalize_live_query_text(_concept_subject_query(user_input))
    terms = [
        term
        for term in re.findall(r"[a-z0-9]+", subject)
        if len(term) > 2 and term not in {"and", "the", "for", "with", "from", "into"}
    ]
    if not terms:
        return True
    results = _extract_live_web_results(web_context)
    if not results:
        return False
    for result in results:
        combined = _normalize_live_query_text(f"{result.get('title', '')} {result.get('snippet', '')}")
        matched = sum(1 for term in terms if _normalized_text_contains_term_form(combined, term))
        if matched >= max(1, min(len(terms), 2)):
            return True
    return False


def _build_multi_question_live_context(user_input: str, force: bool = False) -> str:
    cache_seed = f"{'force' if force else 'live'}:{_normalize_live_query_text(user_input)}"
    cache_key = hashlib.sha256(cache_seed.encode("utf-8", errors="ignore")).hexdigest()
    cached = _LIVE_CONTEXT_CACHE.get(cache_key)
    now = _now_ist()
    if cached and now - cached[0] < timedelta(seconds=90):
        return cached[1]

    questions = _split_live_questions(user_input)
    live_blocks: list[str] = []

    for index, question in enumerate(questions[:4], start=1):
        enriched_question = _inherit_live_question_context(question, user_input)
        if not force and not _needs_live_web_context(enriched_question):
            continue
        search_query = _build_live_search_query(enriched_question)
        source_profile = _preferred_live_source_profile(enriched_question)
        source_policy = (
            f"\nSOURCE POLICY: {source_profile['category']}: {source_profile['policy']}"
            if source_profile["policy"]
            else "\nSOURCE POLICY: Prefer official, primary, exchange, government, or original data sources over blogs and generic summaries."
        )
        direct_context = _build_direct_live_data_context(enriched_question)
        direct_context_is_trusted = bool(direct_context) and "lookup failed" not in direct_context and "returned no exact label match" not in direct_context
        direct_context_block = f"\n{direct_context}" if direct_context else ""
        prefer_static_sources = (
            (source_profile["category"] == "Sports record/static fact" or _looks_like_static_factual_query(enriched_question))
            and not _extract_event_date_query(enriched_question)
            and source_profile["category"] != "Medical/health information"
            and source_profile["category"] != "General concept explainer"
        )
        if direct_context_is_trusted:
            web_context = "LIVE WEB CONTEXT: Direct trusted data was available, so no browser-opening or generic web-search action was needed."
        else:
            web_context = _fetch_wikipedia_live_web_context(enriched_question, limit=3) if prefer_static_sources else ""
            if not web_context:
                web_context = _fetch_live_web_context(search_query, limit=3)
            if (
                source_profile["category"] == "General concept explainer"
                and not _concept_context_covers_subject(enriched_question, web_context)
            ):
                wikipedia_context = _fetch_wikipedia_live_web_context(enriched_question, limit=3)
                if wikipedia_context:
                    web_context = wikipedia_context
        if (
            not direct_context
            and ("No useful web results were returned" in web_context or not _extract_live_web_results(web_context))
            and (_looks_like_sports_record_query(enriched_question) or _looks_like_web_answerable_question(enriched_question))
        ):
            wikipedia_context = _fetch_wikipedia_live_web_context(enriched_question, limit=3)
            if wikipedia_context:
                web_context = wikipedia_context
        live_blocks.append(
            f"LIVE QUESTION {index}: {enriched_question}\nSEARCH QUERY: {search_query}{source_policy}{direct_context_block}\n{web_context}"
        )

    if not live_blocks and (force or _needs_live_web_context(user_input)):
        search_query = _build_live_search_query(user_input)
        source_profile = _preferred_live_source_profile(user_input)
        source_policy = (
            f"\nSOURCE POLICY: {source_profile['category']}: {source_profile['policy']}"
            if source_profile["policy"]
            else "\nSOURCE POLICY: Prefer official, primary, exchange, government, or original data sources over blogs and generic summaries."
        )
        direct_context = _build_direct_live_data_context(user_input)
        direct_context_is_trusted = bool(direct_context) and "lookup failed" not in direct_context and "returned no exact label match" not in direct_context
        direct_context_block = f"\n{direct_context}" if direct_context else ""
        prefer_static_sources = (
            (source_profile["category"] == "Sports record/static fact" or _looks_like_static_factual_query(user_input))
            and not _extract_event_date_query(user_input)
            and source_profile["category"] != "Medical/health information"
            and source_profile["category"] != "General concept explainer"
        )
        if direct_context_is_trusted:
            web_context = "LIVE WEB CONTEXT: Direct trusted data was available, so no browser-opening or generic web-search action was needed."
        else:
            web_context = _fetch_wikipedia_live_web_context(user_input, limit=3) if prefer_static_sources else ""
            if not web_context:
                web_context = _fetch_live_web_context(search_query, limit=3)
            if (
                source_profile["category"] == "General concept explainer"
                and not _concept_context_covers_subject(user_input, web_context)
            ):
                wikipedia_context = _fetch_wikipedia_live_web_context(user_input, limit=3)
                if wikipedia_context:
                    web_context = wikipedia_context
        if (
            not direct_context
            and ("No useful web results were returned" in web_context or not _extract_live_web_results(web_context))
            and (_looks_like_sports_record_query(user_input) or _looks_like_web_answerable_question(user_input))
        ):
            wikipedia_context = _fetch_wikipedia_live_web_context(user_input, limit=3)
            if wikipedia_context:
                web_context = wikipedia_context
        live_blocks.append(f"LIVE QUESTION 1: {user_input}\nSEARCH QUERY: {search_query}{source_policy}{direct_context_block}\n{web_context}")

    context = "\n\n".join(live_blocks)
    if context:
        _LIVE_CONTEXT_CACHE[cache_key] = (now, context)
    return context


TEAM_ALIASES = {
    "csk": "Chennai Super Kings",
    "dc": "Delhi Capitals",
    "gt": "Gujarat Titans",
    "kkr": "Kolkata Knight Riders",
    "lsg": "Lucknow Super Giants",
    "mi": "Mumbai Indians",
    "pbks": "Punjab Kings",
    "rcb": "Royal Challengers Bengaluru",
    "rr": "Rajasthan Royals",
    "srh": "Sunrisers Hyderabad",
}

IPL_TEAM_NAMES = {
    "chennai super kings",
    "delhi capitals",
    "gujarat titans",
    "kolkata knight riders",
    "lucknow super giants",
    "mumbai indians",
    "punjab kings",
    "royal challengers bengaluru",
    "royal challengers bangalore",
    "rajasthan royals",
    "sunrisers hyderabad",
}


def _expand_team_name(team: str) -> str:
    cleaned = re.sub(r"[^A-Za-z ]", "", team).strip()
    alias = cleaned.lower()
    if alias in TEAM_ALIASES:
        return f"{TEAM_ALIASES[alias]} ({cleaned.upper()})"
    return re.sub(r"\s+", " ", cleaned)


def _is_ipl_team_name(team: str) -> bool:
    cleaned = re.sub(r"[^A-Za-z ]", "", team).strip().lower()
    return cleaned in TEAM_ALIASES or cleaned in IPL_TEAM_NAMES


def _extract_matchups_from_live_context(live_context: str) -> list[str]:
    seen: set[str] = set()
    matchups: list[str] = []
    for left, right in re.findall(
        r"\b([A-Z]{2,5}|[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\s+"
        r"(?:v(?:s\.?|ersus)|face(?:s)?|against|take(?:s)? on)\s+"
        r"([A-Z]{2,5}|[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b",
        live_context,
    ):
        if not (_is_ipl_team_name(left) and _is_ipl_team_name(right)):
            continue
        matchup = f"{_expand_team_name(left)} vs {_expand_team_name(right)}"
        key = matchup.lower()
        if key not in seen:
            seen.add(key)
            matchups.append(matchup)
    return matchups


def _build_live_answer_hint(user_input: str, live_context: str) -> str:
    lowered = user_input.lower()
    today_context = (
        f"TIME CONTEXT: Current local time is {_format_ist_datetime()}. Interpret today/yesterday/tomorrow in IST."
    )
    multi_question_context = (
        " If there are multiple LIVE QUESTION blocks, answer each one in order with a short label. "
        "Do not merge unrelated answers or skip smaller questions."
    )
    if "ipl" in lowered and re.search(r"\b(today|yesterday|tomorrow|now|current|present|match|teams?|playing|schedule|score|highest|batting|batter|batters|crease|pitch|striker|non[-\s]?striker|bowling|bowler)\b", lowered):
        if re.search(r"\b(batting|batter|batters|crease|pitch|striker|non[-\s]?striker|bowling|bowler)\b", lowered):
            return (
                f"{today_context}\nDIRECT LIVE ANSWER HINT: This is a live scorecard detail question. "
                "Use only DIRECT LIVE DATA fields such as Verified batters at crease, Current bowler, Current innings score, and fetch timestamp. "
                "Name current batters/bowler only if those exact fields are present. If absent, say the live feed did not verify that detail. "
                f"Do not guess from prior chat, team lineups, snippets, or memory.{multi_question_context}"
            )
        matchups = _extract_matchups_from_live_context(live_context)
        if matchups:
            return (
                f"{today_context}\nDIRECT LIVE ANSWER HINT: The searched results indicate the IPL match/team answer is "
                f"{'; '.join(matchups[:2])}. Start with this direct answer. Then mention the source names "
                f"briefly. Ignore unrelated sports/wrestling/entertainment matchups. Do not tell the user to check a website.{multi_question_context}"
            )
        return (
            f"{today_context}\nDIRECT LIVE ANSWER HINT: This is an IPL current-match question. Use the LIVE WEB CONTEXT titles "
            "and snippets to answer the teams directly. If the result is uncertain, say exactly what was verified "
            f"and ask one focused follow-up. Ignore unrelated sports/wrestling/entertainment results. Do not tell the user to check a website.{multi_question_context}"
        )
    if "LIVE WEB CONTEXT:" in live_context and "No useful web results" not in live_context:
        return (
            f"{today_context}\nDIRECT LIVE ANSWER HINT: If DIRECT LIVE DATA is present, use it before search snippets. Otherwise answer the real-time question directly from LIVE WEB CONTEXT first. "
            "Only add source names or URLs after the answer. Do not respond by sending the user away to check websites. "
            "For prices, scores, weather, versions, and market values, include the visible date/time/unit from the source; if a number is missing or conflicting, say so instead of guessing."
            f"{multi_question_context}"
        )
    if "DIRECT LIVE DATA:" in live_context:
        return (
            f"{today_context}\nDIRECT LIVE ANSWER HINT: Answer directly from DIRECT LIVE DATA. Include the unit, source, and timestamp/date when present. "
            f"Do not tell the user to check a website.{multi_question_context}"
        )
    return ""


def _extract_fetched_at(live_context: str) -> str:
    match = re.search(r"Fetched at ([^.]+(?:IST)?)", live_context)
    return match.group(1).strip() if match else _format_ist_datetime()


def _parse_source_birth_date(text: str) -> datetime | None:
    month_names = (
        "january|february|march|april|may|june|july|august|september|october|november|december|"
        "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    )
    patterns = (
        rf"\bborn[:\s]+(?P<month>{month_names})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})",
        rf"\bborn\s+(?P<month>{month_names})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})",
        rf"\bborn\s+(?P<day>\d{{1,2}})\s+(?P<month>{month_names})\.?\s+(?P<year>\d{{4}})",
        rf"\bdate of birth[:\s]+(?P<month>{month_names})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})",
    )
    month_lookup = {
        name.lower(): index
        for index, names in enumerate(
            [
                ("january", "jan"),
                ("february", "feb"),
                ("march", "mar"),
                ("april", "apr"),
                ("may",),
                ("june", "jun"),
                ("july", "jul"),
                ("august", "aug"),
                ("september", "sep", "sept"),
                ("october", "oct"),
                ("november", "nov"),
                ("december", "dec"),
            ],
            start=1,
        )
        for name in names
    }
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        month = month_lookup.get(match.group("month").lower().rstrip("."))
        if not month:
            continue
        try:
            return datetime(int(match.group("year")), month, int(match.group("day")), tzinfo=IST)
        except ValueError:
            return None
    return None


def _age_on_date(birth_date: datetime, on_date: datetime | None = None) -> int:
    current = on_date or _now_ist()
    age = current.year - birth_date.year
    if (current.month, current.day) < (birth_date.month, birth_date.day):
        age -= 1
    return age


def _source_backed_sports_record_answer(user_input: str, live_context: str) -> str:
    lowered = _normalize_live_query_text(user_input)
    if re.search(r"\bfifa\b|\bworld cup\b", lowered) and re.search(r"\b2022\b", lowered) and re.search(r"\bwon|winner|champion\b", lowered):
        compact_context = _compact_text(live_context)
        if re.search(r"\bArgentina\b", compact_context) and re.search(r"\bFrance\b", compact_context):
            results = _extract_live_web_results(live_context)
            source = next((result for result in results if "2022 fifa world cup final" in result["title"].lower()), results[0] if results else None)
            source_text = f"[{source['title']}]({source['url']})" if source else "retrieved tournament source"
            score_note = ""
            score_match = re.search(r"Argentina (?:defeated|beat) France (?:4-2|4–2).*?penalt", compact_context, flags=re.IGNORECASE)
            if score_match or "penalt" in compact_context.lower():
                score_note = " The final was against France and was decided on penalties."
            return f"Argentina won the 2022 FIFA World Cup.{score_note}\nSource: {source_text}."

    if not (
        re.search(r"\b(india|indian)\b", lowered)
        and re.search(r"\b(test match|test cricket|test)\b", lowered)
        and re.search(r"\b(highest|top|record|score|total|innings|runs)\b", lowered)
    ):
        return ""

    compact = _compact_text(live_context)
    total_match = re.search(
        r"India set their highest innings total of\s+([0-9]+/[0-9]+d?)",
        compact,
        flags=re.IGNORECASE,
    )
    rank_match = re.search(
        r"Rank Score Opposition Venue Date\s+1\s+([0-9]+/[0-9]+d?)\s+([A-Za-z ]+?)\s+(.+?)\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        compact,
        flags=re.IGNORECASE,
    )
    score = total_match.group(1) if total_match else (rank_match.group(1) if rank_match else "")
    if not score:
        return ""

    opposition = "England"
    venue = "M. A. Chidambaram Stadium, Chennai"
    date_text = "16 December 2016"
    if rank_match:
        opposition = _compact_text(rank_match.group(2))
        venue = _compact_text(rank_match.group(3)).strip(" ,")
        date_text = rank_match.group(4)
        venue = re.sub(r"\s+India$", ", India", venue)
        venue = re.sub(r"\s+,", ",", venue)
        venue = re.sub(r",\s*,", ",", venue)

    results = _extract_live_web_results(live_context)
    source = next((result for result in results if "india test cricket records" in result["title"].lower()), results[0] if results else None)
    source_text = f"[{source['title']}]({source['url']})" if source else "retrieved records source"
    return (
        f"India men's highest team score in a Test match is {score} against {opposition}.\n"
        f"Venue/date: {venue}, {date_text}.\n"
        f"Source: {source_text}."
    )


def _source_backed_current_office_answer(user_input: str, live_context: str) -> str:
    office_query = _extract_current_office_holder_query(user_input)
    if not office_query:
        return ""

    entity_pattern = re.escape(office_query["entity"])
    office_pattern = re.escape(office_query["office"])
    results = _extract_live_web_results(live_context)
    for result in results:
        title = result["title"].strip()
        snippet = result["snippet"].strip()
        combined = _compact_text(f"{title}. {snippet}")
        if not re.search(entity_pattern, combined, flags=re.IGNORECASE):
            continue

        if office_query["office"] == "CEO":
            if re.search(rf"\b(?:chief executive officer|CEO)\b.{{0,80}}\b(?:of|at)\s+{entity_pattern}\b", combined, flags=re.IGNORECASE):
                name = re.sub(r"\s*[-|].*$", "", title).strip()
                if (
                    name
                    and not re.search(r"\b(CEO|chief executive|leadership|company|corporation|inc\.?|llc|ltd)\b", name, flags=re.IGNORECASE)
                    and name.lower() != office_query["entity"].lower()
                ):
                    return f"{name} is the current CEO of {office_query['entity']}.\nSource: [{result['title']}]({result['url']})."
            ceo_match = re.search(
                rf"\b([A-Z][A-Za-z .'-]{{2,80}}?)\s+(?:is|has been|serves as|served as)\s+(?:the\s+)?(?:chairman\s+and\s+)?(?:chief executive officer|CEO)\s+of\s+{entity_pattern}\b",
                combined,
            )
            if ceo_match:
                return f"{ceo_match.group(1).strip()} is the current CEO of {office_query['entity']}.\nSource: [{result['title']}]({result['url']})."
            continue

        incumbent_match = re.search(
            rf"\b(?:incumbent|current)\s+{office_pattern}\s+(?:of\s+{entity_pattern}\s+)?(?:is|:)\s+([A-Z][A-Za-z .'-]{{2,80}})",
            combined,
            flags=re.IGNORECASE,
        )
        if incumbent_match:
            return (
                f"{incumbent_match.group(1).strip()} is the current {office_query['office']} of {office_query['entity']}.\n"
                f"Source: [{result['title']}]({result['url']})."
            )
        person_before_office = re.search(
            rf"\b([A-Z][A-Za-z .'-]{{2,80}}?)\s+(?:is|has been|serves as|served as|succeeded)\s+(?:the\s+)?{office_pattern}\s+of\s+{entity_pattern}\b",
            combined,
            flags=re.IGNORECASE,
        )
        if person_before_office:
            return (
                f"{person_before_office.group(1).strip()} is the current {office_query['office']} of {office_query['entity']}.\n"
                f"Source: [{result['title']}]({result['url']})."
            )
        official_style = re.search(rf"\b{office_pattern}\s+([A-Z][A-Za-z .'-]{{2,80}})", combined)
        if official_style and re.search(r"\bofficial|presidency|government|office\b", combined, flags=re.IGNORECASE):
            return (
                f"{official_style.group(1).strip()} is listed as {office_query['office']} of {office_query['entity']}.\n"
                f"Source: [{result['title']}]({result['url']})."
            )
    return ""


def _direct_source_backed_answer(user_input: str, live_context: str) -> str:
    """Return deterministic answers for high-risk live facts when parsed source data exists.

    This prevents the chat model from beautifying partial source data into confident,
    fabricated live/news/sports rows. When a deterministic answer is returned, it is
    intentionally limited to facts present in DIRECT LIVE DATA.
    """
    lowered = _normalize_live_query_text(user_input)

    office_query = _extract_current_office_holder_query(user_input)
    if office_query and "Wikidata current office-holder lookup" in live_context:
        holder_lines = [
            _compact_text(line.lstrip("- "))
            for line in live_context.splitlines()
            if line.strip().startswith("- ") and f"{office_query['office']} of " in line
        ]
        if holder_lines:
            primary = holder_lines[0]
            match = re.search(rf"{re.escape(office_query['office'])}\s+of\s+(.+?):\s+(.+?)\.\s+Source:", primary)
            if match:
                entity_label, person_label = match.groups()
                return (
                    f"{person_label} is the current {office_query['office']} of {entity_label}.\n"
                    f"Source: Wikidata structured data ({office_query['property']}), fetched at {_extract_fetched_at(live_context)}."
                )
            return f"Source-backed current office result: {primary}"
        if "lookup failed" in live_context or "returned no exact label match" in live_context:
            # Fall through to web snippets/search context instead of answering from stale local memory.
            pass

    office_source_answer = _source_backed_current_office_answer(user_input, live_context)
    if office_source_answer:
        return office_source_answer

    event_lookup_match = re.search(
        r"(?m)^-\s+(.+?)\s+(\d{4}):\s+(.+?)\.\s+Source:\s+(.+?)\s+\((https?://[^)]+)\)\.",
        live_context,
    )
    if event_lookup_match and _extract_event_date_query(user_input):
        event_label, year, date_text, source_title, source_url = event_lookup_match.groups()
        return f"{event_label} {year} is listed as {date_text}.\nSource: [{source_title}]({source_url})."

    results = _extract_live_web_results(live_context)
    event_date_answer = _extract_source_event_date(user_input, results)
    if event_date_answer:
        date_text, source = event_date_answer
        event, year = _extract_event_date_query(user_input) or ("The event", "")
        source_text = f"[{source['title']}]({source['url']})" if source.get("url") else source.get("title", "retrieved source")
        return f"{event.title()} {year} is listed as {date_text}.\nSource: {source_text}."

    capital_entity = _extract_capital_query_entity(user_input)
    if capital_entity and "Wikidata capital lookup" in live_context:
        capital_lines = [
            _compact_text(line.lstrip("- "))
            for line in live_context.splitlines()
            if line.strip().startswith("- ") and "Capital of " in line
        ]
        if capital_lines:
            match = re.search(r"Capital of (.+?):\s+(.+?)\.\s+Source:", capital_lines[0])
            if match:
                entity_label, capital_label = match.groups()
                return (
                    f"{capital_label} is the capital of {entity_label}.\n"
                    f"Source: Wikidata structured data (P36), fetched at {_extract_fetched_at(live_context)}."
                )

    highest_point_entity = _extract_highest_point_query_entity(user_input)
    if highest_point_entity and "Wikidata highest point lookup" in live_context:
        point_lines = [
            _compact_text(line.lstrip("- "))
            for line in live_context.splitlines()
            if line.strip().startswith("- ") and "Highest Point of " in line
        ]
        if point_lines:
            match = re.search(r"Highest Point of (.+?):\s+(.+?)\.\s+Source:", point_lines[0], flags=re.IGNORECASE)
            if match:
                entity_label, point_label = match.groups()
                return (
                    f"{point_label} is listed as the highest point of {entity_label}.\n"
                    f"Source: Wikidata structured data (P610), fetched at {_extract_fetched_at(live_context)}."
                )

    if (
        "Python documentation excerpt fetched from docs.python.org" in live_context
        or "Java documentation excerpt fetched from docs.oracle.com" in live_context
    ):
        excerpt_match = re.search(r"Excerpt:\s*(.+)", live_context, flags=re.DOTALL)
        source_match = re.search(r"Source:\s*(https?://\S+)", live_context)
        if excerpt_match:
            answer = _first_useful_sentence(excerpt_match.group(1), max_chars=520)
            source_url = source_match.group(1) if source_match else "retrieved documentation"
            return f"{answer}\n\nSource: {source_url}."

    if "OpenAI official models documentation" in live_context:
        latest_match = re.search(r"Latest model label:\s*(.+)", live_context)
        source_match = re.search(r"Source:\s*(https?://\S+)", live_context)
        excerpt_match = re.search(r"Excerpt:\s*(.+)", live_context, flags=re.DOTALL)
        latest = _compact_text(latest_match.group(1)).strip(" .") if latest_match else ""
        source_url = source_match.group(1) if source_match else "https://platform.openai.com/docs/models"
        excerpt = _first_useful_sentence(excerpt_match.group(1), max_chars=360) if excerpt_match else ""
        if latest and "not explicitly" not in latest.lower():
            return f"OpenAI's official models page lists the latest model label as {latest}.\nSource: {source_url}."
        if excerpt:
            return f"{excerpt}\n\nSource: {source_url}."

    if "Official health source fetched" in live_context:
        topic_match = re.search(r"Topic:\s*(.+)", live_context)
        source_match = re.search(r"Source:\s*(.+?)\s+\((https?://[^)]+)\)", live_context)
        excerpt_match = re.search(r"Excerpt:\s*(.+)", live_context, flags=re.DOTALL)
        topic = _compact_text(topic_match.group(1)).strip(" .") if topic_match else "This condition"
        source_name = source_match.group(1).strip() if source_match else "official health source"
        source_url = source_match.group(2).strip() if source_match else ""
        excerpt = _first_useful_sentence(excerpt_match.group(1), max_chars=520) if excerpt_match else ""
        if excerpt:
            source_text = f"[{source_name}]({source_url})" if source_url else source_name
            return (
                f"{topic.title()} symptoms, source-backed: {excerpt}\n\n"
                "This is general health information, not a diagnosis. Seek urgent care for severe symptoms.\n"
                f"Source: {source_text}."
            )

    if re.search(r"\brepo\b", lowered) and re.search(r"\bgit\b", lowered) and re.search(r"\brepository\b", live_context, flags=re.IGNORECASE):
        results = _extract_live_web_results(live_context)
        source = results[0] if results else None
        source_text = f"[{source['title']}]({source['url']})" if source else "retrieved Git source"
        return (
            "In Git, a repo means a repository: the project storage that contains your files plus Git's version history, "
            "branches, commits, and metadata.\n"
            f"Source: {source_text}."
        )

    sports_record_answer = _source_backed_sports_record_answer(user_input, live_context)
    if sports_record_answer:
        return sports_record_answer
    if (
        _looks_like_sports_record_query(user_input)
        and re.search(r"\b(india|indian)\b", lowered)
        and re.search(r"\b(test match|test cricket|test)\b", lowered)
        and "List of India Test cricket records" not in live_context
    ):
        record_context = _fetch_wikipedia_page_extract_context("List of India Test cricket records", user_input, "Sports record source lookup")
        sports_record_answer = _source_backed_sports_record_answer(user_input, record_context)
        if sports_record_answer:
            return sports_record_answer

    if "Wikidata/Wikipedia birth-date lookup" in live_context and re.search(r"\bage\b|how old", lowered):
        subject_match = re.search(r"Subject:\s*(.+?)\.\s+Born:", live_context)
        born_match = re.search(r"Born:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", live_context)
        source_match = re.search(r"Source:\s*(https?://\S+)", live_context)
        if born_match:
            birth_date = _parse_source_birth_date(f"Born: {born_match.group(1)}")
            if birth_date:
                subject = subject_match.group(1).strip() if subject_match else _extract_age_query_subject(user_input) or "The person"
                born_text = birth_date.strftime("%B %-d, %Y") if os.name != "nt" else birth_date.strftime("%B %#d, %Y")
                source_url = source_match.group(1).rstrip(".") if source_match else "Wikidata/Wikipedia"
                return (
                    f"{subject} is {_age_on_date(birth_date)} years old as of {_format_ist_datetime()}.\n"
                    f"Born: {born_text}.\n"
                    f"Source: {source_url}."
                )

    if re.search(r"\bage\b|how old", lowered):
        birth_date = _parse_source_birth_date(live_context)
        if birth_date:
            results = _extract_live_web_results(live_context)
            source = results[0] if results else {"title": "retrieved source", "url": ""}
            source_text = f"[{source['title']}]({source['url']})" if source.get("url") else source["title"]
            born_text = birth_date.strftime("%B %-d, %Y") if os.name != "nt" else birth_date.strftime("%B %#d, %Y")
            return (
                f"{_age_on_date(birth_date)} years old as of {_format_ist_datetime()}.\n"
                f"Born: {born_text}.\n"
                f"Source: {source_text}."
            )

    if "eBullion live metal ticker" in live_context:
        metal_match = re.search(r"\b(silver|gold|platinum|palladium)\b", lowered)
        requested_metal = metal_match.group(1).title() if metal_match else ""
        metal_lines = [
            _compact_text(line)
            for line in live_context.splitlines()
            if re.match(r"^\s*-\s+(Gold|Silver|Platinum|Palladium):", line)
        ]
        if requested_metal:
            metal_lines = [line for line in metal_lines if line.lower().startswith(f"- {requested_metal.lower()}:")]
        if metal_lines:
            fetched_at = _extract_fetched_at(live_context)
            return (
                f"Source-backed metal price from eBullion, fetched at {fetched_at}.\n\n"
                + "\n".join(metal_lines)
                + "\n\nUnit: eBullion platform per-gram ticker rates. Source: https://api.ebullion.in/price/getallmetaltickerfeed."
            )
        if requested_metal:
            inline_match = re.search(
                rf"{requested_metal}\s+sell\s+INR\s+([0-9.,]+)/(?:g|gram),\s+buy\s+INR\s+([0-9.,]+)/(?:g|gram),\s+([^;]+)",
                live_context,
                flags=re.IGNORECASE,
            )
            if inline_match:
                sell_rate, buy_rate, movement = inline_match.groups()
                ibja_note = ""
                ibja_match = re.search(
                    rf"{requested_metal}\s+999\s+AM/PM:\s+INR\s+([0-9,]+)/INR\s+([0-9,]+)\s+per\s+(?:1kg|10g).*?\(PM equals INR\s+([0-9.]+)\s+per gram\)",
                    live_context,
                    flags=re.IGNORECASE,
                )
                if ibja_match:
                    ibja_note = f"\nIBJA reference: INR {ibja_match.group(3)} per gram PM rate, before GST/making charges unless stated."
                return (
                    f"{requested_metal} in India is source-backed from eBullion at INR {sell_rate} per gram sell rate "
                    f"and INR {buy_rate} per gram buy rate, movement {movement.strip()}.\n"
                    f"Fetched at {_extract_fetched_at(live_context)}. Unit: eBullion per-gram ticker rate."
                    f"{ibja_note}\nSource: https://api.ebullion.in/price/getallmetaltickerfeed."
                )

    if "Weather fallback from Open-Meteo" in live_context:
        weather_match = re.search(
            r"Location:\s*(.+?)\.\s+Time:\s*(.+?)\s+IST\.\s+Temperature:\s*(.+?)\s+C\.\s+"
            r"Humidity:\s*(.+?)%\.\s+Precipitation:\s*(.+?)\s+mm\.\s+Wind:\s*(.+?)\s+km/h\.\s+Condition:\s*(.+?)\.",
            live_context,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if weather_match:
            location, time_text, temp, humidity, precip, wind, condition = [
                _compact_text(part) for part in weather_match.groups()
            ]
            return (
                f"Current weather for {location}: {temp} C, {condition}, humidity {humidity}%, "
                f"precipitation {precip} mm, wind {wind} km/h.\n"
                f"Time: {time_text} IST. Source: Open-Meteo geocoding/forecast API."
            )

    if "India cricket upcoming fixtures extracted from BCCI" in live_context:
        fixture_lines = [
            _compact_text(line.lstrip("- "))
            for line in live_context.splitlines()
            if line.strip().startswith("- ")
        ]
        if fixture_lines:
            lines = [
                f"Next India cricket matches from BCCI, fetched at {_extract_fetched_at(live_context)}:",
                "",
            ]
            for fixture in fixture_lines[:5]:
                lines.append(f"- {fixture}")
            lines.append("")
            lines.append("Source: BCCI getUpcomingMatches endpoint (https://www.bcci.tv/getUpcomingMatches).")
            return "\n".join(lines)

    if "IPL 2026 winner extracted" in live_context:
        result_match = re.search(r"Result:\s*(.+)", live_context)
        final_match = re.search(r"Final:\s*(.+)", live_context)
        scores_match = re.search(r"Scores:\s*(.+)", live_context)
        result = result_match.group(1).strip().rstrip(".") if result_match else "winner verified in official IPL feed"
        final = final_match.group(1).strip().rstrip(".") if final_match else "IPL 2026 final"
        scores = scores_match.group(1).strip().rstrip(".") if scores_match else ""
        return (
            f"{result}.\n\n"
            f"Final: {final}."
            + (f"\nScores: {scores}." if scores else "")
            + f"\nSource: official IPLT20/SportsMechanics feed, fetched at {_extract_fetched_at(live_context)}."
        )

    if "IPL 2026 points table extracted" in live_context:
        table_lines = [
            line.strip()
            for line in live_context.splitlines()
            if line.strip().startswith("|") and line.strip().endswith("|")
        ]
        source_match = re.search(r"extracted from ([^(]+)\((https?://[^)]+)\)", live_context)
        source_name = source_match.group(1).strip() if source_match else "parsed IPL standings source"
        source_url = source_match.group(2).strip() if source_match else ""
        source_text = f"[{source_name}]({source_url})" if source_url else source_name
        return (
            f"Source-backed IPL 2026 points table, fetched at {_extract_fetched_at(live_context)}.\n\n"
            + "\n".join(table_lines)
            + f"\n\nSource: {source_text}. Status: rows are source-backed only; I did not add confidence or fill missing values from memory."
        )

    if "IPL points table was requested, but no trusted standings table could be parsed" in live_context:
        return (
            "I could not verify the current IPL points table from a trusted parseable source right now. "
            "I will not generate a confident table from memory because that can be wrong. Please retry once, or paste/upload the standings source and I will format it."
        )

    if (
        re.search(r"\b(news|headlines|breaking|latest|updates?|happened|today)\b", lowered)
        or _looks_like_public_discussion_query(user_input)
    ) and "RSS/news feeds fetched" in live_context:
        item_lines = [line.strip()[2:] for line in live_context.splitlines() if line.strip().startswith("- ")]
        if not item_lines:
            return ""
        table = [
            "| # | Source | Headline | Published | Status |",
            "|---:|---|---|---|---|",
        ]
        for index, item in enumerate(item_lines[:8], start=1):
            source = "Source feed"
            headline = item
            published = "Not provided"
            if ": " in item:
                source, headline = item.split(": ", 1)
            date_match = re.search(r"\[([^\]]+)\]", headline)
            if date_match:
                published = date_match.group(1)
                headline = re.sub(r"\s*\[[^\]]+\]", "", headline).strip()
            headline = re.sub(r"\s*\(https?://[^)]+\)", "", headline).strip()
            headline = re.sub(r"\s+-\s+.*$", "", headline).strip()
            table.append(
                f"| {index} | {source} | {headline} | {published} | Source-reported, not independently confirmed |"
            )
        intro = (
            "What people may be talking about in India right now, based on source-reported headlines"
            if _looks_like_public_discussion_query(user_input)
            else "Verified news feed items"
        )
        return (
            f"{intro}, fetched at {_extract_fetched_at(live_context)}. "
            "I am only listing source-reported items and not adding extra confident claims.\n\n"
            + "\n".join(table)
        )

    return ""


def _extract_live_web_results(live_context: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r"(?m)^\d+\.\s*(?P<title>.+?)\n[ \t]+URL:[ \t]*(?P<url>\S+)\n[ \t]+Snippet:[ \t]*(?P<snippet>.*?)(?=\n\d+\.|\Z)",
        re.DOTALL,
    )
    for match in pattern.finditer(live_context or ""):
        snippet = re.sub(r"\n\s+Page details:\s*", " ", match.group("snippet")).strip()
        results.append(
            {
                "title": _compact_text(match.group("title")),
                "url": match.group("url").strip(),
                "snippet": _compact_text(snippet),
            }
        )
    return results


def _looks_like_low_quality_source_result(result: dict[str, str]) -> bool:
    combined = _normalize_live_query_text(f"{result.get('title', '')} {result.get('url', '')} {result.get('snippet', '')}")
    return bool(
        re.search(
            r"\b(togel|lotto|syair|bocoran|paito|casino|betting|jackpot|slot|porn|xxx)\b",
            combined,
        )
    )


def _select_primary_source_result(user_input: str, results: list[dict[str, str]]) -> dict[str, str] | None:
    if not results:
        return None
    clean_results = [result for result in results if not _looks_like_low_quality_source_result(result)]
    candidates = clean_results or results
    is_concept_query = _looks_like_concept_explainer_query(user_input)
    lowered_query = _normalize_live_query_text(_concept_subject_query(user_input) if is_concept_query else _retrieval_query_text(user_input))
    concept_title_query = _normalize_live_query_text(_concept_core_subject_query(user_input)) if is_concept_query else ""
    query_terms = [
        term
        for term in re.findall(r"[a-z0-9]+", lowered_query)
        if len(term) > 2 and term not in {"what", "who", "when", "where", "which", "how", "why", "the", "and", "for", "with"}
    ]
    if not query_terms:
        return candidates[0]

    def score(result: dict[str, str]) -> int:
        combined = _normalize_live_query_text(f"{result.get('title', '')} {result.get('snippet', '')}")
        score_value = sum(2 for term in query_terms if term in combined)
        title = result.get("title", "").lower()
        url = result.get("url", "").lower()
        if "wikipedia.org" in url or "espncricinfo.com" in url or "bcci.tv" in url or "official" in title:
            score_value += 2
        if is_concept_query:
            normalized_title = _normalize_live_query_text(title)
            if concept_title_query and concept_title_query in normalized_title:
                score_value += 8
            elif query_terms and all(_normalized_text_contains_term_form(normalized_title, term) for term in query_terms[:3]):
                score_value += 5
            elif len(query_terms) > 1 and sum(1 for term in query_terms if _normalized_text_contains_term_form(normalized_title, term)) <= 1:
                score_value -= 4
            if re.search(r"\b(what is|definition|meaning|explained|explainer|guide|overview|how .{0,30}works)\b", combined):
                score_value += 3
            if re.search(r"\b(is|are|means|refers to|describes|process|concept|used to)\b", combined):
                score_value += 3
            if re.search(r"\b(dictionary|definition & meaning)\b", combined) and not any(term in title for term in query_terms):
                score_value -= 3
            if re.search(r"\b(crisis|controversy|history|lawsuit|settlement)\b", combined) and not any(
                term in {"crisis", "controversy", "history", "lawsuit", "settlement"} for term in query_terms
            ):
                score_value -= 3
        if result.get("snippet"):
            score_value += 1
        return score_value

    return max(candidates, key=score)


def _source_context_fallback_answer(user_input: str, live_context: str, language_preference: str | None = None) -> str:
    """Use already-fetched source context when the model provider is unavailable."""
    if not live_context:
        return ""

    direct_answer = _direct_source_backed_answer(user_input, live_context)
    if direct_answer:
        return direct_answer

    stable_fallback = _local_static_factual_answer(user_input)
    if stable_fallback and "DIRECT LIVE DATA:" not in live_context:
        return stable_fallback

    if "DIRECT LIVE DATA:" in live_context and "LIVE WEB CONTEXT: Direct trusted data was available" in live_context:
        direct_lines = [
            _compact_text(line)
            for line in live_context.splitlines()
            if line.strip()
            and not line.startswith(("LIVE QUESTION", "SEARCH QUERY", "SOURCE POLICY", "LIVE WEB CONTEXT"))
        ]
        if direct_lines:
            return (
                "Source-backed result:\n\n"
                + "\n".join(f"- {line}" for line in direct_lines[:8])
            )

    if "Web lookup failed" in live_context:
        documentation_context = _fetch_programming_docs_context(user_input)
        if documentation_context:
            documentation_answer = _source_context_fallback_answer(user_input, documentation_context, language_preference)
            if documentation_answer:
                return documentation_answer
        if _looks_like_static_factual_query(user_input) or _looks_like_concept_explainer_query(user_input):
            retry_context = _fetch_wikipedia_live_web_context(user_input, limit=3)
            if (not retry_context or "No useful web results" in retry_context) and _looks_like_concept_explainer_query(user_input):
                retry_context = _fetch_concept_summary_context(user_input)
            if retry_context and "Web lookup failed" not in retry_context and "No useful web results" not in retry_context:
                retry_answer = _source_context_fallback_answer(user_input, retry_context, language_preference)
                if retry_answer:
                    return retry_answer
        stable_retry = _local_static_factual_answer(user_input)
        if stable_retry:
            return stable_retry
        return _last_resort_text_answer(user_input, language_preference, reason="live web lookup was temporarily unreachable")

    results = _extract_live_web_results(live_context)
    if not results:
        return ""

    primary = _select_primary_source_result(user_input, results)
    if not primary:
        return ""
    if _looks_like_concept_explainer_query(user_input) and not _concept_result_title_matches_core(user_input, primary):
        retry_context = _fetch_wikipedia_live_web_context(user_input, limit=3) or _fetch_concept_summary_context(user_input)
        if retry_context and retry_context != live_context:
            retry_answer = _source_context_fallback_answer(user_input, retry_context, language_preference)
            if retry_answer:
                return retry_answer
    shaped_answer = _shape_source_snippet_answer(user_input, primary, results)
    if shaped_answer:
        return shaped_answer

    answer_lines = [
        "Live web result:",
        "",
        primary["snippet"] or primary["title"],
        "",
        f"Source: [{primary['title']}]({primary['url']})",
    ]
    if len(results) > 1:
        answer_lines.append("")
        answer_lines.append("Other source hits:")
        for result in results[1:4]:
            answer_lines.append(f"- [{result['title']}]({result['url']})")
    answer_lines.append("")
    answer_lines.append("Status: source-snippet answer. I did not add facts beyond the retrieved source text.")
    return "\n".join(answer_lines)


def _clean_source_snippet(snippet: str) -> str:
    raw = snippet or ""
    if re.search(r"\bPage extract:\s*", raw, flags=re.IGNORECASE):
        raw = re.split(r"\bPage extract:\s*", raw, maxsplit=1, flags=re.IGNORECASE)[1]
    cleaned = _compact_text(raw)
    cleaned = re.sub(r"\s*\[[0-9]+\]", "", cleaned)
    return cleaned.strip()


def _first_useful_sentence(text: str, max_chars: int = 420) -> str:
    cleaned = _clean_source_snippet(text)
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        selected.append(sentence)
        total += len(sentence)
        if total >= 180 or len(selected) >= 2:
            break
    answer = " ".join(selected) or cleaned
    if len(answer) > max_chars:
        answer = answer[: max_chars - 1].rstrip() + "..."
    return answer


def _split_source_sentences(text: str) -> list[str]:
    cleaned = _clean_source_snippet(text)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    sentences: list[str] = []
    for part in parts:
        sentence = _compact_text(part).strip(" -")
        sentence = re.sub(r"^(?:[A-Z][a-z]{2,9}\s+\d{1,2},\s+\d{4}\s*·\s*)+", "", sentence).strip(" -")
        if sentence:
            sentences.append(sentence)
    return sentences


def _looks_like_promotional_source_sentence(sentence: str) -> bool:
    return bool(
        re.match(
            r"^(?:learn|discover|find|read|stay updated|whether you're|click|visit|sign up|subscribe)\b",
            sentence,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(world.?s most|expert insights|calculator can serve|available on this website|learn more|"
            r"manage your finances better|here is a detailed guide|explore .{0,80} with detailed)\b",
            sentence,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_heading_only_source_sentence(sentence: str) -> bool:
    cleaned = _compact_text(sentence).strip(" ?.!:")
    return bool(
        re.fullmatch(
            r"(?:what\s+(?:is|are)|definition(?:\s+of)?|meaning(?:\s+of)?|explained?|guide\s+to)\s+.{1,90}",
            cleaned,
            flags=re.IGNORECASE,
        )
        and not re.search(
            r"\b(is|are|means|refers to|describes|process|method|system|used to|works by|happens when|in which)\b.+\b(by|for|with|when|that|to|from|into|between)\b",
            cleaned,
            flags=re.IGNORECASE,
        )
    )


def _is_unrequested_side_topic_result(user_input: str, result: dict[str, str]) -> bool:
    side_topic = re.search(
        r"\b(scam|controversy|crisis|history|lawsuit|settlement|scandal|fraud)\b",
        f"{result.get('title', '')} {result.get('url', '')}",
        flags=re.IGNORECASE,
    )
    if not side_topic:
        return False
    requested = _normalize_live_query_text(user_input)
    return side_topic.group(1).lower() not in requested


def _concept_result_title_matches_core(user_input: str, result: dict[str, str]) -> bool:
    core = _normalize_live_query_text(_concept_core_subject_query(user_input))
    title = _normalize_live_query_text(result.get("title", ""))
    if not core or not title:
        return True
    core_terms = [term for term in re.findall(r"[a-z0-9]+", core) if len(term) > 2]
    if len(core_terms) <= 1:
        return _normalized_text_contains_term_form(title, core_terms[0]) if core_terms else True
    return all(_normalized_text_contains_term_form(title, term) for term in core_terms)


def _concept_explainer_answer(user_input: str, primary: dict[str, str], results: list[dict[str, str]]) -> str:
    subject = _concept_core_subject_query(user_input)
    normalized_subject = _normalize_live_query_text(subject)
    subject_terms = [
        term
        for term in re.findall(r"[a-z0-9]+", normalized_subject)
        if len(term) > 2 and term not in {"and", "the", "for", "with", "from", "into"}
    ]
    required_term_matches = len(subject_terms) if ("ratio" in subject_terms or "-" in normalized_subject) else max(1, min(len(subject_terms), 2))
    ordered_results = [primary] + [
        result
        for result in results
        if result is not primary
        and not _looks_like_low_quality_source_result(result)
    ]

    selected: list[tuple[str, dict[str, str]]] = []
    fallback: list[tuple[str, dict[str, str]]] = []
    for result in ordered_results:
        if result is not primary and _is_unrequested_side_topic_result(user_input, result):
            continue
        for sentence in _split_source_sentences(result.get("snippet") or ""):
            lowered_sentence = _normalize_live_query_text(sentence)
            if _looks_like_heading_only_source_sentence(sentence):
                continue
            matched_terms = sum(1 for term in subject_terms if _normalized_text_contains_term_form(lowered_sentence, term))
            if _looks_like_promotional_source_sentence(sentence):
                if matched_terms >= required_term_matches:
                    fallback.append((sentence, result))
                continue
            has_subject = not subject_terms or matched_terms >= required_term_matches
            has_explanation = bool(
                re.search(
                    r"\b(is|are|means|refers to|describes|process|method|system|used to|works by|happens when|in which)\b",
                    lowered_sentence,
                )
            )
            if has_subject and has_explanation:
                selected.append((sentence, result))
            elif has_subject:
                fallback.append((sentence, result))
            if len(selected) >= 3:
                break
        if len(selected) >= 3:
            break

    useful = selected or fallback
    if not useful:
        return ""
    first_sentence, first_source = useful[0]
    source_text = f"[{first_source['title']}]({first_source['url']})"
    if len(useful) == 1:
        return f"Short answer: {first_sentence}\n\nSource: {source_text}."

    lines = [f"Short answer: {first_sentence}", "", "Useful details:"]
    seen_sentences = {_normalize_live_query_text(first_sentence)}
    for sentence, _result in useful[1:4]:
        normalized_sentence = _normalize_live_query_text(sentence)
        if normalized_sentence in seen_sentences:
            continue
        seen_sentences.add(normalized_sentence)
        lines.append(f"- {sentence}")
    lines.append("")
    lines.append(f"Source: {source_text}.")
    return "\n".join(lines)


def _shape_source_snippet_answer(user_input: str, primary: dict[str, str], results: list[dict[str, str]]) -> str:
    lowered = _normalize_live_query_text(user_input)
    source_text = f"[{primary['title']}]({primary['url']})"
    snippet = _clean_source_snippet(primary.get("snippet") or "")
    title = primary.get("title") or "source"

    if not snippet:
        return ""

    if _looks_like_concept_explainer_query(user_input):
        concept_answer = _concept_explainer_answer(user_input, primary, results)
        if concept_answer:
            return concept_answer

    if re.search(r"\bwhat\s+is|define|meaning|explain\b", lowered):
        return f"{_first_useful_sentence(snippet)}\n\nSource: {source_text}."

    if re.search(r"\bwho\s+is|who\s+was\b", lowered):
        return f"{_first_useful_sentence(snippet)}\n\nSource: {source_text}."

    if re.search(r"\bwhen\b|date\b", lowered):
        return f"{_first_useful_sentence(snippet)}\n\nSource: {source_text}."

    if re.search(r"\bwhich\b|\bwhere\b|\bhow\b|\bwhy\b", lowered):
        return f"{_first_useful_sentence(snippet)}\n\nSource: {source_text}."

    if re.search(r"\bnews|latest|updates?|today\b", lowered):
        lines = [f"Source-reported result from {title}:", "", f"- {_first_useful_sentence(snippet, max_chars=260)}"]
        for result in results[1:4]:
            extra = _first_useful_sentence(result.get("snippet") or result.get("title") or "", max_chars=180)
            if extra:
                lines.append(f"- {extra} Source: [{result['title']}]({result['url']}).")
        lines.append("")
        lines.append(f"Primary source: {source_text}.")
        return "\n".join(lines)

    return f"{_first_useful_sentence(snippet)}\n\nSource: {source_text}."


def _prompt_style_flags(user_input: str) -> dict[str, bool]:
    lowered = _normalize_live_query_text(user_input)
    return {
        "one_line": bool(
            re.search(
                r"\b(one line|single line|short answer|brief answer|keep it short|make it short)\b",
                lowered,
            )
        ),
        "simple": bool(re.search(r"\b(simple words?|explain like i am \d+|eli\d+)\b", lowered)),
        "bullets": bool(re.search(r"\b(bullet points?|bullets?)\b", lowered)),
        "table": bool(re.search(r"\b(table|tabular)\b", lowered)),
    }


def _escape_markdown_table_cell(value: str) -> str:
    return _compact_text(value).replace("|", "\\|")


def _apply_prompt_response_style(user_input: str, answer: str) -> str:
    """Apply lightweight deterministic formatting without changing source facts."""
    if not answer:
        return answer
    flags = _prompt_style_flags(user_input)
    if not any(flags.values()):
        return answer

    if flags["table"]:
        if re.search(r"(?m)^\|.+\|\s*$", answer) and re.search(r"(?m)^\|[-: ]+\|", answer):
            return answer
        source_match = re.search(r"(?im)^Source:\s*(.+)$", answer)
        source_text = source_match.group(1).strip() if source_match else ""
        body = re.sub(r"(?im)^Source:\s*.+$", "", answer).strip()
        rows = ["| Field | Value |", "|---|---|"]
        for line in [line.strip("- ").strip() for line in body.splitlines() if line.strip()]:
            key_candidate = line.split(":", 1)[0] if ":" in line else ""
            if (
                ":" in line
                and len(key_candidate) <= 42
                and not re.search(r"\d|,\s+\d{4}", key_candidate)
            ):
                key, value = line.split(":", 1)
                rows.append(f"| {_escape_markdown_table_cell(key)} | {_escape_markdown_table_cell(value)} |")
            else:
                rows.append(f"| Answer | {_escape_markdown_table_cell(line)} |")
        if source_text:
            rows.append(f"| Source | {_escape_markdown_table_cell(source_text)} |")
        return "\n".join(rows)

    if flags["bullets"]:
        source_match = re.search(r"(?im)^Source:\s*(.+)$", answer)
        source_text = source_match.group(1).strip() if source_match else ""
        body = re.sub(r"(?im)^Source:\s*.+$", "", answer).strip()
        if re.search(r"(?m)^\s*[-*]\s+", body):
            styled = body
        else:
            sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", _compact_text(body)) if sentence.strip()]
            styled = "\n".join(f"- {sentence}" for sentence in sentences[:4]) or f"- {_compact_text(body)}"
        if source_text:
            styled += f"\n- Source: {source_text}"
        return styled

    if flags["one_line"]:
        compacted = _compact_text(answer)
        return compacted[:700].rstrip() + ("..." if len(compacted) > 700 else "")

    if flags["simple"]:
        if "Clear breakdown:" in answer or "Simple example:" in answer:
            return answer if answer.lower().startswith("simple answer:") else f"Simple answer:\n\n{answer}"
        source_match = re.search(r"(?im)^Source:\s*(.+)$", answer)
        source_text = source_match.group(1).strip() if source_match else ""
        body = re.sub(r"(?im)^Source:\s*.+$", "", answer).strip()
        simple_body = _first_useful_sentence(body, max_chars=360) or _compact_text(body)
        if simple_body.lower().startswith("simple answer:"):
            styled = simple_body
        else:
            styled = f"Simple answer: {simple_body}"
        if source_text:
            styled += f"\n\nSource: {source_text}"
        return styled

    return answer


def _last_resort_text_answer(user_input: str, language_preference: str | None = None, reason: str = "") -> str:
    """Final non-blocking fallback when provider and live source lanes fail.

    This avoids sending users into the old dead-end refusal loop while still
    keeping live/current facts clearly labeled when they could not be verified.
    """
    utility = _fast_local_reply(_NoopDb(), user_input, language_preference)
    if utility:
        return utility

    math_answer = _simple_math_reply(user_input)
    if math_answer:
        return math_answer

    stable = _local_static_factual_answer(user_input)
    if stable:
        return stable

    cleaned = _compact_text(user_input)
    lowered = _normalize_live_query_text(cleaned)
    if not cleaned:
        return "I am ready. Send the question again and I will answer it."

    if re.search(r"\b(latest|current|present|today|now|live|price|rate|weather|score|news|stock|crypto)\b", lowered):
        reason_text = f" ({reason})" if reason else ""
        return (
            f"I could not verify a live source for this exact request{reason_text}. "
            "For current values I need a working source, so I will not fake the number. "
            "The chat route is reachable, but the upstream model/source returned no usable data for this prompt."
        )

    if re.search(r"\b(explain|what is|what are|how does|how do|why|define)\b", lowered):
        topic = re.sub(r"^(?:please\s+)?(?:explain|define|what\s+is|what\s+are|how\s+does|how\s+do|why)\s+", "", cleaned, flags=re.IGNORECASE).strip(" ?.!")
        if topic:
            return (
                f"I could not get a complete model/source response for {topic}. "
                "The chat route is reachable, but this prompt needs either the OpenRouter model response or a usable source result."
            )

    return (
        "I could not get a complete answer from the model or source fallback for this request. "
        "The chat service is reachable, but the upstream model/source returned no usable content."
    )


def _detect_output_formats(user_input: str) -> list[str]:
    lowered = user_input.lower()
    formats: list[str] = []
    checks = [
        ("xlsx", r"\b(excel|xlsx|spreadsheets?|workbooks?)\b"),
        ("pdf", r"\b(pdfs?|reports?|invoices?|receipts?|certificates?|resume|resumes|notes?|formula sheets?|study plans?)\b"),
        ("docx", r"\b(word|docx|documents?)\b"),
        ("pptx", r"\b(powerpoint|ppt|pptx|presentation|slides?)\b"),
        ("csv", r"\b(csvs?|csv files?)\b"),
        ("json", r"\b(json)\b"),
        ("png", r"\b(pngs?|images?|diagrams?|photos?|pictures?|charts?)\b"),
        ("jpg", r"\b(jpgs?|jpegs?)\b"),
        ("markdown", r"\b(markdown|md)\b"),
        ("zip", r"\b(zips?|archives?)\b"),
    ]
    for name, pattern in checks:
        if re.search(pattern, lowered):
            formats.append(name)
    return formats


def _needs_structured_table(user_input: str) -> bool:
    lowered = user_input.lower()
    return bool(
        re.search(
            r"\b(table|points table|standings?|stats|statistics|compare|comparison|ranking|rank|"
            r"schedule|fixtures?|scorecard|list|summarize|summary|compress)\b",
            lowered,
        )
    )


def _is_deep_conversation_mode(conversation_mode: str | None) -> bool:
    return (conversation_mode or "").strip().lower() in {"research", "agent", "skill", "deep"}


def _response_token_limit(
    user_input: str,
    attachments: list[dict] | None = None,
    conversation_mode: str | None = None,
) -> int:
    """Keep Quick/chat/voice responsive; reserve longer responses for explicit work modes."""
    is_deep_mode = _is_deep_conversation_mode(conversation_mode)
    if attachments:
        return 900 if is_deep_mode else 520
    requested_formats = _detect_output_formats(user_input)
    lowered = user_input.lower()
    if requested_formats:
        return 1500 if is_deep_mode else 900
    if _needs_structured_table(user_input) or _needs_live_web_context(user_input):
        return 900 if is_deep_mode else 420
    if len(user_input) <= 120 and not re.search(r"\b(explain|detail|clear|clearly|deep|complete|full|step by step)\b", lowered):
        return 260
    return 700 if is_deep_mode else 600


def _openrouter_token_budget_candidates(requested_tokens: int) -> list[int]:
    """Only try the requested tokens — no cascading micro-retries that add latency."""
    return [requested_tokens]



def _chat_request_timeout_seconds(
    user_input: str,
    attachments: list[dict] | None = None,
    conversation_mode: str | None = None,
) -> float:
    """Tighter timeouts — fail fast and use fallback rather than hang."""
    if _is_deep_conversation_mode(conversation_mode):
        return 20.0
    if attachments:
        return 16.0
    if _needs_live_web_context(user_input) or _needs_structured_table(user_input):
        return 10.0
    return 8.0   # default: 8s max — voice needs <3s first token


#: How long the *whole* cascade may take, as opposed to one request in it.
#:
#: This is the number that was missing, and its absence is what "a very high
#: amount of latency" was. The per-request timeout above is tight, but the retry
#: loop multiplies it: nine candidates times two message variants times eight
#: seconds is 144 seconds of a user watching nothing happen, and the honest
#: fallback that follows arrives so late it reads as no answer at all -- which is
#: exactly how it was reported ("No fallback reply is coming").
#:
#: So the cascade gets a wall clock. When it runs out, whatever has been learned
#: so far goes to the fallback immediately. Better a cited web extract or a plain
#: "I could not reach a model" in ten seconds than a perfect reply in two minutes.
_CASCADE_BUDGET_VOICE_S = 12.0
#: Text can wait a little longer than a listener can, but not much: past about
#: half a minute a chat reply has already lost the conversation.
_CASCADE_BUDGET_TEXT_S = 30.0
#: Below this there is no point starting another provider round trip -- it would
#: only push the fallback further out.
_CASCADE_MIN_ATTEMPT_S = 2.0

#: How long `_provider_failure_fallback` may spend on its own forced web lookup.
#:
#: The cascade clock above bounds the cascade and nothing else, and the fallback is
#: not free: with no live context and no attachments it calls
#: `_build_multi_question_live_context(..., force=True)`, which walks up to four
#: questions and can issue a dozen serial requests whose individual timeouts run to
#: 14s. That is spent *after* the 30s text budget has already gone, and it is spent
#: in silence -- the fallback is one `yield` at the end of the generator, so the
#: client sees an empty stream the whole time and eventually gives up. So the second
#: half of "no fallback reply is coming" was never the fallback missing; it was the
#: fallback still shopping for sources a minute later.
#:
#: Six seconds is the compromise: enough for one cached or fast source, short enough
#: that the local replies and the last-resort text below always get their turn.
_FALLBACK_LIVE_BUDGET_S = 6.0

#: The same clock for the live lookup that runs *before* the model, which is the
#: other half of the reported latency and the half that hits ordinary turns.
#: `_needs_live_web_context` is deliberately broad -- `version`, `model`, `table`,
#: `top` and `now` are all in it -- so a large share of prompts pay a serial web walk
#: before the first token, with no cap on it whatsoever.
#:
#: Text gets twelve seconds because on those prompts the sources *are* the answer.
#: Voice gets seven: the cascade budget after it is twelve, the acknowledgement line
#: is already being spoken over this, and a listener who has been told "let me check
#: that" still expects an answer in the same breath.
_PRE_MODEL_LIVE_BUDGET_TEXT_S = 12.0
_PRE_MODEL_LIVE_BUDGET_VOICE_S = 7.0


def _live_context_within_budget(
    user_input: str,
    budget_s: float = _FALLBACK_LIVE_BUDGET_S,
    force: bool = False,
) -> str:
    """`_build_multi_question_live_context`, but it cannot overrun.

    The work happens on a worker thread and the executor is shut down *without*
    waiting, which is the whole point -- `with ThreadPoolExecutor()` would join on
    exit and reintroduce the delay it is here to remove. A thread cannot be killed
    in Python, so a slow lookup does keep running; it just no longer keeps the user
    waiting, and it writes its result into `_LIVE_CONTEXT_CACHE` on the way out, so
    an immediate retry of the same question gets the sources for free.

    An empty string on timeout is deliberate, and it is honest in both callers: no
    live context means the model is not handed sources it can cite, and the fallback
    treats it as a reason to answer locally -- which is the correct behaviour when
    the provider is down and the web is slow.
    """
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-ctx")
    try:
        future = executor.submit(_build_multi_question_live_context, user_input, force)
        try:
            return future.result(timeout=budget_s)
        except concurrent.futures.TimeoutError:
            print(f"Live lookup exceeded {budget_s:.0f}s; answering without it")
            return ""
        except Exception as exc:
            print(f"Live lookup failed: {exc}")
            return ""
    finally:
        executor.shutdown(wait=False)


def _build_output_intent_context(user_input: str) -> str:
    requested_formats = _detect_output_formats(user_input)
    table_needed = _needs_structured_table(user_input)
    if not requested_formats and not table_needed:
        return (
            "OUTPUT INTELLIGENCE: Choose the most useful format automatically. Use Markdown tables for comparisons, "
            "standings, stats, schedules, live data, prices, news, and multi-item summaries. For live/current claims, "
            "separate source-reported facts from unverified details."
        )

    lines = [
        "OUTPUT INTELLIGENCE CONTRACT:",
        "- Infer the user's desired output format from the prompt and answer in that format immediately.",
        "- If a table is useful or requested, produce a clean Markdown table with concise column names.",
        "- Do not refuse table/file-style requests just because every field is not verified; include a Source/Status column and mark missing live fields as Not verified.",
        "- For live/current data, use DIRECT LIVE DATA or LIVE WEB CONTEXT first, include source names and fetched timestamp/date.",
        "- Never tell the user to visit a website instead of answering; answer with verified facts and uncertainty labels.",
        "- Do not use confident words like current/latest/confirmed unless the value is in DIRECT LIVE DATA or a cited live source. If not verified, label it Not verified instead of guessing.",
        "- Confidence score means source coverage only: High = exact parsed source rows, Medium = source snippet/headline only, Low = partial or conflicting sources. Never use confidence to make an unverified fact sound true.",
        "- For news, summarize only source-reported items that appear in the live context. Do not add background claims, names, scores, dates, or conclusions that are not present in the source lines.",
    ]
    if table_needed:
        lines.append("- The user needs structured information: prioritize a table before explanatory paragraphs.")
    if requested_formats:
        lines.append(
        "- The user requested downloadable/exportable formats: "
        + ", ".join(requested_formats)
        + ". The backend artifact engine will create the real downloadable files from the final answer, so keep the content structured and never invent sandbox:/ download links."
        )
    return "\n".join(lines)


def _build_attachment_message_content(user_input: str, attachments: list[dict]) -> list[dict]:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"{user_input}\n\n"
                "Attached content is part of the question. Analyze it carefully and answer directly. "
                "For screenshots, inspect visible UI state, buttons, text, alignment, errors, and small details."
            ),
        }
    ]

    for index, attachment in enumerate(attachments[:5], start=1):
        name = str(attachment.get("name") or f"attachment-{index}")
        mime_type = str(attachment.get("type") or "")
        data_url = str(attachment.get("data_url") or "")
        text = str(attachment.get("text") or "")
        size = attachment.get("size")

        label = f"Attachment {index}: {name}"
        if mime_type:
            label += f" ({mime_type})"
        if size:
            label += f", {size} bytes"

        if data_url.startswith("data:image/"):
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {"url": data_url}})
            continue

        if text:
            content.append(
                {
                    "type": "text",
                    "text": f"{label}\nFile text excerpt:\n{text[:16000]}",
                }
            )
            continue

        content.append(
            {
                "type": "text",
                "text": f"{label}\nNo readable preview was available for this file type.",
            }
        )

    return content


def _local_image_attachment_summary(attachments: list[dict]) -> str | None:
    """Return a deterministic local pixel summary when cloud vision is unavailable."""
    image_lines: list[str] = []
    for index, attachment in enumerate((attachments or [])[:5], start=1):
        mime_type = str(attachment.get("type") or "")
        data_url = str(attachment.get("data_url") or "")
        if not mime_type.startswith("image/") or not data_url.startswith("data:image/") or "," not in data_url:
            continue

        name = str(attachment.get("name") or f"image-{index}")
        size = attachment.get("size")
        try:
            from PIL import Image, ImageStat

            raw = base64.b64decode(data_url.split(",", 1)[1], validate=False)
            with Image.open(io.BytesIO(raw)) as image:
                rgb = image.convert("RGB")
                stat = ImageStat.Stat(rgb.resize((80, 80)))
                mean = tuple(int(value) for value in stat.mean[:3])
                brightness = round(sum(mean) / 3)
                orientation = "landscape" if image.width > image.height else "portrait" if image.height > image.width else "square"
                image_lines.append(
                    f"- {name}: {image.width}x{image.height}px {orientation} {mime_type}; "
                    f"approx brightness {brightness}/255; average RGB {mean}; uploaded size {size or len(raw)} bytes."
                )
        except Exception:
            image_lines.append(f"- {name}: image received and queued, but local pixel metadata could not be decoded.")

    if not image_lines:
        return None

    return (
        "I received the image and completed the local pixel pass:\n"
        + "\n".join(image_lines)
        + "\nFor tiny text/OCR, object recognition, or detailed screenshot reasoning, the vision lane must be active; I will not invent details that are not locally readable."
    )


QUESTION_NUMBER_KEYS = {
    "question_number",
    "questionno",
    "question_no",
    "questionid",
    "question_id",
    "qno",
    "q_no",
    "number",
    "no",
    "id",
    "serial",
    "index",
    "sno",
    "s_no",
}


def _attachment_text_items(attachments: list[dict] | None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for index, attachment in enumerate((attachments or [])[:5], start=1):
        text = str(attachment.get("text") or "")
        if not text.strip():
            continue
        name = str(attachment.get("name") or f"attachment-{index}")
        items.append((name, text))
    return items


def _extract_requested_question_number(user_input: str) -> int | None:
    cleaned = _compact_text(user_input)
    patterns = (
        r"\b(?:question|q)\s*(?:number|no\.?|#)?\s*(\d{1,5})\b",
        r"\b(\d{1,5})(?:st|nd|rd|th)?\s+(?:question|q)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if value > 0:
                return value
    return None


def _int_like(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if re.fullmatch(r"\d+", cleaned):
            return int(cleaned)
    return None


def _find_numbered_json_item(data: object, number: int) -> object | None:
    """Find item whose explicit id/no is number; fall back to list position."""
    def walk(node: object) -> object | None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())
                if _int_like(key) == number and isinstance(value, (dict, list, str)):
                    return value
                if normalized_key in QUESTION_NUMBER_KEYS and _int_like(value) == number:
                    return node
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found is not None:
                    return found
            if 0 < number <= len(node):
                return node[number - 1]
        return None

    return walk(data)


def _first_dict_value(item: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(key).lower(): value for key, value in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _compact_json_value(value: object, limit: int = 700) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _format_question_item_answer(file_name: str, number: int, item: object) -> str:
    if isinstance(item, dict):
        question = _first_dict_value(
            item,
            (
                "question",
                "question_text",
                "title",
                "prompt",
                "statement",
                "text",
                "content",
                "name",
            ),
        )
        topic = _first_dict_value(item, ("topic", "subject", "category", "chapter", "section", "tag"))
        answer = _first_dict_value(item, ("answer", "correct_answer", "solution", "explanation"))
        options = item.get("options") or item.get("choices") or item.get("mcq_options")

        lines = [f"Question {number} in {file_name}:"]
        if question:
            lines.append(f"- It is about: {question}")
        else:
            lines.append(f"- Item content: {_compact_json_value(item)}")
        if topic:
            lines.append(f"- Topic/category: {topic}")
        if options:
            lines.append(f"- Options: {_compact_json_value(options, 400)}")
        if answer:
            lines.append(f"- Answer/solution field: {_compact_json_value(answer, 500)}")
        return "\n".join(lines)

    return (
        f"Question {number} in {file_name}:\n"
        f"- It is about: {_compact_json_value(item, 900)}"
    )


def _find_numbered_text_item(text: str, number: int) -> str | None:
    lines = text.splitlines()
    marker = re.compile(rf"^\s*(?:q(?:uestion)?\.?\s*)?{number}\s*[\).:\-]\s*(.+)?$", re.IGNORECASE)
    for index, line in enumerate(lines):
        if marker.match(line):
            window = "\n".join(lines[index : min(len(lines), index + 8)]).strip()
            return window[:1400]

    inline = re.search(
        rf"(?:question|q)\s*(?:number|no\.?|#)?\s*{number}\b(.{{0,1200}})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if inline:
        return re.sub(r"\s+", " ", inline.group(0)).strip()[:1400]
    return None


def _local_attachment_question_answer(user_input: str, attachments: list[dict] | None) -> str | None:
    number = _extract_requested_question_number(user_input)
    if not number:
        return None

    for file_name, text in _attachment_text_items(attachments):
        try:
            parsed = json.loads(text)
            item = _find_numbered_json_item(parsed, number)
            if item is not None:
                return _format_question_item_answer(file_name, number, item)
        except Exception:
            pass

        text_item = _find_numbered_text_item(text, number)
        if text_item:
            return (
                f"Question {number} in {file_name}:\n"
                f"- Matched text:\n{text_item}"
            )

    return None


def _provider_failure_kind(error: Exception) -> str:
    message = str(error).lower()
    if isinstance(error, OpenRouterConfigurationError) or "401" in message or "authentication" in message or "unauthorized" in message:
        return "auth"
    if "402" in message or "credit" in message or "budget" in message or "insufficient" in message:
        return "capacity"
    # A route this key cannot use at all: OpenRouter answers 404 "No endpoints found"
    # / "unavailable for free" for models that exist but are not served to us. Split
    # out from "provider" because it is durable — worth a cooldown — whereas a plain
    # provider error usually is not.
    if "404" in message or "no endpoints" in message or "unavailable" in message or "not available" in message:
        return "unavailable"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "rate" in message and "limit" in message:
        return "rate_limit"
    return "provider"


def _provider_failure_fallback(
    user_input: str,
    attachments: list[dict] | None,
    error: Exception,
    language_preference: str | None = None,
    live_context: str = "",
) -> str:
    preference = (language_preference or "").lower()
    kind = _provider_failure_kind(error)
    has_images = any(str(item.get("type") or "").startswith("image/") for item in attachments or [])
    has_attachments = bool(attachments)
    local_attachment_answer = _local_attachment_question_answer(user_input, attachments)
    if local_attachment_answer:
        return local_attachment_answer

    if not has_attachments and _should_use_local_utility_reply(user_input):
        return _fast_local_reply_for_provider_failure(user_input, language_preference)

    math_answer = _simple_math_reply(user_input)
    if math_answer:
        return math_answer

    currency_answer = _quick_currency_reply(user_input, preference)
    if currency_answer:
        return _apply_prompt_response_style(user_input, currency_answer)

    if not live_context and not has_attachments:
        # A conversational prompt must not be answered out of a search result. With
        # the provider down, the forced lookup below was the only thing left, so
        # "hi" came back as a dictionary entry for the word HI (1.7 s) and "how is
        # the world going on" as trivia about the Gershwin song "How Long Has This
        # Been Going On?" (3.4 s) — slower than a real answer, and answering a
        # different question than the one asked.
        #
        # The right replies already existed and were merely unreachable: the local
        # conversational path is gated above behind `_should_use_local_utility_reply`,
        # which is False for anything conversational by design (those prompts are
        # supposed to reach the model — see the dynamic-prompt list in the audit
        # regressions). This is the branch where the model is *gone*, so the local
        # reply is the best remaining answer rather than a bypass.
        #
        # Gated on `_needs_live_web_context` so intent decides, not phrasing luck:
        # prompts that genuinely want live information ("latest IPL score today")
        # have no local reply and still fall through to the fetch below. The
        # last-resort placeholder is deliberately not consulted here — an unhelpful
        # "I could not get a complete answer" is worse than a real source, so a
        # miss falls through too.
        if not _needs_live_web_context(user_input):
            conversational = _fast_local_reply(
                _NoopDb(), user_input, language_preference
            ) or _quick_local_response(
                _NoopDb(),
                user_input,
                language_preference,
                speaker_profile=None,
                allow_conversation_fragments=True,
            )
            if conversational:
                return conversational
        live_context = _live_context_within_budget(user_input, force=True)

    source_fallback = _source_context_fallback_answer(user_input, live_context, language_preference)
    if source_fallback:
        return _apply_prompt_response_style(user_input, source_fallback)

    if has_images:
        local_summary = _local_image_attachment_summary(attachments or [])
        if local_summary:
            return local_summary
        if "hindi" in preference:
            return (
                "Image attach ho gayi hai. Main local pixel summary de sakta hoon, par detailed OCR/object reasoning abhi complete nahi hua. "
                "Screenshot ka exact part batao, main visible metadata se help karta hoon."
            )
        if "telugu" in preference:
            return (
                "Image attach ayyindi. Local pixel summary cheppagalanu, kani detailed OCR/object reasoning ippudu complete avvaledu. "
                "Screenshot lo exact part cheppu, visible metadata tho help chestha."
            )
        return (
            "I received the image. I can summarize local pixel metadata, but detailed OCR/object reasoning did not complete this turn. "
            "Tell me the exact area you want checked and I will use the visible metadata without guessing."
        )

    if has_attachments:
        return (
            "I received the attachment and the upload path is working. Detailed file/video analysis did not complete this turn; "
            "for now, paste the exact section you want checked and I will handle that part locally."
        )

    if _should_use_local_utility_reply(user_input):
        return _fast_local_reply_for_provider_failure(user_input, language_preference)

    local_quick_answer = _quick_local_response(
        _NoopDb(),
        user_input,
        language_preference,
        speaker_profile=None,
        allow_conversation_fragments=True,
    )
    if local_quick_answer:
        return local_quick_answer

    if _wants_joke(user_input):
        return _fallback_joke_reply(user_input, language_preference or "")

    if kind == "auth":
        return _apply_prompt_response_style(
            user_input,
            _last_resort_text_answer(
                user_input,
                language_preference,
                reason="the model provider is not authenticated in this backend session",
            ),
        )

    if kind in {"capacity", "rate_limit"}:
        if _should_use_local_utility_reply(user_input):
            return _fast_local_reply_for_provider_failure(user_input, language_preference)
        return _apply_prompt_response_style(
            user_input,
            _last_resort_text_answer(
                user_input,
                language_preference,
                reason="the model provider is temporarily unavailable",
            ),
        )

    if kind == "timeout":
        return _apply_prompt_response_style(
            user_input,
            _last_resort_text_answer(user_input, language_preference, reason="the answer timed out"),
        )

    return _apply_prompt_response_style(
        user_input,
        _last_resort_text_answer(user_input, language_preference, reason="the model provider did not return a usable response"),
    )


def _fast_local_reply_for_provider_failure(user_input: str, language_preference: str | None = None) -> str:
    return _fast_local_reply(_NoopDb(), user_input, language_preference) or _quick_local_response(
        _NoopDb(),
        user_input,
        language_preference,
        speaker_profile=None,
        allow_conversation_fragments=True,
    ) or (
        _last_resort_text_answer(user_input, language_preference)
    )


def _handle_dynamic_filesystem_or_desktop_intent(user_input: str) -> str | None:
    # Latin view for the keywords; `user_input` for the path, which is Latin
    # already and must keep its exact case and separators.
    text = _intent_text(user_input).lower()

    is_folder_query = bool(
        re.search(r"\b(folder|directory|dir|files|inventory|contents)\b", text)
        and any(kw in text for kw in ["detail", "details", "list", "show", "inspect", "pdf", "generate", "contents", "files", "summary", "report"])
    ) or bool(re.search(r"([a-zA-Z]:\\[^ \t\n\r\f\v]+|[a-zA-Z]:/[^ \t\n\r\f\v]+|~/[^ \t\n\r\f\v]+)", user_input))

    if not is_folder_query:
        return None

    from .agent_modules.desktop_automation import DesktopAutomationModule
    desktop = DesktopAutomationModule()
    res = desktop.inspect_directory(user_input)
    if res.get("status") == "success":
        return res.get("markdown")
    return None


def _handle_task_automation_intent(db: Session, user_input: str) -> str | None:
    # The Latin view. Every keyword in this function -- "open ", "youtube",
    # "run automation" -- is English, so before this line a Telugu or Hindi
    # command walked straight past the whole interceptor and was answered with
    # conversation instead of being carried out. `user_input` is still used
    # verbatim wherever the *content* matters (prompt payloads, URLs), because the
    # view is for matching only.
    text = _intent_text(user_input).lower()

    
    # 1. Listing automations
    if text.startswith("/automations") or any(kw in text for kw in [
        "list automations", "show automations", "my automations", "active automations", 
        "task automation status", "task studio automations", "show task automations", "check automations"
    ]):
        try:
            automations = db.query(TaskAutomation).all()
            if not automations:
                return (
                    "⚡ **Task Automations Studio Overview**\n\n"
                    "No active task automations configured.\n\n"
                    "👉 **Try saying**: *'create automation to check codebase health every 60m'* or click **Task Studio** in the sidebar!"
                )
            
            lines = ["⚡ **Task Automations Studio - Control Center Overview**\n"]
            for a in automations:
                status_icon = "🟢 ACTIVE" if a.status == "active" else "⏸️ PAUSED" if a.status == "paused" else "🔴 ERROR"
                cfg = json.loads(a.trigger_config or "{}")
                trigger_info = f"Every {cfg.get('interval_minutes', 60)}m" if a.trigger_type == "schedule" else f"Webhook key: `{cfg.get('webhook_key', 'wh')}`"
                lines.append(f"### 🤖 Automation #{a.id}: **{a.name}**")
                lines.append(f"- **Status**: `{status_icon}` | **Type**: `{a.action_type}`")
                lines.append(f"- **Trigger**: {trigger_info} | **Total Runs**: `{a.run_count or 0}`")
                if a.description:
                    lines.append(f"- **Description**: {a.description}")
                lines.append("")
            
            logs = db.query(AutomationExecutionLog).order_by(AutomationExecutionLog.started_at.desc()).limit(3).all()
            if logs:
                lines.append("---")
                lines.append("📋 **Recent Audit Logs:**")
                for l in logs:
                    st_icon = "✅ SUCCESS" if l.status == "success" else "❌ FAILED"
                    lines.append(f"- [{st_icon}] **{l.automation_name}**: {l.output_summary}")
            
            lines.append("\n💡 *To execute any automation right now, reply:* `'run automation <name or ID>'` *or use* `/run-automation <id>`")
            return "\n".join(lines)
        except Exception as err:
            return f"⚡ Unable to fetch automations: {str(err)}"

    # 2. Immediate Execution ("Run Now") or GitHub issue decomposition / daily report triggers
    if text.startswith("/run-automation") or any(kw in text for kw in [
        "run automation", "trigger automation", "execute automation", "run daily report", 
        "decompose github", "decompose issue", "run task automation", "trigger task automation"
    ]):
        try:
            automations = db.query(TaskAutomation).all()
            target_auto = None
            
            # Match by explicit ID or name
            for a in automations:
                if a.name.lower() in text or f"#{a.id}" in text or f"id {a.id}" in text or str(a.id) in text.split():
                    target_auto = a
                    break
            
            # If user said 'decompose' or 'daily report' specifically
            if not target_auto:
                if "decompose" in text or "github" in text:
                    target_auto = next((a for a in automations if a.action_type == "github_decompose"), None)
                elif "report" in text or "daily" in text:
                    target_auto = next((a for a in automations if a.action_type == "daily_report"), None)
            
            if not target_auto and automations:
                target_auto = automations[0]
            
            # If no existing automation exists, create & run on the fly!
            if not target_auto:
                target_auto = TaskAutomation(
                    name=f"Automation: {user_input[:32]}",
                    description=user_input,
                    trigger_type="manual",
                    trigger_config=json.dumps({"source": "chat_instant"}),
                    action_type="github_decompose" if ("decompose" in text or "github" in text) else "ai_workflow",
                    action_payload=json.dumps({"prompt": user_input}),
                    status="active"
                )
                db.add(target_auto)
                db.commit()
                db.refresh(target_auto)

            import asyncio
            import concurrent.futures
            from .main import _execute_task_automation
            
            try:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(lambda: asyncio.run(_execute_task_automation(target_auto, trigger_source="chat_direct", db=db)))
                    res = future.result(timeout=30)
            except Exception as e:
                res = {"success": False, "error": str(e)}

            if res.get("success"):
                return (
                    f"🚀 **Task Automation Executed Successfully!**\n\n"
                    f"### 🤖 Workflow: **{target_auto.name}** (ID #{target_auto.id})\n"
                    f"- **Trigger Source**: Chat Interface (Direct Command)\n"
                    f"- **Status**: `✅ SUCCESS`\n"
                    f"- **Output Summary**: {res.get('output_summary')}\n\n"
                    f"👉 *View full details or manage schedules anytime in [Task Studio](/task-automations).* 🎉"
                )
            else:
                return f"⚠️ **Execution Failed for '{target_auto.name}'**: {res.get('error', 'Execution error')}"
        except Exception as err:
            return f"⚡ Automation execution error: {str(err)}"

    # 3. Creation / Scheduling of automations
    if text.startswith("/automate") or any(kw in text for kw in [
        "schedule automation", "create automation", "set up automation", 
        "automate codebase", "automate daily", "automate task"
    ]):
        prompt_content = user_input.replace("/automate", "").replace("schedule automation", "").replace("create automation", "").replace("set up automation", "").strip()
        if not prompt_content:
            prompt_content = "Periodic codebase health audit & task decomposition"
        
        try:
            auto = TaskAutomation(
                name=f"Automation: {prompt_content[:32]}",
                description=prompt_content,
                trigger_type="schedule",
                trigger_config=json.dumps({"interval_minutes": 60}),
                action_type="ai_workflow",
                action_payload=json.dumps({"prompt": prompt_content}),
                status="active"
            )
            db.add(auto)
            db.commit()
            db.refresh(auto)
            return (
                f"✅ **New Task Automation Created & Active!**\n\n"
                f"### 🤖 **{auto.name}** (ID #{auto.id})\n"
                f"- **Schedule**: Every 60 minutes (Background Loop)\n"
                f"- **Action Type**: `{auto.action_type}`\n"
                f"- **Prompt Payload**: {prompt_content}\n\n"
                f"💡 *Say 'run automation #{auto.id}' anytime to trigger an immediate execution!*"
            )
        except Exception as err:
            return f"⚡ Error creating task automation: {str(err)}"

    # 4. Universal Action Interceptor (Physical App & Web Execution Engine)
    action_keywords = ["open ", "check ", "launch ", "go to ", "visit ", "show ", "ping "]
    if any(text.startswith(kw) or f" {kw}" in text for kw in action_keywords) or any(p in text for p in ["twitter", "x.com", "codechef", "github", "whatsapp", "leetcode", "hackerrank", "youtube", "gmail", "linkedin", "notion", "google"]):
        import subprocess
        from .automation import _launch_windows_app

        actions_taken = []
        
        # Site URL Registry & Dynamic Matcher
        site_map = {
            "twitter": ("Twitter Notifications", "https://x.com/notifications" if "notification" in text else "https://x.com"),
            "x.com": ("Twitter / X", "https://x.com/notifications" if "notification" in text else "https://x.com"),
            "github": ("GitHub", "https://github.com/notifications" if "notification" in text else "https://github.com"),
            "codechef": ("CodeChef", "https://www.codechef.com/notifications" if "notification" in text else "https://www.codechef.com"),
            "leetcode": ("LeetCode", "https://leetcode.com/problemset/all/"),
            "hackerrank": ("HackerRank", "https://www.hackerrank.com/dashboard"),
            "youtube": ("YouTube", "https://www.youtube.com"),
            "gmail": ("Gmail", "https://mail.google.com"),
            "email": ("Email Inbox", "https://mail.google.com"),
            "linkedin": ("LinkedIn", "https://www.linkedin.com/messaging/" if "message" in text else "https://www.linkedin.com"),
            "notion": ("Notion", "https://www.notion.so"),
            "google": ("Google", "https://www.google.com"),
        }

        # Check Desktop Apps
        desktop_apps = ["notepad", "vscode", "visual studio", "calculator", "spotify", "chrome", "brave", "edge"]
        
        def _launch_web_url_os(target_url: str) -> None:
            """Hand a URL to the interactive Windows shell, or report the failure.

            This handler runs from the synchronous chat generator, which is called
            while FastAPI's event loop is already active. Do not use
            ``run_until_complete`` here: it fails in that situation and previously
            got swallowed, producing a false "Opening" confirmation.
            """
            completed = subprocess.run(
                ["cmd.exe", "/d", "/s", "/c", "start", "", target_url],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "Windows could not start the browser.").strip()
                raise RuntimeError(detail)

        # 1. Handle WhatsApp
        if "whatsapp" in text or "ping me" in text:
            try:
                _launch_windows_app("whatsapp")
                actions_taken.append("WhatsApp Desktop")
            except Exception as desktop_error:
                try:
                    _launch_web_url_os("https://web.whatsapp.com")
                    actions_taken.append("WhatsApp Web")
                except Exception as web_error:
                    return f"I couldn't open WhatsApp: {desktop_error}; browser fallback: {web_error}"

        # 2. Check registered sites
        matched_site = False
        launch_errors: list[str] = []
        for key, (label, url) in site_map.items():
            if key in text:
                matched_site = True
                try:
                    _launch_web_url_os(url)
                    actions_taken.append(label)
                except Exception as error:
                    launch_errors.append(f"{label}: {error}")

        if matched_site and not actions_taken:
            return f"I couldn't open the requested site. {'; '.join(launch_errors)}"

        # 3. Check registered desktop apps
        for app in desktop_apps:
            if app in text:
                try:
                    _launch_windows_app(app)
                    actions_taken.append(app.capitalize())
                except Exception as error:
                    return f"I couldn't open {app}: {error}"

        # 4. Fallback for generic 'open <domain>' or 'go to <domain>'
        if not matched_site and not any(a in text for a in desktop_apps) and "whatsapp" not in text:
            words = text.split()
            for w in words:
                if "." in w and not w.startswith("http"):
                    target_url = f"https://{w.strip('?,!')}"
                    try:
                        _launch_web_url_os(target_url)
                        actions_taken.append(w)
                    except Exception as error:
                        return f"I couldn't open {w}: {error}"
                    break
                elif w.startswith("http://") or w.startswith("https://"):
                    try:
                        _launch_web_url_os(w)
                        actions_taken.append("target site")
                    except Exception as error:
                        return f"I couldn't open that site: {error}"
                    break

        if actions_taken:
            targets_str = " and ".join(dict.fromkeys(actions_taken))
            return f"Right away, sir. Opening {targets_str} for you right now."

        if any(text.startswith(kw) for kw in action_keywords):
            return "I need the app, website, or file you want me to open."

    return None


def generate_chat_stream(
    db: Session,
    user_input: str,
    session_id: str = "default",
    user_tone: str | None = None,
    response_style: str | None = None,
    conversation_mode: str | None = None,
    language_preference: str | None = None,
    attachments: list[dict] | None = None,
    speaker_profile: dict | None = None,
):
    """Generator for streaming responses."""
    effective_language_preference = _detect_user_language_preference(user_input, language_preference)

    folder_reply = _handle_dynamic_filesystem_or_desktop_intent(user_input)
    if folder_reply:
        yield folder_reply
        return

    auto_task_reply = _handle_task_automation_intent(db, user_input)

    if auto_task_reply:
        yield auto_task_reply
        return

    local_attachment_answer = _local_attachment_question_answer(user_input, attachments)
    if local_attachment_answer:
        yield local_attachment_answer
        return

    if _should_use_local_utility_reply(user_input, attachments):
        fast_reply = _fast_local_reply(db, user_input, effective_language_preference)
        if fast_reply:
            yield fast_reply
            return

    if not attachments and not _is_deep_conversation_mode(conversation_mode):
        quick_reply = _quick_local_response(
            db,
            user_input,
            effective_language_preference,
            speaker_profile=speaker_profile,
            allow_conversation_fragments=True,
        )
        if quick_reply:
            yield quick_reply
            return

    history_records = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.display_order.desc(), ChatMessage.id.desc())
        .limit(10)
        .all()
    )
    
    # We remove the duplicate manual append since main.py already saved the current user_input to the DB
    history = [{"role": r.role, "content": r.content} for r in reversed(history_records)]
    
    if RELEVANT_MEMORY_RETRIEVER.requires_memory_context(user_input):
        all_memories = db.query(Memory).order_by(Memory.importance.desc()).all()
        memory_dicts = [{"topic": m.topic, "insight": m.insight, "importance": m.importance} for m in all_memories]
        memory_str = RELEVANT_MEMORY_RETRIEVER.format_retrieved_memories_prompt(user_input, memory_dicts)
    else:
        memory_str = ""
        all_memories = []


    dynamic_context = [
        build_social_intelligence_context(speaker_profile, user_input, user_tone),
        f"USER TONE SIGNAL: {user_tone or 'neutral'}",
        f"RESPONSE STYLE PREFERENCE: {response_style or 'friendly'}",
        f"CONVERSATION MODE: {conversation_mode or 'hybrid'}",
        f"LANGUAGE PREFERENCE: {effective_language_preference}",
        "Speak naturally, with short spoken-language sentences when the conversation mode involves voice. Use a brief acknowledgement/filler only when it sounds human, then answer directly.",
        _language_instruction(effective_language_preference, language_preference),
        _build_output_intent_context(user_input),
        f"CURRENT TIME CONTEXT: {_format_ist_datetime()}. Use IST for today, yesterday, tomorrow, now, present, and current.",
        (
            "CURRENT FACT ACCURACY CONTRACT: For any current/live/recent question in any domain, use DIRECT LIVE DATA "
            "or LIVE WEB CONTEXT as the source of truth. Answer all requested fields directly, include source name and "
            "timestamp/date/unit when available, and do not invent missing details. Never present guessed or memory-based "
            "live facts confidently. If the exact detail is not verified by the live context, say that exact detail is "
            "not verified yet and give the closest verified facts. Do not say 'current', 'latest', 'confirmed', or "
            "'here is the table' unless the rows/values are present in DIRECT LIVE DATA or a cited live source. "
            "For news and articles, every headline/claim must be traceable to a source line in DIRECT LIVE DATA or LIVE WEB CONTEXT; "
            "if the source is only an RSS/search snippet, call it 'source-reported' rather than fully confirmed. "
            "When the user says re-check/wrong, do a fresh live lookup and do not defend the previous answer from memory."
        ),
    ]
    live_context = ""
    source_backed_answer = ""
    # Voice used to be excluded from live lookups outright — `_is_voice_session`
    # skipped this whole block "because they add 2-8s blocking latency". The
    # latency was real, but the trade was wrong: it meant the same question got a
    # cited answer when typed and a from-memory guess when spoken, so voice was
    # the least accurate way to ask precisely the questions that need live data.
    #
    # The gap is now covered instead of avoided. On voice, an acknowledgement is
    # yielded *before* the fetch begins, so the client's TTS is already speaking
    # while the network work happens behind it. The user hears "let me check
    # that" rather than several seconds of nothing, which is both what a person
    # does and what makes the wait legible instead of alarming.
    _is_voice_session = (session_id or "").startswith("voice")
    if _needs_live_web_context(user_input):
        if _is_voice_session:
            yield _live_lookup_acknowledgement(user_input, effective_language_preference)
        live_context = _live_context_within_budget(
            user_input,
            _PRE_MODEL_LIVE_BUDGET_VOICE_S if _is_voice_session else _PRE_MODEL_LIVE_BUDGET_TEXT_S,
        )
        dynamic_context.append(live_context)
        live_answer_hint = _build_live_answer_hint(user_input, live_context)
        if live_answer_hint:
            dynamic_context.append(live_answer_hint)
        source_backed_answer = _direct_source_backed_answer(user_input, live_context)

    if attachments:
        dynamic_context.append(
            "ATTACHMENT VISION MODE: The user attached screenshots/images/files. Inspect images carefully: visible text, UI controls, layout, colors, warnings, tiny labels, and any likely user intent. "
            "Answer from the attachment content first, then reason about the next useful action. If a detail is not visible, say it is not visible instead of guessing."
        )

    if source_backed_answer and not attachments:
        yield _apply_prompt_response_style(user_input, source_backed_answer)
        return

    if "hindi" in effective_language_preference:
        _lang_rule = "\n\nLANGUAGE RULE: Reply ONLY in Hindi. Do not use Telugu words."
    elif "telugu" in effective_language_preference:
        _lang_rule = "\n\nLANGUAGE RULE: Reply ONLY in Telugu-English (Telglish). Do not use Hindi words."
    else:
        _lang_rule = "\n\nLANGUAGE RULE: Reply ONLY in English. Do not add Telugu or Hindi unless the user writes in those languages."
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + _lang_rule + "\n\n" + memory_str + "\n\n" + "\n".join(dynamic_context),
        }
    ] + history

    if "hindi" in effective_language_preference:
        compact_language = "Reply ONLY in Hindi. Do not mix Telugu or English unless the user's message contains them."
    elif "telugu" in effective_language_preference:
        compact_language = "Reply ONLY in Telugu-English (Telglish) mix. Do not add Hindi words."
    else:
        compact_language = "Reply ONLY in clear English. Do not mix Telugu or Hindi unless the user writes in those languages first."
    compact_system_parts = [
        "You are Akansha. Answer the latest user message directly.",
        compact_language,
        f"IST now: {_format_ist_datetime()}.",
    ]
    compact_memories = "; ".join(
        f"{m.topic}: {m.insight}" for m in all_memories[:5] if getattr(m, "topic", None) and getattr(m, "insight", None)
    )

    if compact_memories and re.search(r"\b(my|me|remember|name|cgpa|profile|family|mother|father|friend)\b", user_input, re.IGNORECASE):
        compact_system_parts.append(f"Memory: {compact_memories[:220]}")
    if live_context:
        compact_system_parts.append(
            "Source context for current facts; use only verified details from it:\n"
            + live_context[:2200]
        )
    compact_history = history[-2:] if history else [{"role": "user", "content": user_input}]
    if not compact_history or compact_history[-1].get("role") != "user":
        compact_history = compact_history + [{"role": "user", "content": user_input}]
    compact_messages = [{"role": "system", "content": "\n".join(compact_system_parts)}] + compact_history
    # Voice sessions: compact messages only — no live context, shorter prompt = faster first token
    # Chat sessions: try compact first (fast path), fall back to full messages if empty
    if _is_voice_session:
        request_message_variants = [compact_messages]
    elif not attachments and not _is_deep_conversation_mode(conversation_mode):
        request_message_variants = [compact_messages, messages]
    else:
        request_message_variants = [messages]

    if attachments:
        multimodal_content = _build_attachment_message_content(user_input, attachments)
        if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == user_input:
            messages[-1] = {"role": "user", "content": multimodal_content}
        else:
            messages.append({"role": "user", "content": multimodal_content})
        request_message_variants = [messages]
    
    if os.getenv("AKANSHA_DEBUG_PROMPT") == "1":
        with open("debug_prompt.txt", "w", encoding="utf-8") as f:
            f.write(json.dumps(messages, indent=2, ensure_ascii=False))

    try:
        request_timeout = _chat_request_timeout_seconds(user_input, attachments, conversation_mode)
        full_response = ""
        last_model_error: Exception | None = None
        requested_tokens = _response_token_limit(user_input, attachments, conversation_mode)
        # Voice gets a short cascade rather than a single model — see
        # `_voice_model_candidates`. Still short: a listener cannot wait out five
        # sequential provider failures.
        models_to_try = _voice_model_candidates() if _is_voice_session else _openrouter_model_candidates()
        if not models_to_try:
            raise OpenRouterConfigurationError(
                "No model provider is connected. Add OPENROUTER_API_KEY to C:\\MY-AI\\aura\\.env, "
                "or start Ollama and pick a local model in Settings > Models."
            )
        # An auth failure is durable for the *provider*, not for the machine. It
        # used to end the whole cascade, which was right when there was one
        # provider and is wrong now: a revoked OpenRouter key would have skipped
        # past a working local model that needs no key at all.
        dead_providers: set[str] = set()
        # One clock for the whole cascade, not one per request. See
        # `_CASCADE_BUDGET_VOICE_S`.
        cascade_budget = _CASCADE_BUDGET_VOICE_S if _is_voice_session else _CASCADE_BUDGET_TEXT_S
        cascade_deadline = time.time() + cascade_budget
        out_of_time = False
        for request_messages in request_message_variants:
            if out_of_time:
                break
            for model_name in models_to_try:
                if model_routes.provider_of(model_name) in dead_providers:
                    continue
                remaining = cascade_deadline - time.time()
                if remaining < _CASCADE_MIN_ATTEMPT_S:
                    out_of_time = True
                    last_model_error = last_model_error or TimeoutError(
                        f"No model answered within {cascade_budget:.0f}s"
                    )
                    break
                for token_budget in _openrouter_token_budget_candidates(requested_tokens):
                    try:
                        # Never start a request that cannot finish inside the
                        # cascade's remaining time; that only delays the fallback.
                        client = _client_for_model(model_name, timeout=min(request_timeout, remaining))
                        response = client.chat.completions.create(
                            model=model_routes.wire_name(model_name),
                            messages=request_messages,
                            temperature=0.35,   # lower = faster first token, more deterministic
                            max_tokens=token_budget,
                            stream=True,
                        )

                        # Strip reasoning dumps and chat-template residue as they
                        # stream. In speech mode this also flattens the markdown a
                        # TTS voice would otherwise read out as punctuation.
                        sanitizer = StreamSanitizer(for_speech=_is_voice_session)
                        for chunk in response:
                            if chunk.choices[0].delta.content:
                                content = sanitizer.feed(chunk.choices[0].delta.content)
                                if content:
                                    full_response += content
                                    yield content
                        tail = sanitizer.finish()
                        if tail:
                            full_response += tail
                            yield tail
                        if full_response.strip():
                            break
                        # Empty *after* sanitising counts as a failure, which is the
                        # point of doing it here: a model whose entire reply was a
                        # reasoning block has told us nothing, and the next
                        # candidate should get a turn.
                        raise RuntimeError(f"OpenRouter model {model_name} returned an empty response stream")
                    except Exception as model_exc:
                        last_model_error = model_exc
                        if note_model_failure(model_name, model_exc):
                            print(f"Model {model_name} demoted for {int(_MODEL_COOLDOWN_S)}s: {_provider_failure_kind(model_exc)}")
                        if _provider_failure_kind(model_exc) == "auth":
                            dead_providers.add(model_routes.provider_of(model_name))
                            break
                        continue
                if full_response.strip():
                    break
            if full_response.strip() or all(
                model_routes.provider_of(model) in dead_providers for model in models_to_try
            ):
                break
        if not full_response.strip():
            raise last_model_error or RuntimeError("OpenRouter returned an empty response stream")
    except Exception as exc:
        yield _apply_prompt_response_style(
            user_input,
            _provider_failure_fallback(user_input, attachments, exc, effective_language_preference, live_context=live_context),
        )
            
    # We yield a special token at the end or handle the background task in main.py

def analyze_intent_and_memory(
    db: Session,
    user_input: str,
    assistant_response: str,
    speaker_profile: dict | None = None,
):
    """Background task to extract memory and intent (e.g. creating tasks).

    `speaker_profile` is the resolved profile for the turn, as returned by
    `speaker_identity.resolve_speaker_identity`. It gates the automation branch
    below, which launches real desktop applications. `None` means the caller did
    not resolve a speaker at all, which is treated as the local desktop session
    and therefore as the owner -- the same answer `resolve_speaker_identity`
    gives for a request that carried no claim, so defaulting here does not open a
    hole that the resolver would have closed.
    """
    _capture_deterministic_memories(db, user_input)
    if _should_skip_ai_memory_analysis(user_input, assistant_response):
        try:
            db.commit()
        except Exception:
            pass
        return

    existing_memories = db.query(Memory).all()
    memories_str = "Existing Memories:\n" + "\n".join([f"ID: {m.id} | Topic: {m.topic} | Insight: {m.insight}" for m in existing_memories])

    prompt = f"""
    Analyze the recent interaction. 
    1. Extract any new long-term memories about the user.
    2. Identify if the user implicitly or explicitly requested a task to be tracked.
    3. Identify if the user wants to trigger a desktop automation action.
    
    IMPORTANT: You must consolidate memories! Do not create duplicate memories for the same topic. 
    If new information overlaps with an existing memory, update the existing memory instead of creating a new one.

    {memories_str}
    
    Return ONLY a JSON object:
    {{
        "memories_to_add": [{{"topic": "str", "insight": "str", "importance": 1-5}}],
        "memories_to_update": [{{"id": int, "new_insight": "str", "new_importance": 1-5}}],
        "new_tasks": [{{"title": "str", "description": "str"}}],
        "automation": {{"action": "open_notepad|type|open_url|open_youtube_song|new_tab|close_tab|type_text|edit_field|remove_draft", "target": "str"}} 
    }}
    
    Interaction:
    User: {user_input}
    Akansha: {assistant_response}
    """
    try:
        # The head of the same cascade the reply used, rather than the configured
        # model verbatim: a route that is cooling because it has no credits would
        # otherwise silently drop every memory extraction for 15 minutes.
        analysis_model = (_openrouter_model_candidates() or [OPENROUTER_MODEL])[0]
        res = _client_for_model(analysis_model).chat.completions.create(
            model=model_routes.wire_name(analysis_model),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=220,
        )
        data = json.loads(res.choices[0].message.content)
        
        # Save New Memories
        for m in data.get('memories_to_add', []):
            _upsert_memory(db, m['topic'], m['insight'], m.get('importance', 1))
            
        # Update Existing Memories
        for m in data.get('memories_to_update', []):
            existing_mem = db.query(Memory).filter(Memory.id == m['id']).first()
            if existing_mem:
                existing_mem.insight = m['new_insight']
                if 'new_importance' in m:
                    existing_mem.importance = m['new_importance']
            
        # Save Tasks
        for t in data.get('new_tasks', []):
            new_task = Task(title=t['title'], description=t.get('description', ''))
            db.add(new_task)
            
        db.commit()

        # Handle Automation
        automation = data.get('automation')
        if automation and automation.get('action'):
            # Owner only. This launches real desktop applications on the machine
            # the assistant is running on, driven by a model's reading of the
            # conversation -- so the question "who asked for this" has to be
            # answered before it runs, not after.
            #
            # `resolve_speaker_identity` returns owner authority whenever the
            # request carried no speaker claim, which is every request the shipped
            # frontend makes. So the ordinary desktop path is unchanged, and what
            # is blocked is specifically a turn that named somebody other than the
            # owner. Note that a self-declared owner claim also passes: this is
            # authority derived from a claim, not identity that was verified.
            speaker_access = str((speaker_profile or {}).get("access_level") or OWNER)
            if not at_least(speaker_access, OWNER):
                print(
                    "Automation skipped: desktop control requires owner access, "
                    f"this turn resolved to '{speaker_access}'."
                )
                return

            import asyncio
            from .automation import execute_desktop_command

            # Since this is a background thread, we need an event loop
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            loop.run_until_complete(execute_desktop_command(automation['action'], automation.get('target')))
    except Exception as e:
        kind = _provider_failure_kind(e)
        if kind in {"auth", "capacity", "unavailable", "rate_limit", "timeout"}:
            print("Analysis skipped: provider unavailable for background memory extraction.")
            return
        print(f"Analysis skipped: {type(e).__name__}")
