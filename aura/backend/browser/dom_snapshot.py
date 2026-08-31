from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

INTERACTIVE_SELECTOR = (
    "input, button, select, textarea, a[href], "
    "[role='button'], [role='searchbox'], [role='option'], "
    "[role='menuitem'], [role='tab']"
)


@dataclass
class SnapshotElement:
    ref: str            # e.g. "{e3}"
    element_type: str   # input, button, a, select, etc.
    text: str           # visible text (max 80 chars)
    selector: str       # unique CSS selector
    xpath: str          # XPath expression
    bbox: Dict[str, float]  # {"x":..,"y":..,"width":..,"height":..}
    aria_label: str = ""   # aria-label attribute value or ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "type": self.element_type,
            "text": self.text,
            "selector": self.selector,
            "xpath": self.xpath,
            "bbox": self.bbox,
            "aria_label": self.aria_label,
        }


class LiveDOMSnapshot:
    """Scans the live Playwright page DOM and returns SnapshotElement list.

    Only called after the page has fully loaded (networkidle) so JavaScript
    has finished rendering dynamic content (e.g. YouTube search box).
    """

    async def scan(self, page: Any) -> List[SnapshotElement]:
        """Return list of SnapshotElement in document order.

        Returns empty list (never raises) when no interactive elements found.
        """
        try:
            elements = await page.query_selector_all(INTERACTIVE_SELECTOR)
        except Exception as e:
            logger.warning("DOM scan failed: %s", e)
            return []

        results: List[SnapshotElement] = []
        for i, el in enumerate(elements):
            try:
                ref = "{e" + str(i + 1) + "}"
                tag = (await el.get_attribute("tagName") or "unknown").lower()
                # Get visible text
                try:
                    raw_text = (await el.inner_text()) or ""
                except Exception:
                    raw_text = ""
                text = raw_text.strip()[:80]
                # Get aria-label
                aria_label = (await el.get_attribute("aria-label")) or ""
                # Get bounding box
                try:
                    box = await el.bounding_box()
                    bbox = box if box else {"x": 0, "y": 0, "width": 0, "height": 0}
                except Exception:
                    bbox = {"x": 0, "y": 0, "width": 0, "height": 0}
                # Generate CSS selector
                selector = await self._generate_selector(page, el, i)
                xpath = f"//*[{i+1}]"  # positional fallback XPath

                snap = SnapshotElement(
                    ref=ref,
                    element_type=tag,
                    text=text,
                    selector=selector,
                    xpath=xpath,
                    bbox=bbox,
                    aria_label=aria_label,
                )
                results.append(snap)
            except Exception as e:
                logger.debug("Skipping element %d during snapshot: %s", i, e)
                continue

        if not results:
            logger.warning("LiveDOMSnapshot: zero interactive elements found on page")
        else:
            logger.info("LiveDOMSnapshot: captured %d elements", len(results))
        return results

    async def _generate_selector(self, page: Any, el: Any, index: int) -> str:
        """Generate a unique CSS selector for this element."""
        try:
            # Try to get a unique selector via JS
            selector = await page.evaluate("""(el) => {
                if (el.id) return '#' + CSS.escape(el.id);
                if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
                // Build path
                let path = [];
                let node = el;
                while (node && node.nodeType === 1 && node.tagName !== 'BODY') {
                    let tag = node.tagName.toLowerCase();
                    let siblings = Array.from(node.parentNode?.children || []).filter(c => c.tagName === node.tagName);
                    if (siblings.length > 1) {
                        let idx = siblings.indexOf(node) + 1;
                        path.unshift(tag + ':nth-of-type(' + idx + ')');
                    } else {
                        path.unshift(tag);
                    }
                    node = node.parentNode;
                }
                return path.join(' > ');
            }""", el)
            return selector or f"*:nth-child({index+1})"
        except Exception:
            return f"*:nth-child({index+1})"
