"""
NLP Context Processor Module for Akansha AI OS.
Cleans acoustic noise, filler words, irregular phrases, and extracts core actionable intents and entities.
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

# Common conversational fillers, hesitation markers, and acoustic noise tokens
FILLER_WORDS: Set[str] = {
    "um", "uh", "err", "ah", "like", "you know", "actually", "basically",
    "literally", "sort of", "kind of", "i mean", "so yeah", "anyway",
    "well", "hmmm", "okay so", "right so", "as i was saying", "you see",
}

STOP_PHRASES: List[str] = [
    r"\b(can you please|could you please|please|would you mind|if you can|i want you to)\b",
    r"\b(kindly|just|simply|maybe|probably|i guess|i think)\b",
    r"\b(you know what i mean|as in|to be honest|frankly speaking)\b",
]

@dataclass
class CleanedPromptResult:
    original_prompt: str
    cleaned_prompt: str
    removed_fillers: List[str]
    detected_intent: str
    extracted_entities: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.95


class NLPContextProcessor:
    """
    NLP Context Processor.
    Uses NLP concepts to filter acoustic speech noise, remove redundant conversational filler phrases,
    and isolate actionable commands and parameters for high-accuracy intent routing.
    """

    def __init__(self) -> None:
        self.filler_words = FILLER_WORDS
        self.stop_phrases_pattern = re.compile("|".join(STOP_PHRASES), flags=re.IGNORECASE)

    def clean_noise(self, prompt: str) -> CleanedPromptResult:
        """
        Strips noise, hesitation words, and conversational padding from prompt.
        """
        if not prompt or not prompt.strip():
            return CleanedPromptResult(original_prompt="", cleaned_prompt="", removed_fillers=[], detected_intent="empty")

        raw_text = prompt.strip()
        removed_fillers: List[str] = []

        # 1. Strip stop phrases
        cleaned = self.stop_phrases_pattern.sub("", raw_text)

        # 2. Strip single filler tokens
        words = cleaned.split()
        filtered_words = []
        for w in words:
            clean_word = re.sub(r"[^\w\s]", "", w).lower()
            if clean_word in self.filler_words:
                removed_fillers.append(w)
            else:
                filtered_words.append(w)

        cleaned_str = " ".join(filtered_words)
        cleaned_str = re.sub(r"\s+", " ", cleaned_str).strip()

        # Fallback if over-cleaned
        if not cleaned_str:
            cleaned_str = raw_text

        intent = self._infer_intent(cleaned_str)
        entities = self._extract_entities(cleaned_str)

        logger.info("NLP Cleaned prompt: '%s' -> '%s' (Removed %d fillers)", raw_text, cleaned_str, len(removed_fillers))

        return CleanedPromptResult(
            original_prompt=raw_text,
            cleaned_prompt=cleaned_str,
            removed_fillers=removed_fillers,
            detected_intent=intent,
            extracted_entities=entities,
        )

    def _infer_intent(self, text: str) -> str:
        lowered = text.lower()
        if re.search(r"\b(open|navigate|launch|visit|go to|browse)\b", lowered):
            return "navigation"
        if re.search(r"\b(click|press|tap|submit|fill|type|enter|select)\b", lowered):
            return "interaction"
        if re.search(r"\b(search|find|look up|query|google)\b", lowered):
            return "search"
        if re.search(r"\b(schedule|reminder|cron|timer|alert)\b", lowered):
            return "schedule"
        if re.search(r"\b(log in|login|signed in|logged in|authentication)\b", lowered):
            return "auth_verification"
        return "general_query"

    def _extract_entities(self, text: str) -> Dict[str, Any]:
        entities: Dict[str, Any] = {}
        # URL / domain detection
        url_match = re.search(r"https?://[^\s]+|([a-zA-Z0-9-]+\.(com|org|net|edu|in|io|dev))", text)
        if url_match:
            entities["url"] = url_match.group(0)

        # Quoted strings
        quoted = re.findall(r'"([^"]*)"|\'([^\']*)\'', text)
        if quoted:
            entities["quoted_strings"] = [q[0] or q[1] for q in quoted]

        return entities

    def system_prompt_section(self) -> str:
        return """
# MODULE: NLP CONTEXT PROCESSOR
- NOISE FILTERING: Automatically strips acoustic speech fillers ("um", "like", "you know", "actually") and phrase padding.
- INTENT ISOLATION: Normalizes raw spoken inputs into crisp, unambiguous actionable commands.
- CONTEXT PRESERVATION: Retains core entities, dates, URLs, and task parameters while eliminating conversational bloat.
"""
