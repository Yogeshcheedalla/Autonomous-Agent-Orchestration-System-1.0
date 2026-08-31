"""
Meaning-based recall, for the queries substring matching cannot answer.

`Memory.topic.ilike('%scooter%')` finds a memory about a scooter. It does not
find the memory that says "the Activa is due for a service", because the two
share no characters -- and that is the memory the user was asking for. Every
recall path in this codebase was a `LIKE`, so Akansha could only remember things
she had been told in the same words the question used.

This is the second index: sentence embeddings from all-MiniLM-L6-v2 on ONNX
Runtime. Same constraints as `voice_local` -- no torch, no GPU, no network at
query time, and a missing model reports itself rather than pretending.

ChromaDB was the obvious way to do this and is not what shipped. Measured on this
machine, same model, same machine, same query:

    chromadb collection.query()          1334 - 2654 ms
    chromadb embedding function alone      365 -  633 ms
    this module (model_O3, tight padding)    2.8 -   5.1 ms

Two causes, both avoidable. Chroma's tokenizer pads every input to its full 128
tokens, so a five-word question costs the same as a paragraph; and its query path
adds HNSW, a second SQLite database, and a telemetry client on top. Neither buys
anything here: a few hundred memories is a brute-force dot product, which is
exact where HNSW is approximate and takes under a millisecond. So the model is
kept, the wrapper is not -- along with the thirty packages it depends on,
including grpcio, kubernetes and six opentelemetry modules.

The scores are worth being honest about. "my scooter needs servicing" against
"the Activa is due for a service" is +0.238, and against "what is the capital of
France" is -0.007. +0.238 is a modest score, not a confident match; it is
decisive only because the alternative is below zero and `LIKE` scores both at
exactly nothing. Hits carry their similarity so callers can rank rather than
trust.

At 3.7 ms this does not have to be a fallback. It runs alongside the lexical
query, not after it, and the two result sets are merged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

_log = logging.getLogger(__name__)

#: Where the vectors live. Beside the SQLite database, because they are a derived
#: index of it: losing one without the other is a rebuild, not a data loss.
STORE_DIR = os.getenv(
    "AKANSHA_VECTOR_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "vector_store"),
)

MODEL_REPO = os.getenv("AKANSHA_EMBED_REPO", "sentence-transformers/all-MiniLM-L6-v2")

#: `model_O3` rather than `model` or a quantised build, and the choice was
#: measured rather than assumed. O3 is the graph-optimised fp32 export: 3.7 ms
#: median against 5.1 ms for plain fp32 and 21 ms for `qint8_avx512_vnni`. The
#: int8 build is 23 MB instead of 90 MB and scored no better on the pair above,
#: but it is only competitive when ONNX Runtime is pinned to four threads and is
#: five times slower when left to pick for itself -- a worse default for a laptop
#: whose core count we do not control.
MODEL_FILE = os.getenv("AKANSHA_EMBED_MODEL", "onnx/model_O3.onnx")
TOKENIZER_FILE = "tokenizer.json"

EMBED_DIMENSIONS = 384

#: Hard cap on tokens per text. The tokenizer's own config truncates at 128, and
#: cost is quadratic in sequence length through attention, so a long memory is
#: clipped rather than allowed to make every search slower.
MAX_TOKENS = 128

COLLECTION_MEMORY = "memory"

#: Below this a "match" is noise. Calibrated against the pair in the module
#: docstring: the true pair scored +0.238 and the unrelated pair -0.007, so the
#: line sits between them and nearer the bottom. At 0.20 an unrelated memory has
#: to be much better than chance to surface at all.
MIN_SIMILARITY = float(os.getenv("AKANSHA_VECTOR_MIN_SIMILARITY", "0.20"))

@dataclass(frozen=True)
class Capability:
    """What semantic recall can do here, and what to run if it cannot."""

    installed: bool
    ready: bool
    detail: str
    model: str = MODEL_REPO
    dimensions: int = EMBED_DIMENSIONS

    @property
    def usable(self) -> bool:
        return self.installed and self.ready

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "ready": self.ready,
            "detail": self.detail,
            "model": self.model,
            "dimensions": self.dimensions,
        }


@dataclass(frozen=True)
class Hit:
    """One remembered thing, with how well it actually matched."""

    id: str
    text: str
    similarity: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "similarity": round(self.similarity, 4),
            "metadata": dict(self.metadata),
        }


# One ORT session and one tokenizer for the life of the process. Building the
# session is ~700 ms and reading the tokenizer is ~40 ms; doing either per request
# would cost two hundred times more than the inference it enables.
_lock = threading.RLock()
_session: Any = None
_tokenizer: Any = None
_unavailable_reason: str | None = None

# The vector matrix, held in memory per collection and rebuilt lazily after a
# write. Brute force needs the whole matrix contiguous, and re-reading a few
# hundred BLOBs out of SQLite on every keystroke would undo the point of this.
_matrix_cache: dict[str, tuple[np.ndarray, list[str]]] = {}

def _model_paths(*, allow_download: bool) -> tuple[str, str] | None:
    """Locate the ONNX model and tokenizer, downloading only if allowed.

    `allow_download=False` is what the capability probe uses: answering "is this
    ready?" must not itself fetch 90 MB. The cache is HuggingFace's own, which
    `faster-whisper` already populates for its models, so there is no second
    cache directory to explain to anyone.
    """
    global _unavailable_reason
    try:
        from huggingface_hub import hf_hub_download  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on the environment
        _unavailable_reason = (
            f"huggingface_hub is not importable ({exc}). Run: pip install huggingface_hub"
        )
        return None
    try:
        kwargs = {} if allow_download else {"local_files_only": True}
        model = hf_hub_download(MODEL_REPO, MODEL_FILE, **kwargs)
        tokenizer = hf_hub_download(MODEL_REPO, TOKENIZER_FILE, **kwargs)
        return model, tokenizer
    except Exception as exc:
        _unavailable_reason = (
            f"embedding model not cached ({exc}). It is a 90 MB download; call warm() to fetch it."
            if not allow_download
            else f"embedding model could not be fetched ({exc})"
        )
        return None


def _encoder() -> tuple[Any, Any] | None:
    """The ORT session and tokenizer, built once.

    Returns None rather than raising: a machine without the model must fall back
    to lexical recall, not fail the request that asked for it.
    """
    global _session, _tokenizer, _unavailable_reason
    if _session is not None and _tokenizer is not None:
        return _session, _tokenizer
    with _lock:
        if _session is not None and _tokenizer is not None:
            return _session, _tokenizer
        paths = _model_paths(allow_download=True)
        if paths is None:
            return None
        model_path, tokenizer_path = paths
        try:
            import onnxruntime as ort  # noqa: PLC0415
            from tokenizers import Tokenizer  # noqa: PLC0415

            options = ort.SessionOptions()
            # 0 means "decide from the machine". Pinning a thread count was
            # measured as slower on this box for the fp32 graph, and the whole
            # point of leaving it at 0 is that we do not know the core count of
            # whatever machine this runs on next.
            options.intra_op_num_threads = int(os.getenv("AKANSHA_EMBED_THREADS", "0"))
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            _session = ort.InferenceSession(
                model_path, options, providers=["CPUExecutionProvider"]
            )
            _tokenizer = Tokenizer.from_file(tokenizer_path)
        except Exception as exc:
            _unavailable_reason = f"embedding session could not be built ({exc})"
            _log.warning("Semantic memory unavailable: %s", exc)
            _session = None
            _tokenizer = None
            return None
        return _session, _tokenizer


def embed(texts: Sequence[str]) -> np.ndarray | None:
    """Unit-normalised sentence vectors, or None when the model is unavailable.

    The padding here is the whole performance story. This tokenizer is configured
    to pad every input to 128 tokens, so encoding a five-word question and then
    feeding all 128 positions to the model costs the same as a full paragraph --
    that is the 500 ms the wrapper library spends. Trimming to the longest real
    sequence in the batch, which for a short query is about eight tokens, is what
    turns 500 ms into 3.7 ms. `attention_mask` is the authority on which tokens
    are real; `len(ids)` is not, because it counts the padding.
    """
    bodies = [(text or "").strip() for text in texts]
    if not any(bodies):
        return None
    built = _encoder()
    if built is None:
        return None
    session, tokenizer = built
    try:
        encoded = tokenizer.encode_batch(bodies)
        lengths = [max(1, min(int(sum(item.attention_mask)), MAX_TOKENS)) for item in encoded]
        width = max(lengths)
        ids = np.zeros((len(encoded), width), dtype=np.int64)
        mask = np.zeros((len(encoded), width), dtype=np.int64)
        for row, (item, length) in enumerate(zip(encoded, lengths)):
            ids[row, :length] = item.ids[:length]
            mask[row, :length] = 1
        feed: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
        if any(spec.name == "token_type_ids" for spec in session.get_inputs()):
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, feed)[0]
        # Mean pooling over real tokens only, then L2 normalise so a dot product
        # is the cosine. all-MiniLM-L6-v2 is trained with exactly this pooling;
        # taking the [CLS] vector instead gives measurably worse similarities.
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)
    except Exception as exc:
        _log.warning("Embedding failed: %s", exc)
        return None


def capability() -> Capability:
    """Report availability without building anything expensive."""
    try:
        import onnxruntime  # noqa: F401, PLC0415
        import tokenizers  # noqa: F401, PLC0415
    except Exception as exc:
        return Capability(
            installed=False,
            ready=False,
            detail=f"onnxruntime/tokenizers not importable ({exc}). Run: pip install onnxruntime tokenizers",
        )
    if _session is not None:
        return Capability(installed=True, ready=True, detail="Semantic recall ready.")
    cached = _model_paths(allow_download=False) is not None
    return Capability(
        installed=True,
        ready=cached,
        detail=(
            "Semantic recall ready."
            if cached
            else _unavailable_reason or "Embedding model not downloaded yet; call warm()."
        ),
    )


def available() -> bool:
    """True when a semantic search can be attempted at all."""
    return capability().installed


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    collection  TEXT    NOT NULL,
    doc_id      TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    fingerprint TEXT    NOT NULL,
    metadata    TEXT    NOT NULL DEFAULT '{}',
    vector      BLOB    NOT NULL,
    PRIMARY KEY (collection, doc_id)
);
"""


