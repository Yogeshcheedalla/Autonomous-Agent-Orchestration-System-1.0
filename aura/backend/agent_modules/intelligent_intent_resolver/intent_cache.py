"""IntentCache — in-memory TTL cache for intent resolutions."""
from __future__ import annotations
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

DEFAULT_TTL_SECONDS = 300  # 5 minutes
MAX_CACHE_SIZE = 1000


class IntentCache:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._cache: OrderedDict = OrderedDict()  # key -> (value, stored_at, context_hash)

    def _context_hash(self, context: Dict[str, Any]) -> str:
        workflow = str(context.get("active_workflow", ""))
        return workflow[:50]

    def has(self, key: str) -> bool:
        if key not in self._cache:
            return False
        _, stored_at, _ = self._cache[key]
        if time.time() - stored_at > self.ttl:
            del self._cache[key]
            return False
        return True

    def get(self, key: str):
        if not self.has(key):
            return None
        value, _, _ = self._cache[key]
        self._cache.move_to_end(key)
        return value

    def set(self, key: str, value, context: Optional[Dict[str, Any]] = None) -> None:
        if len(self._cache) >= MAX_CACHE_SIZE:
            self._cache.popitem(last=False)  # evict oldest (LRU)
        ctx_hash = self._context_hash(context or {})
        self._cache[key] = (value, time.time(), ctx_hash)
        self._cache.move_to_end(key)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def should_use_cache(self, key: str, current_context: Dict[str, Any]) -> bool:
        """Return True only if cache entry is fresh, context matches, and confidence is sufficient."""
        if key not in self._cache:
            return False
        value, stored_at, cached_ctx_hash = self._cache[key]
        # Staleness check
        if time.time() - stored_at > self.ttl:
            return False
        # Active workflow change invalidates
        if self._context_hash(current_context) != cached_ctx_hash:
            return False
        # Low confidence results should not be reused
        cached_confidence = getattr(value, "confidence", 1.0)
        if cached_confidence < 0.8:
            return False
        return True
