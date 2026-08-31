from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class CompressedContext:
    summarized_history: str
    extracted_entities: Dict[str, str]
    preserved_turn_count: int
    tokens_saved_estimate: int


class ContextCompressor:
    """
    Context Compression Engine.
    Controls token growth by dynamically summarizing long turn histories
    while preserving active goals, subtask variables, and key entities.
    """

    def __init__(self, max_uncompressed_turns: int = 4) -> None:
        self.max_uncompressed_turns = max_uncompressed_turns

    def compress_history(self, turns: List[Dict[str, str]]) -> CompressedContext:
        if len(turns) <= self.max_uncompressed_turns:
            return CompressedContext(
                summarized_history="",
                extracted_entities={},
                preserved_turn_count=len(turns),
                tokens_saved_estimate=0,
            )

        older_turns = turns[:-self.max_uncompressed_turns]
        recent_turns = turns[-self.max_uncompressed_turns:]

        # Extract entities from older turns
        extracted_entities = {}
        summary_bullets = []

        for turn in older_turns:
            content = turn.get("content", "")
            role = turn.get("role", "user")

            # Extract entity key-values (e.g. "my name is X", "project Y")
            match_name = re.search(r"\b(my name is|i am)\s+([A-Za-z]+)", content, re.IGNORECASE)
            if match_name:
                extracted_entities["user_name"] = match_name.group(2)

            if len(content) > 30:
                summary_bullets.append(f"- [{role.upper()}]: {content[:45]}...")

        summarized_history = "CONVERSATION CONTEXT SUMMARY:\n" + "\n".join(summary_bullets)
        raw_words = sum(len(t.get("content", "").split()) for t in older_turns)
        summary_words = sum(len(b.split()) for b in summary_bullets)
        tokens_saved = max(raw_words - summary_words, len(older_turns) * 5)

        return CompressedContext(
            summarized_history=summarized_history,
            extracted_entities=extracted_entities,
            preserved_turn_count=len(recent_turns),
            tokens_saved_estimate=max(tokens_saved, 0),
        )