def _db() -> sqlite3.Connection:
    """A connection to the vector store, schema ensured.

    A new connection per call rather than a shared one: SQLite objects are not
    safe to move between threads, and this is read from request handlers and
    written from a background sync thread. Opening is microseconds; debugging a
    cross-thread handle is not.
    """
    os.makedirs(STORE_DIR, exist_ok=True)
    connection = sqlite3.connect(
        os.path.join(STORE_DIR, "semantic.sqlite3"), timeout=10.0
    )
    connection.executescript(_SCHEMA)
    return connection


def _fingerprint(text: str) -> str:
    """Content hash, so re-indexing an unchanged memory costs nothing.

    A full re-embed of five hundred memories is a couple of seconds of CPU, and
    the overwhelmingly common case at startup is that none of them changed.
    """
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


def _matrix(collection: str) -> tuple[np.ndarray, list[str]]:
    """Every vector in the collection as one contiguous array, plus its ids."""
    cached = _matrix_cache.get(collection)
    if cached is not None:
        return cached
    with _lock:
        cached = _matrix_cache.get(collection)
        if cached is not None:
            return cached
        ids: list[str] = []
        blobs: list[np.ndarray] = []
        try:
            with _db() as connection:
                rows = connection.execute(
                    "SELECT doc_id, vector FROM vectors WHERE collection = ? ORDER BY doc_id",
                    (collection,),
                ).fetchall()
            for doc_id, blob in rows:
                vector = np.frombuffer(blob, dtype=np.float32)
                if vector.size != EMBED_DIMENSIONS:
                    # A vector of the wrong width is from an older model. Skipping
                    # it keeps search working on the rest; the next index() call
                    # overwrites it with the right one.
                    continue
                ids.append(str(doc_id))
                blobs.append(vector)
        except Exception as exc:
            _log.warning("Vector store unreadable: %s", exc)
            ids, blobs = [], []
        matrix = (
            np.vstack(blobs) if blobs else np.zeros((0, EMBED_DIMENSIONS), dtype=np.float32)
        )
        built = (matrix, ids)
        _matrix_cache[collection] = built
        return built


