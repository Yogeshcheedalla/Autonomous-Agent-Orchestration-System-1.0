from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LanguageDetectionResult:
    primary_language: str  # "telugu", "hindi", "english", "mixed"
    detected_dialect: str  # "telugu", "telglish", "hindi", "hinglish", "english"
    contains_telugu_script: bool
    contains_hindi_script: bool
    honorific_suffix: str  # "-garu", "-ji", ""
    suggested_slang: List[str]


class MultilingualEngine:
    """
    Multilingual & Code-Switching Engine.
    Processes native intent and code-switching inputs across Telugu, English, Hindi, Telglish, and Hinglish.
    Injects authentic conversational slang and enforces regional honorifics without robotic transitions.
    """

    TELUGU_SLANG = ["ఏంటి సంగతులు", "బాగున్నావా", "మామా", "సరేరా", "చూద్దాంలే", "ఏరా"]
    HINDI_SLANG = ["क्या हाल है", "भाई", "ठीक है", "बताओ", "अरे"]

    TELUGU_SCRIPT_PATTERN = re.compile(r"[\u0C00-\u0C7F]")
    HINDI_SCRIPT_PATTERN = re.compile(r"[\u0900-\u097F]")
    TELGLISH_KEYWORDS = {"bagunnava", "enti", "sangatulu", "mama", "sarera", "chuddamle", "namaskaram", "nanna", "amma", "garu"}
    HINGLISH_KEYWORDS = {"kaise", "ho", "bhai", "theek", "hai", "batao", "kya", "namaste", "ji"}

    def detect_language(self, user_input: str) -> LanguageDetectionResult:
        text = user_input.strip()
        has_telugu = bool(self.TELUGU_SCRIPT_PATTERN.search(text))
        has_hindi = bool(self.HINDI_SCRIPT_PATTERN.search(text))

        words = set(re.findall(r"\w+", text.lower()))
        has_telglish = bool(words & self.TELGLISH_KEYWORDS)
        has_hinglish = bool(words & self.HINGLISH_KEYWORDS)

        if has_telugu:
            primary = "telugu"
            dialect = "telugu"
            honorific = "-garu"
            slang = self.TELUGU_SLANG
        elif has_hindi:
            primary = "hindi"
            dialect = "hindi"
            honorific = "-ji"
            slang = self.HINDI_SLANG
        elif has_telglish:
            primary = "telugu"
            dialect = "telglish"
            honorific = "-garu"
            slang = self.TELUGU_SLANG
        elif has_hinglish:
            primary = "hindi"
            dialect = "hinglish"
            honorific = "-ji"
            slang = self.HINDI_SLANG
        else:
            primary = "english"
            dialect = "english"
            honorific = ""
            slang = []

        return LanguageDetectionResult(
            primary_language=primary,
            detected_dialect=dialect,
            contains_telugu_script=has_telugu,
            contains_hindi_script=has_hindi,
            honorific_suffix=honorific,
            suggested_slang=slang,
        )

    def build_multilingual_prompt_context(self, user_input: str) -> str:
        lang_res = self.detect_language(user_input)
        parts = [f"DETECTED LANGUAGE/DIALECT: {lang_res.detected_dialect.upper()}"]

        if lang_res.primary_language == "telugu":
            parts.append(
                "MULTILINGUAL MANDATE: Respond in authentic Telugu or Telglish. "
                "Use conversational slang naturally (e.g. ఏంటి సంగతులు, బాగున్నావా, మామా, సరేరా) and apply '-garu' honorific when applicable."
            )
        elif lang_res.primary_language == "hindi":
            parts.append(
                "MULTILINGUAL MANDATE: Respond in authentic Hindi or Hinglish. "
                "Use natural conversational expressions (e.g. क्या हाल है, भाई, ठीक है) and apply '-ji' honorific when applicable."
            )

        return "\n".join(parts)
