from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_SCROLL_PIXELS = 600


@dataclass
class ScrollResult:
    success: bool
    scroll_y: float         # new window.scrollY after scroll
    action: str             # "scrollBy" or "scrollIntoView"
    pixels: Optional[int] = None
    error: Optional[str] = None


class BrowserScrollAction:
    """Browser-internal scroll using page.evaluate(). Never calls pyautogui.
    
    All scrolling happens inside the Playwright browser page, not on the
    desktop OS level. This correctly scrolls YouTube results pages,
    infinite-scroll feeds, etc.
    """

    async def scroll(
        self,
        page: Any,
        direction: str = "down",
        pixels: int = DEFAULT_SCROLL_PIXELS,
    ) -> ScrollResult:
        """Execute window.scrollBy(0, ±pixels). Return new window.scrollY.
        
        direction='down' → positive delta (scroll down)
        direction='up'   → negative delta (scroll up)
        """
        delta = pixels if direction.lower() == "down" else -pixels
        try:
            await page.evaluate(f"window.scrollBy(0, {delta})")
            scroll_y = await page.evaluate("window.scrollY")
            logger.info("BrowserScrollAction: scrolled %s %dpx, scrollY=%.0f", direction, pixels, scroll_y)
            return ScrollResult(success=True, scroll_y=float(scroll_y), action="scrollBy", pixels=pixels)
        except Exception as e:
            logger.error("BrowserScrollAction.scroll failed: %s", e)
            return ScrollResult(success=False, scroll_y=0.0, action="scrollBy", pixels=pixels, error=str(e))

    async def scroll_to_element(self, page: Any, selector: str) -> ScrollResult:
        """Call element.scrollIntoView(). Return new window.scrollY."""
        try:
            await page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) el.scrollIntoView({behavior: 'smooth', block: 'center'});
                }""",
                selector
            )
            # Wait briefly for smooth scroll to settle
            import asyncio
            await asyncio.sleep(0.5)
            scroll_y = await page.evaluate("window.scrollY")
            logger.info("BrowserScrollAction: scrolled to element '%s', scrollY=%.0f", selector, scroll_y)
            return ScrollResult(success=True, scroll_y=float(scroll_y), action="scrollIntoView")
        except Exception as e:
            logger.error("BrowserScrollAction.scroll_to_element failed for '%s': %s", selector, e)
            return ScrollResult(success=False, scroll_y=0.0, action="scrollIntoView", error=str(e))

    async def get_scroll_position(self, page: Any) -> float:
        """Return current window.scrollY."""
        try:
            return float(await page.evaluate("window.scrollY") or 0)
        except Exception:
            return 0.0