def _invalidate(collection: str) -> None:
    """Drop the in-memory matrix so the next search rebuilds it.

    Cheaper than patching the array in place, and correct in the case that
    actually happens: a batch of writes followed by a read, rather than one write
    between two reads.
    """
    _matrix_cache.pop(collection, None)


def index(
    records: Iterable[dict[str, Any]],
    *,
    collection: str = COLLECTION_MEMORY,
) -> dict[str, Any]:
    """Embed and store records, skipping any whose text has not changed.

    Each record is `{"id": str, "text": str, "metadata": dict}`. The id is the
    caller's -- for memories it is the SQLAlchemy primary key -- so re-indexing is
    an upsert and never accumulates duplicates of the same memory.

    The fingerprint check is what makes this callable on every startup. Embedding
    five hundred memories is seconds of CPU; comparing five hundred hashes is
    microseconds, and the normal case is that nothing changed.
    """
    pending: list[tuple[str, str, str, str]] = []
    skipped = 0
    seen: set[str] = set()

    try:
        with _db() as connection:
            known = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT doc_id, fingerprint FROM vectors WHERE collection = ?",
                    (collection,),
                )
            }
    except Exception as exc:
        _log.warning("Vector store unreadable for indexing: %s", exc)
        known = {}

    for record in records:
        doc_id = str(record.get("id") or "").strip()
        text = str(record.get("text") or "").strip()
        if not doc_id or not text or doc_id in seen:
            continue
        seen.add(doc_id)
        fingerprint = _fingerprint(text)
        if known.get(doc_id) == fingerprint:
            skipped += 1
            continue
        metadata = record.get("metadata") or {}
        try:
            encoded_metadata = json.dumps(metadata, ensure_ascii=False, default=str)
        except Exception:
            encoded_metadata = "{}"
        pending.append((doc_id, text, fingerprint, encoded_metadata))

    if not pending:
        return {"indexed": 0, "skipped": skipped, "total": len(known)}

    vectors = embed([item[1] for item in pending])
    if vectors is None:
        return {
            "indexed": 0,
            "skipped": skipped,
            "total": len(known),
            "error": _unavailable_reason or "embedding unavailable",
        }

    rows = [
        (collection, doc_id, text, fingerprint, metadata, vectors[row].tobytes())
        for row, (doc_id, text, fingerprint, metadata) in enumerate(pending)
    ]
    try:
        with _db() as connection:
            connection.executemany(
                "INSERT INTO vectors (collection, doc_id, text, fingerprint, metadata, vector)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(collection, doc_id) DO UPDATE SET"
                "   text = excluded.text,"
                "   fingerprint = excluded.fingerprint,"
                "   metadata = excluded.metadata,"
                "   vector = excluded.vector",
                rows,
            )
    except Exception as exc:
        _log.warning("Vector write failed: %s", exc)
        return {"indexed": 0, "skipped": skipped, "total": len(known), "error": str(exc)}

    _invalidate(collection)
    return {
        "indexed": len(rows),
        "skipped": skipped,
        "total": len(set(known) | {item[0] for item in pending}),
    }


