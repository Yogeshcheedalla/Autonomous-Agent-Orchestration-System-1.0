"""EntityExtractor — identifies and resolves entity references from natural language."""
from __future__ import annotations
import re
import logging
from typing import Any, Dict, List, Optional
from .models import ExtractedEntity, COMMON_ENTITIES

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 0.85


class EntityExtractor:
    def __init__(self, context_manager=None) -> None:
        self.context_manager = context_manager

    def extract_entities(self, text: str, language_context=None) -> List[ExtractedEntity]:
        entities: List[ExtractedEntity] = []
        text_lower = text.lower() if text else ""
        # Direct URL detection
        url_match = re.search(r'https?://\S+', text or "")
        if url_match:
            url = url_match.group(0)
            entities.append(ExtractedEntity(
                name=url, type="website", canonical_name=url,
                url=url, confidence=0.99,
            ))
            return entities
        # Brand name / common entity detection
        for entity_name, url in COMMON_ENTITIES.items():
            if entity_name in text_lower:
                entities.append(ExtractedEntity(
                    name=entity_name.title(), type="website",
                    canonical_name=entity_name.title(),
                    url=url, confidence=0.95,
                ))
        # Sort by confidence descending
        entities.sort(key=lambda e: e.confidence, reverse=True)
        return entities if entities else [ExtractedEntity(
            name=text or "", type="query", canonical_name=text or "",
            url=None, confidence=0.3,
        )]

    def resolve_website(self, entity_name: str, context: Dict[str, Any]) -> Optional[str]:
        """Priority: user corrections → recent activity → common entities → None."""
        if not entity_name:
            return None
        # Priority 1: user corrections
        corrections = context.get("corrections", {})
        if entity_name in corrections:
            return corrections[entity_name]
        # Priority 2: recent activity (fuzzy match > 0.85)
        recent = context.get("recent_entities", [])
        for item in recent:
            name = item[0] if isinstance(item, (list, tuple)) else str(item)
            if self._fuzzy_match(entity_name.lower(), name.lower()) > FUZZY_MATCH_THRESHOLD:
                if isinstance(item, (list, tuple)) and len(item) > 2:
                    return item[2]  # url at index 2
        # Priority 3: common entities dict
        entity_lower = entity_name.lower().strip()
        if entity_lower in COMMON_ENTITIES:
            return COMMON_ENTITIES[entity_lower]
        # Priority 4: try to construct from entity name
        for key in COMMON_ENTITIES:
            if key in entity_lower or entity_lower in key:
                return COMMON_ENTITIES[key]
        return None

    @staticmethod
    def _fuzzy_match(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        longer = a if len(a) >= len(b) else b
        shorter = b if len(a) >= len(b) else a
        if longer in shorter or shorter in longer:
            return len(shorter) / len(longer)
        # Simple character overlap
        matches = sum(1 for c in shorter if c in longer)
        return matches / len(longer)
