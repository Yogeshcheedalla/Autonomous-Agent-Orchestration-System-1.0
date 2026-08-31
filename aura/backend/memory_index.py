"""
Keeps the vector index and the `memories` table saying the same thing.

`semantic_memory` deliberately knows nothing about SQLAlchemy -- it stores ids,
text and vectors, and does not care where they came from. This module is the one
place that knows the mapping: a `Memory` row becomes one document whose text is
"topic. insight", because that is what a question is actually asked against.

Two jobs, and they are separate on purpose:

  sync()   -- reconcile the whole table, cheap when nothing changed, and safe to
              call from a startup thread.
  recall() -- one semantic query, returning rows the caller can rank against its
              own lexical hits.

The vector store is a *derived* index. It is never the source of truth, and
anything it holds that the table no longer does is pruned rather than trusted --
a memory the user asked Akansha to forget must not keep answering questions.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from . import semantic_memory
from .database import Memory, SessionLocal

_log = logging.getLogger(__name__)

#: Sync in batches rather than one 500-row embed. A batch pads to the longest
#: sequence *within the batch*, so mixing one long memory with 499 short ones
#: would make every one of them cost the long one's attention.
BATCH = 64


def _document(row: Any) -> str:
    """The text a question is matched against.

    Topic and insight joined, not insight alone: the topic is often the only
    place the subject is named ("Activa"), while the insight carries the
    predicate ("due for a service"). Either half alone loses half the query.
    """
    topic = (getattr(row, "topic", "") or "").strip()
    insight = (getattr(row, "insight", "") or "").strip()
    if topic and insight:
        return f"{topic}. {insight}"
    return topic or insight


def sync(db: Any = None, *, prune: bool = True) -> dict[str, Any]:
    """Bring the index in line with the table.

    Returns counts rather than raising. A failed sync must leave lexical recall
    working, because that is what every caller falls back to.
    """
    if not semantic_memory.available():
        return {"ok": False, "detail": semantic_memory.capability().detail}

    owned = db is None
    session = db or SessionLocal()
    try:
        rows = session.query(Memory).all()
    except Exception as exc:
        _log.warning("Memory sync could not read the table: %s", exc)
        if owned:
            session.close()
        return {"ok": False, "detail": str(exc)}

    records = []
    for row in rows:
        text = _document(row)
        if not text:
            continue
        records.append(
            {
                "id": str(row.id),
                "text": text,
                "metadata": {
                    "topic": (row.topic or "").strip(),
                    "importance": int(row.importance or 1),
                    "timestamp": row.timestamp.isoformat() if row.timestamp else "",
                },
            }
        )
    if owned:
        session.close()

    indexed = 0
    skipped = 0
    for start in range(0, len(records), BATCH):
        result = semantic_memory.index(records[start : start + BATCH])
        indexed += int(result.get("indexed") or 0)
        skipped += int(result.get("skipped") or 0)

    removed = 0
    if prune:
        live = {record["id"] for record in records}
        removed = _prune(live)

    return {"ok": True, "indexed": indexed, "skipped": skipped, "pruned": removed, "rows": len(records)}


def _prune(live_ids: set[str]) -> int:
    """Drop vectors whose memory no longer exists."""
    stats = semantic_memory.stats()
    if not stats.get("count"):
        return 0
    try:
        with semantic_memory._db() as connection:  # noqa: SLF001 - same package, one owner
            stored = {
                str(row[0])
                for row in connection.execute(
                    "SELECT doc_id FROM vectors WHERE collection = ?",
                    (semantic_memory.COLLECTION_MEMORY,),
                )
            }
    except Exception as exc:
        _log.warning("Memory prune could not list the index: %s", exc)
        return 0
    stale = stored - live_ids
    return semantic_memory.forget(stale) if stale else 0


def index_rows(rows: Sequence[Any]) -> int:
    """Index a handful of rows immediately, for the write path.

    Called after a memory is created or updated so the next question can find it
    without waiting for a full sync. Silent when the model is missing.
    """
    records = []
    for row in rows:
        text = _document(row)
        if text and getattr(row, "id", None) is not None:
            records.append(
                {
                    "id": str(row.id),
                    "text": text,
                    "metadata": {
                        "topic": (getattr(row, "topic", "") or "").strip(),
                        "importance": int(getattr(row, "importance", 1) or 1),
                    },
                }
            )
    if not records:
        return 0
    try:
        return int(semantic_memory.index(records).get("indexed") or 0)
    except Exception as exc:
        _log.warning("Memory index write failed: %s", exc)
        return 0


def forget_rows(ids: Iterable[Any]) -> int:
    """Remove vectors for memories deleted upstream."""
    try:
        return semantic_memory.forget([str(item) for item in ids])
    except Exception as exc:
        _log.warning("Memory index delete failed: %s", exc)
        return 0


def recall(query: str, *, limit: int = 5, min_similarity: float | None = None) -> list[dict[str, Any]]:
    """Semantic hits for `query`, as plain dicts the API layer can merge.

    Empty list when the model is unavailable -- never an exception, because every
    caller runs this alongside a `LIKE` query and the `LIKE` must still answer.
    """
    try:
        hits = semantic_memory.search(query, limit=limit, min_similarity=min_similarity)
    except Exception as exc:
        _log.warning("Semantic recall failed: %s", exc)
        return []
    return [
        {
            "id": hit.id,
            "text": hit.text,
            "similarity": round(hit.similarity, 4),
            "topic": str(hit.metadata.get("topic") or ""),
            "importance": int(hit.metadata.get("importance") or 1),
        }
        for hit in hits
    ]


def warm() -> dict[str, Any]:
    """Build the embedding session and do a first full sync."""
    capability = semantic_memory.warm()
    if not capability.usable:
        return {"ok": False, "detail": capability.detail}
    return {"ok": True, "detail": capability.detail, **sync()}