def search(
    query: str,
    *,
    limit: int = 5,
    collection: str = COLLECTION_MEMORY,
    min_similarity: float | None = None,
) -> list[Hit]:
    """Memories that mean something close to the query, best first.

    Brute force on purpose. A few hundred 384-dimension vectors is one numpy
    matmul -- exact, where HNSW is approximate, and under a millisecond at this
    scale. The cost that matters is the single embed of the query, which is the
    3.7 ms in the module docstring.

    Returns `[]` rather than raising when the model is missing, so a caller can
    always merge this with its lexical result.
    """
    text = (query or "").strip()
    if not text:
        return []
    matrix, ids = _matrix(collection)
    if matrix.shape[0] == 0:
        return []
    vectors = embed([text])
    if vectors is None:
        return []

    floor = MIN_SIMILARITY if min_similarity is None else float(min_similarity)
    # Both sides are already L2-normalised, so the dot product *is* the cosine --
    # no per-row division here.
    scores = matrix @ vectors[0]
    count = min(max(1, int(limit)), scores.shape[0])
    # argpartition finds the top `count` without sorting the other five hundred.
    top = np.argpartition(-scores, count - 1)[:count]
    top = top[np.argsort(-scores[top])]

    wanted = [(ids[position], float(scores[position])) for position in top if scores[position] >= floor]
    if not wanted:
        return []

    placeholders = ",".join("?" for _ in wanted)
    try:
        with _db() as connection:
            rows = connection.execute(
                f"SELECT doc_id, text, metadata FROM vectors WHERE collection = ? AND doc_id IN ({placeholders})",
                (collection, *[doc_id for doc_id, _ in wanted]),
            ).fetchall()
    except Exception as exc:
        _log.warning("Vector text lookup failed: %s", exc)
        return []

    bodies = {str(row[0]): (str(row[1]), row[2]) for row in rows}
    hits: list[Hit] = []
    for doc_id, similarity in wanted:
        body = bodies.get(doc_id)
        if body is None:
            continue
        try:
            metadata = json.loads(body[1] or "{}")
        except Exception:
            metadata = {}
        hits.append(
            Hit(
                id=doc_id,
                text=body[0],
                similarity=similarity,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    return hits


def forget(
    ids: Iterable[str],
    *,
    collection: str = COLLECTION_MEMORY,
) -> int:
    """Remove vectors for ids the caller has deleted upstream.

    Needed because the vector store is a second copy. A memory deleted from
    SQLAlchemy and left here would keep answering questions after the user asked
    Akansha to forget it, which is worse than not having semantic recall at all.
    """
    targets = [str(item).strip() for item in ids if str(item or "").strip()]
    if not targets:
        return 0
    placeholders = ",".join("?" for _ in targets)
    try:
        with _db() as connection:
            cursor = connection.execute(
                f"DELETE FROM vectors WHERE collection = ? AND doc_id IN ({placeholders})",
                (collection, *targets),
            )
            removed = int(cursor.rowcount or 0)
    except Exception as exc:
        _log.warning("Vector delete failed: %s", exc)
        return 0
    if removed:
        _invalidate(collection)
    return removed


def stats(*, collection: str = COLLECTION_MEMORY) -> dict[str, Any]:
    """What is actually in the index, for the capability endpoint to report."""
    try:
        with _db() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vectors WHERE collection = ?", (collection,)
                ).fetchone()[0]
            )
    except Exception as exc:
        return {"collection": collection, "count": 0, "error": str(exc)}
    matrix, _ = _matrix(collection)
    return {
        "collection": collection,
        "count": count,
        "cached": int(matrix.shape[0]),
        "dimensions": EMBED_DIMENSIONS,
        "min_similarity": MIN_SIMILARITY,
        "store": os.path.join(STORE_DIR, "semantic.sqlite3"),
    }


def warm() -> Capability:
    """Build the session now, downloading the model if it is not cached.

    Called from the startup thread for the same reason the voice models are: the
    ~700 ms session build and the 90 MB first-run download should not be paid by
    whichever request happens to be first.
    """
    if _encoder() is None:
        return Capability(
            installed=True,
            ready=False,
            detail=_unavailable_reason or "Embedding model unavailable.",
        )
    return Capability(installed=True, ready=True, detail="Semantic recall ready.")


def reset_for_tests() -> None:
    """Forget every cached singleton. Tests only."""
    global _session, _tokenizer, _unavailable_reason
    with _lock:
        _session = None
        _tokenizer = None
        _unavailable_reason = None
        _matrix_cache.clear()
