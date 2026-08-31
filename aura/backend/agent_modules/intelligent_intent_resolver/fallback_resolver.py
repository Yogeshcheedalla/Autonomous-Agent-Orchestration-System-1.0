"""FallbackIntentResolver — three-level fallback when LLM is unavailable."""
from __future__ import annotations
import re
from .models import IntentResolutionResult, COMMON_ENTITIES


class FallbackIntentResolver:
    def __init__(self) -> None:
        self.url_pattern = re.compile(
            r'https?://[^\s]+|([a-zA-Z0-9-]+\.(com|org|net|edu|in|io|dev))'
        )

    def resolve_simple(self, text: str) -> IntentResolutionResult:
        # Level 1: direct URL extraction
        url_match = self.url_pattern.search(text or "")
        if url_match:
            url = url_match.group(0)
            if not url.startswith("http"):
                url = f"https://{url}"
            return IntentResolutionResult(
                resolved_intent=f"navigate to {url}",
                target_entity=url,
                entity_type="website",
                canonical_url=url,
                confidence=0.75,
                resolved_by="fallback",
            )
        # Level 2: keyword matching
        text_lower = (text or "").lower()
        for keyword, url in COMMON_ENTITIES.items():
            if keyword in text_lower:
                return IntentResolutionResult(
                    resolved_intent=f"navigate to {keyword.title()}",
                    target_entity=keyword.title(),
                    entity_type="website",
                    canonical_url=url,
                    confidence=0.65,
                    resolved_by="fallback",
                )
        # Level 3: generic query
        return IntentResolutionResult(
            resolved_intent=f"query: {text}",
            target_entity=None,
            entity_type="query",
            canonical_url=None,
            confidence=0.3,
            resolved_by="fallback",
            reasoning_trace=f"No pattern matched for input: {(text or '')[:100]}",
        )
