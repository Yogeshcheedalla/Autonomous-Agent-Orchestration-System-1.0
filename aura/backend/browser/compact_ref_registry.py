from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .dom_snapshot import SnapshotElement

logger = logging.getLogger(__name__)


@dataclass
class RefEntry:
    """Stored mapping for a single compact ref."""
    ref: str
    selector: str
    xpath: str
    element_type: str
    text: str
    aria_label: str
    snapshot_time: float = field(default_factory=time.time)
    stale: bool = False


class CompactRefRegistry:
    """Maps {eN} compact refs to live SnapshotElement data.
    
    Automatically invalidated on navigation or SPA pushState change.
    Maintains sliding window of last 5 snapshots for debugging.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, RefEntry] = {}
        self._stale: bool = False
        self._history: Deque[List[SnapshotElement]] = deque(maxlen=5)
        self._current_url: str = ""

    def populate(self, elements: List[SnapshotElement]) -> None:
        """Assign {e1}..{eN} and store. Clears previous refs first."""
        self._entries.clear()
        self._stale = False
        for el in elements:
            self._entries[el.ref] = RefEntry(
                ref=el.ref,
                selector=el.selector,
                xpath=el.xpath,
                element_type=el.element_type,
                text=el.text,
                aria_label=el.aria_label,
            )
        self._history.append(list(elements))
        logger.info("CompactRefRegistry: populated %d refs", len(elements))

    def resolve(self, ref: str) -> Optional[str]:
        """Return live CSS selector for ref, or None if stale/missing."""
        if self._stale:
            logger.warning("CompactRefRegistry: all refs are stale (navigation occurred)")
            return None
        entry = self._entries.get(ref)
        if entry is None:
            logger.warning("CompactRefRegistry: unknown ref %s", ref)
            return None
        if entry.stale:
            logger.warning("CompactRefRegistry: ref %s is stale", ref)
            return None
        return entry.selector

    def invalidate(self) -> None:
        """Mark all refs stale. Called on every navigation or URL change."""
        self._stale = True
        for entry in self._entries.values():
            entry.stale = True
        logger.info("CompactRefRegistry: all refs invalidated")

    async def verify_live(self, ref: str, page: Any) -> bool:
        """Re-check element still in DOM using stored selector."""
        selector = self.resolve(ref)
        if not selector:
            return False
        try:
            el = await page.query_selector(selector)
            return el is not None
        except Exception as e:
            logger.warning("verify_live failed for %s: %s", ref, e)
            return False

    def resolve_by_text(self, element_type: str, text: str) -> Optional[str]:
        """Fallback: find selector by matching element type and visible text."""
        for entry in self._entries.values():
            if (entry.element_type == element_type and 
                    text.lower() in entry.text.lower()):
                return entry.selector
        return None

    def resolve_by_aria(self, aria_contains: str) -> Optional[str]:
        """Find selector by aria-label containing a substring."""
        for entry in self._entries.values():
            if aria_contains.lower() in entry.aria_label.lower():
                return entry.selector
        return None

    @property
    def snapshot_history(self) -> List[List[SnapshotElement]]:
        """Returns last 5 snapshot lists (sliding window)."""
        return list(self._history)

    @property
    def is_stale(self) -> bool:
        return self._stale

    def __len__(self) -> int:
        return len(self._entries)
