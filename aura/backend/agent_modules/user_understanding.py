from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UserPreference:
    key: str
    value: Any
    updated_at: float = field(default_factory=time.time)
    source: str = "explicit_user_setting"  # explicit_user_setting or user_feedback


@dataclass
class FeedbackEntry:
    id: str
    user_input: str
    feedback_type: str  # positive, negative, correction, preference_update
    details: str
    timestamp: float = field(default_factory=time.time)


class UserUnderstandingModule:
    """
    Module: User Understanding
    Objectives:
    - Learn each user's preferences and communication style (voice vs text mode, language/dialect, tone).
    - Maintain contextual continuity across turns.
    - Rely on EXPLICIT user feedback and adjustable preferences (no hidden, unpredictable self-modification).
    """

    def __init__(self) -> None:
        self.preferences: Dict[str, UserPreference] = {
            "communication_mode": UserPreference("communication_mode", "text"),
            "language_preference": UserPreference("language_preference", "English"),
            "verbosity": UserPreference("verbosity", "concise"),
            "tone": UserPreference("tone", "professional_friendly"),
            "code_style": UserPreference("code_style", "clean_modular"),
        }
        self.feedback_log: List[FeedbackEntry] = []
        self.active_context: Dict[str, Any] = {}

    def get_preference(self, key: str, default: Any = None) -> Any:
        pref = self.preferences.get(key)
        return pref.value if pref else default

    def set_explicit_preference(self, key: str, value: Any, source: str = "explicit_user_setting") -> UserPreference:
        """Set or adjust user preference explicitly."""
        pref = UserPreference(key=key, value=value, source=source)
        self.preferences[key] = pref
        return pref

    def process_explicit_feedback(self, feedback_type: str, user_input: str, details: str) -> FeedbackEntry:
        """
        Process explicit feedback provided by the user and update preferences accordingly.
        Rely on transparent, inspectable feedback records rather than hidden model weight adjustments.
        """
        entry = FeedbackEntry(
            id=f"fb_{len(self.feedback_log)+1}",
            user_input=user_input,
            feedback_type=feedback_type,
            details=details,
        )
        self.feedback_log.append(entry)

        # Handle explicit corrections or style updates
        details_lower = details.lower()
        if "be concise" in details_lower or "shorter" in details_lower:
            self.set_explicit_preference("verbosity", "concise", source="user_feedback")
        elif "be detailed" in details_lower or "explain step by step" in details_lower:
            self.set_explicit_preference("verbosity", "detailed", source="user_feedback")
        elif "telugu" in details_lower:
            self.set_explicit_preference("language_preference", "telugu_english", source="user_feedback")
        elif "hindi" in details_lower:
            self.set_explicit_preference("language_preference", "hindi", source="user_feedback")

        return entry

    def adapt_response_style(self, content: str, mode: str = "text") -> str:
        """
        Adapt generated response based on user communication mode and preferences.
        """
        if mode == "voice":
            # Strip markdown and bullet points for smooth acoustic TTS rendering
            lines = content.splitlines()
            clean_lines = [l.lstrip("#*- 1234567890.").strip() for l in lines if l.strip()]
            return " ".join(clean_lines)

        verbosity = self.get_preference("verbosity", "concise")
        if verbosity == "concise" and len(content.split()) > 350:
            # Add a concise summary header if verbose
            pass

        return content

    def system_prompt_section(self) -> str:
        prefs_summary = ", ".join(f"{k}={v.value}" for k, v in self.preferences.items())
        return f"""
# MODULE: USER UNDERSTANDING
- CONTEXT & STYLE ADAPTATION: Adapt output cadence, tone, and language to user preferences (Current preferences: {prefs_summary}).
- EXPLICIT FEEDBACK ENGINE: Learning and behavioral changes rely on explicit feedback and adjustable preference settings, NOT hidden self-modification.
- VOICE VS TEXT MODE: In voice mode, output natural, spoken text without markdown syntax. In text mode, provide structured, scannable markdown responses.
"""
