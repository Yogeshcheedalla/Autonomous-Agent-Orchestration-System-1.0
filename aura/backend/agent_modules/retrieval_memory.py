from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class RelevantMemoryRetriever:
    """
    Targeted Memory Retriever.
    Memory retrieval is OPTIONAL and invoked ONLY for queries that explicitly depend
    on user profile facts, previous interaction context, or memory keywords.
    For standard queries, memory retrieval is completely bypassed to eliminate DB overhead and prompt bloat.
    """

    MEMORY_DEPENDENT_PATTERNS = [
        r"\b(i|my|me|mine|our|us)\b",
        r"\b(remember|recap|recollect|recall|saved|profile|preference|favorite)\b",
        r"\b(who am i|my name|my age|my cgpa|my family|my mom|my dad|my friend|my email|my phone)\b",
        r"\b(earlier|previous|last time|as i said|like i mentioned|we discussed|you said|you told me)\b",
        r"\b(what did i|did i tell you|do you know my|have i told you)\b",
    ]

    def __init__(self, top_k: int = 3) -> None:
        self.top_k = top_k

    def requires_memory_context(self, query: str) -> bool:
        """Determines if query depends on user profile facts or previous interaction context."""
        if not query:
            return False
        q_lower = query.lower()
        return any(re.search(pat, q_lower) for pat in self.MEMORY_DEPENDENT_PATTERNS)

    def retrieve_relevant_memories(
        self,
        query: str,
        memories: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not query or not memories or not self.requires_memory_context(query):
            return []

        query_tokens = set(re.findall(r"\w+", query.lower()))
        # Stopwords filter
        stopwords = {
            "what", "is", "my", "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of", "with", "me", "tell",
            "show", "do", "i", "you", "we", "he", "she", "it", "they", "like", "want", "know", "have", "can", "will", "would",
            "about", "how", "when", "where", "why", "who", "which"
        }
        keywords = {kw for kw in query_tokens - stopwords if len(kw) > 2}

        if not keywords:
            sorted_m = sorted(memories, key=lambda m: m.get("importance", 0), reverse=True)
            return sorted_m[: self.top_k]

        scored_memories = []
        for m in memories:
            topic = str(m.get("topic", "")).lower()
            insight = str(m.get("insight", "")).lower()
            text = f"{topic} {insight}"

            # Calculate match score based on whole-word keyword matching
            score = 0
            for kw in keywords:
                pattern = rf"\b{re.escape(kw)}\b"
                if re.search(pattern, topic):
                    score += 3
                elif re.search(pattern, text):
                    score += 1

            if score > 0:
                scored_memories.append((score, m.get("importance", 0), m))

        # Sort by relevance score, then importance
        scored_memories.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored_memories[: self.top_k]]

    def format_retrieved_memories_prompt(self, query: str, memories: List[Dict[str, Any]]) -> str:
        if not self.requires_memory_context(query):
            return ""

        relevant = self.retrieve_relevant_memories(query, memories)
        if not relevant:
            return ""

        facts = [f"- {m.get('topic', 'Fact')}: {m.get('insight', '')}" for m in relevant]
        return "RELEVANT MEMORIES (CONTEXTUALLY LOADED):\n" + "\n".join(facts)
