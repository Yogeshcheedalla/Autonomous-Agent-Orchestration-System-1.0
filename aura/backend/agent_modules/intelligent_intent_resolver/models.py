from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time

CLARIFICATION_THRESHOLD = 0.7

SENSITIVE_PATTERNS = [
    r'password[:\s]+\S+',
    r'api[_-]?key[:\s]+\S+',
    r'token[:\s]+\S+',
    r'\b\d{3}-\d{2}-\d{4}\b',
    r'\b\d{16}\b',
]

COMMON_ENTITIES = {
    "youtube": "https://www.youtube.com",
    "github": "https://www.github.com",
    "linkedin": "https://www.linkedin.com",
    "codechef": "https://www.codechef.com",
    "google": "https://www.google.com",
    "leetcode": "https://www.leetcode.com",
    "coursera": "https://www.coursera.org",
    "instagram": "https://www.instagram.com",
    "twitter": "https://www.twitter.com",
    "stackoverflow": "https://stackoverflow.com",
}


@dataclass
class IntentResolutionResult:
    resolved_intent: str
    target_entity: Optional[str]
    entity_type: str  # "website", "application", "action", "query"
    canonical_url: Optional[str]
    confidence: float
    parameters: Dict[str, Any] = field(default_factory=dict)
    requires_clarification: bool = False
    clarification_options: List[str] = field(default_factory=list)
    reasoning_trace: str = ""
    resolution_time_ms: float = 0.0
    resolved_by: str = "llm"  # "llm", "cache", "fallback", "fast_path"
    language: str = "english"
    alternatives: List[str] = field(default_factory=list)
    requires_user_confirmation: bool = False
    resolution_confidence: float = 0.0

    def __post_init__(self):
        self.resolution_confidence = self.confidence


@dataclass
class ResolutionRequest:
    raw_transcript: str
    user_id: str = "default"
    session_id: str = "default"
    active_workflow: Optional[Any] = None
    language_hint: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExtractedEntity:
    name: str
    type: str  # "website", "desktop_app", "action"
    canonical_name: str
    url: Optional[str]
    confidence: float
    alternative_matches: List[tuple] = field(default_factory=list)


@dataclass
class ConfidenceScore:
    overall: float
    entity_confidence: float
    parameter_confidence: float
    context_alignment: float
    requires_clarification: bool
    risk_level: str = "low"  # "low", "medium", "high"


@dataclass
class UserPreferences:
    user_id: str
    frequently_accessed: Dict[str, int] = field(default_factory=dict)
    recent_entities: List[tuple] = field(default_factory=list)
    corrections: Dict[str, str] = field(default_factory=dict)
    language_preference: str = "english"
    disambiguation_history: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DisambiguationQuery:
    question: str
    options: List[str]
    original_input: str
    created_at: float = field(default_factory=time.time)


@dataclass
class EnrichedSpeechTaskSpec:
    cleaned_transcript: str
    intent_category: str
    target_action: str
    target_site: Optional[str]
    parameters: Dict[str, Any] = field(default_factory=dict)
    resolution_confidence: float = 0.0
    resolved_by: str = "llm"
    entity_type: str = "query"
    canonical_url: Optional[str] = None
    alternatives: List[str] = field(default_factory=list)
    requires_user_confirmation: bool = False
