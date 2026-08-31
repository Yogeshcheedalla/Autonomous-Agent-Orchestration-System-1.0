from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScreenshotResult:
    success: bool
    file_path: Optional[str] = None
    data_uri: Optional[str] = None   # base64 PNG for frontend display
    error: Optional[str] = None


class ScreenshotService:
    """Captures screenshots after key actions and stores them as PNG files.
    
    Always returns a result dict — never raises exceptions.
    Failure is gracefully logged and returned as ScreenshotResult(success=False).
    """

    def __init__(self, output_dir: str = "screenshots") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def capture(self, page: Any, label: str) -> ScreenshotResult:
        """Save PNG to output_dir/{timestamp}_{label}.png. Return path and base64 URI."""
        try:
            timestamp = int(time.time())
            # Sanitize label for filename
            safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:50]
            filename = f"{timestamp}_{safe_label}.png"
            file_path = self.output_dir / filename
            
            png_bytes = await page.screenshot(path=str(file_path), full_page=False)
            
            # Also encode as base64 for inline display in chat UI
            if not png_bytes:
                png_bytes = file_path.read_bytes()
            b64 = base64.b64encode(png_bytes).decode("utf-8")
            data_uri = f"data:image/png;base64,{b64}"
            
            logger.info("ScreenshotService: captured %s (%d bytes)", filename, len(png_bytes))
            return ScreenshotResult(
                success=True,
                file_path=str(file_path),
                data_uri=data_uri,
            )
        except Exception as e:
            logger.warning("ScreenshotService: capture failed for label='%s': %s", label, e)
            return ScreenshotResult(success=False, error=str(e))

    async def verify_element_visible(self, page: Any, selector: str) -> bool:
        """Return True iff element exists and is within the viewport."""
        try:
            el = await page.query_selector(selector)
            if not el:
                return False
            is_visible = await el.is_visible()
            if not is_visible:
                return False
            # Check it's in viewport
            bbox = await el.bounding_box()
            if not bbox:
                return False
            viewport = await page.evaluate("({width: window.innerWidth, height: window.innerHeight})")
            in_viewport = (
                bbox["x"] >= 0 and
                bbox["y"] >= 0 and
                bbox["x"] + bbox["width"] <= viewport["width"] + 50 and  # small tolerance
                bbox["y"] + bbox["height"] <= viewport["height"] + 50
            )
            return in_viewport
        except Exception as e:
            logger.debug("verify_element_visible failed for '%s': %s", selector, e)
            return False

    async def verify_text_visible(self, page: Any, text: str) -> bool:
        """Return True iff text appears anywhere on the visible page."""
        try:
            content = await page.content()
            return text.lower() in content.lower()
        except Exception as e:
            logger.debug("verify_text_visible failed: %s", e)
            return False
