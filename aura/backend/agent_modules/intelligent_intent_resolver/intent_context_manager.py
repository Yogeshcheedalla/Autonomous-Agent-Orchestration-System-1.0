"""IntentContextManager — maintains user context and preferences for IIR."""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, List, Optional
from .models import UserPreferences

logger = logging.getLogger(__name__)


class IntentContextManager:
    def __init__(self, db_session=None) -> None:
        self.db = db_session
        self.preferences_cache: Dict[str, UserPreferences] = {}

    def get_user_context(self, user_id: str = "default") -> Dict[str, Any]:
        prefs = self.preferences_cache.get(user_id)
        if prefs is None:
            prefs = UserPreferences(user_id=user_id)
            self.preferences_cache[user_id] = prefs
        return {
            "user_id": user_id,
            "frequent_entities": list(prefs.frequently_accessed.keys())[:5],
            "recent_entities": prefs.recent_entities[:10],
            "corrections": prefs.corrections,
            "language_preference": prefs.language_preference,
            "active_workflow": None,
        }

    def record_resolution(self, user_id: str, resolution, confirmed: bool = False) -> None:
        prefs = self.preferences_cache.get(user_id) or UserPreferences(user_id=user_id)
        if resolution.target_entity:
            prefs.frequently_accessed[resolution.target_entity] = (
                prefs.frequently_accessed.get(resolution.target_entity, 0) + 1
            )
            prefs.recent_entities.insert(
                0, (resolution.target_entity, time.time())
            )
            prefs.recent_entities = prefs.recent_entities[:10]
        self.preferences_cache[user_id] = prefs

    def update_preferences(self, user_id: str, entity: str, action: str) -> None:
        prefs = self.preferences_cache.get(user_id) or UserPreferences(user_id=user_id)
        prefs.frequently_accessed[entity] = prefs.frequently_accessed.get(entity, 0) + 1
        self.preferences_cache[user_id] = prefs

    def record_correction(self, user_id: str, misheard: str, correct: str) -> None:
        prefs = self.preferences_cache.get(user_id) or UserPreferences(user_id=user_id)
        prefs.corrections[misheard] = correct
        self.preferences_cache[user_id] = prefs
