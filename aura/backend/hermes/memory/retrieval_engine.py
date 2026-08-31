"""
Ranking what Hermes remembers, by meaning as well as by wording.

The `semantic` term in the score below used to be `cosine_similarity` over
`token_embedding` -- a normalised bag of words, so its similarity is word
overlap. That is a real signal and it is kept, but it is exactly 0 whenever the
question and the memory use different words for the same thing: "my scooter" and
"the Activa" score nothing, and that is the recall the user actually wanted.

So the term is now the better of the two: word overlap, or the sentence-embedding
cosine from `semantic_memory`. `max` rather than a blend, because they fail in
opposite directions -- lexical is confident on shared words and blind otherwise,
the embedding is the reverse -- and either one being high is evidence.

Indexing happens here, lazily, in the `hermes` collection. It is safe on the hot
path because unchanged content is recognised by hash and skipped: the first
recall after new memories pays one embed, every later one pays nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ... import semantic_memory
from ..database.store import CognitiveStore, cosine_similarity, loads, token_embedding, utc_now

_log = logging.getLogger(__name__)

#: Separate from the `memory` collection, which mirrors the SQLAlchemy
#: `memories` table. Two different stores with two different id spaces; sharing
#: one collection would let a Hermes memory_id collide with a Memory row id.
COLLECTION = "hermes"


def _days_old(timestamp: str | None) -> float:
    if not timestamp:
        return 365.0
    try:
        value = datetime.fromisoformat(timestamp)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - value).total_seconds() / 86400)
    except Exception:
        return 365.0


class MemoryRetrievalEngine:
    def __init__(self, store: CognitiveStore) -> None:
        self.store = store

    def _vector_scores(self, query: str, rows: list[dict[str, Any]]) -> dict[str, float]:
        """Sentence-embedding similarity per memory_id, empty when unavailable.

        Indexes first so a memory written a second ago is findable now. `index`
        skips anything whose content hash it already has, so the cost of this on a
        settled store is a dictionary comparison.
        """
        if not rows or not semantic_memory.available():
            return {}
        try:
            semantic_memory.index(
                [
                    {
                        "id": str(row["memory_id"]),
                        "text": str(row.get("content") or ""),
                        "metadata": {"category": str(row.get("category") or "")},
                    }
                    for row in rows
                    if str(row.get("content") or "").strip()
                ],
                collection=COLLECTION,
            )
            hits = semantic_memory.search(
                query,
                limit=max(10, min(len(rows), 50)),
                collection=COLLECTION,
                # No floor here. The caller is ranking, not filtering, and a 0.15
                # is still worth more than the 0.0 that word overlap gives a
                # correct-but-differently-worded memory.
                min_similarity=-1.0,
            )
        except Exception as exc:
            _log.warning("Hermes semantic recall unavailable: %s", exc)
            return {}
        return {hit.id: hit.similarity for hit in hits}

    def recall(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query_embedding = token_embedding(query)
        with self.store.connect(self.store.files.memories) as conn:
            rows = conn.execute(
                """
                SELECT * FROM long_term_memories
                WHERE archived = 0
                """
            ).fetchall()

            data_rows = [dict(row) for row in rows]
            vector_scores = self._vector_scores(query, data_rows)

            scored: list[dict[str, Any]] = []
            for data in data_rows:
                lexical = cosine_similarity(query_embedding, loads(data["embedding"], {}))
                vector = float(vector_scores.get(str(data["memory_id"]), 0.0))
                semantic = max(lexical, vector)
                recency = 1.0 / (1.0 + _days_old(data.get("last_accessed")) / 30.0)
                usage = min(1.0, float(data.get("usage_count") or 0) / 20.0)
                importance = max(0.0, min(1.0, float(data["importance_score"])))
                confidence = max(0.0, min(1.0, float(data["confidence"])))
                score = (
                    0.35 * semantic
                    + 0.20 * importance
                    + 0.15 * recency
                    + 0.15 * usage
                    + 0.15 * confidence
                )
                data["recall_score"] = round(score, 6)
                data["semantic_similarity"] = round(semantic, 6)
                # Kept apart so a caller debugging a bad recall can see which ear
                # heard it: a hit with lexical 0 and vector 0.4 is the case this
                # change exists for.
                data["lexical_similarity"] = round(lexical, 6)
                data["vector_similarity"] = round(vector, 6)
                scored.append(data)

            top = sorted(scored, key=lambda item: item["recall_score"], reverse=True)[: max(1, min(limit, 10))]
            now = utc_now()
            for item in top:
                conn.execute(
                    """
                    UPDATE long_term_memories
                    SET usage_count = usage_count + 1, last_accessed = ?
                    WHERE memory_id = ?
                    """,
                    (now, item["memory_id"]),
                )
        return top
