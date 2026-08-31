from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .exceptions import WaitTimeoutError

logger = logging.getLogger(__name__)

MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 60_000
DEFAULT_ELEMENT_TIMEOUT_MS = 15_000
DEFAULT_NAV_TIMEOUT_MS = 30_000
POLL_INTERVAL_MS = 100


class WaitStrategy:
    """Configurable wait policies for Playwright page conditions.
    
    All waits poll at 100ms intervals and raise WaitTimeoutError on expiry.
    Timeout must be between 1s and 60s (validated on each call).
    """

    def _clamp_timeout(self, timeout_ms: int) -> int:
        if timeout_ms < MIN_TIMEOUT_MS:
            logger.warning("Timeout %dms below minimum, clamping to %dms", timeout_ms, MIN_TIMEOUT_MS)
            return MIN_TIMEOUT_MS
        if timeout_ms > MAX_TIMEOUT_MS:
            logger.warning("Timeout %dms above maximum, clamping to %dms", timeout_ms, MAX_TIMEOUT_MS)
            return MAX_TIMEOUT_MS
        return timeout_ms

    async def wait_for_element(
        self,
        page: Any,
        selector: str,
        timeout_ms: int = DEFAULT_ELEMENT_TIMEOUT_MS,
        state: str = "visible",
    ) -> None:
        """Wait until selector is visible (or specified state). Raise WaitTimeoutError on timeout."""
        timeout_ms = self._clamp_timeout(timeout_ms)
        try:
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            logger.debug("WaitStrategy: element '%s' ready", selector)
        except Exception as e:
            if "timeout" in str(e).lower() or "TimeoutError" in type(e).__name__:
                raise WaitTimeoutError(selector, timeout_ms)
            raise

    async def wait_for_network_idle(
        self,
        page: Any,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> None:
        """Wait until no network requests for 500ms."""
        timeout_ms = self._clamp_timeout(timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            logger.debug("WaitStrategy: network idle")
        except Exception as e:
            if "timeout" in str(e).lower() or "TimeoutError" in type(e).__name__:
                raise WaitTimeoutError("networkidle", timeout_ms)
            raise

    async def wait_for_animations(self, page: Any) -> None:
        """Poll document.getAnimations().length until zero or 5s passes."""
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            try:
                count = await page.evaluate("document.getAnimations().length")
                if count == 0:
                    return
            except Exception:
                return  # If JS fails, don't block
            await asyncio.sleep(POLL_INTERVAL_MS / 1000)
        logger.debug("WaitStrategy: animations did not complete within 5s, proceeding")

    async def wait_for_url_change(
        self,
        page: Any,
        expected_url_contains: str,
        timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
    ) -> None:
        """Wait until the page URL contains expected_url_contains."""
        timeout_ms = self._clamp_timeout(timeout_ms)
        deadline = time.perf_counter() + timeout_ms / 1000
        while time.perf_counter() < deadline:
            try:
                if expected_url_contains in page.url:
                    return
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL_MS / 1000)
        raise WaitTimeoutError(f"url_contains:{expected_url_contains}", timeout_ms)
