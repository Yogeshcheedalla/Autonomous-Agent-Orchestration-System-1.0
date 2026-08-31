from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAY_S = 3.0


class ErrorClass(str, Enum):
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    STALE_ELEMENT = "STALE_ELEMENT"
    PAGE_CRASH = "PAGE_CRASH"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class FailureReport:
    action_name: str
    error_class: ErrorClass
    retries_attempted: int
    error_message: str
    compact_ref: Optional[str] = None
    last_screenshot_path: Optional[str] = None


class BrowserErrorRecovery:
    """Wraps browser actions with retry logic and structured error classification."""

    def __init__(
        self,
        snapshot_fn: Optional[Callable] = None,
        screenshot_fn: Optional[Callable] = None,
    ) -> None:
        self._snapshot_fn = snapshot_fn
        self._screenshot_fn = screenshot_fn
        self._last_known_url: str = ""

    def classify(self, exception: Exception) -> ErrorClass:
        msg = str(exception).lower()
        exc_type = type(exception).__name__
        if "timeout" in msg or "TimeoutError" in exc_type:
            return ErrorClass.NAVIGATION_TIMEOUT
        if "not attached" in msg or "detached" in msg or "stale" in msg:
            return ErrorClass.STALE_ELEMENT
        if "not found" in msg or "no element" in msg or "element is not visible" in msg or "selector" in msg:
            return ErrorClass.ELEMENT_NOT_FOUND
        if "crash" in msg or "target closed" in msg:
            return ErrorClass.PAGE_CRASH
        if "net::" in msg or "connection" in msg or "network" in msg:
            return ErrorClass.NETWORK_ERROR
        return ErrorClass.UNKNOWN

    async def handle(self, action_fn: Callable, *args, action_name: str = "action", compact_ref: Optional[str] = None, **kwargs) -> Any:
        """Wrap action_fn with retry logic. Returns FailureReport after all retries."""
        last_error = None
        last_error_class = ErrorClass.UNKNOWN

        for attempt in range(MAX_RETRIES + 1):
            try:
                return await action_fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                last_error_class = self.classify(exc)
                logger.warning("BrowserErrorRecovery: attempt %d/%d failed for '%s': %s [%s]",
                               attempt + 1, MAX_RETRIES + 1, action_name, exc, last_error_class)

                if last_error_class == ErrorClass.PAGE_CRASH:
                    await self._recover_crashed_page()
                    break  # no retry after crash

                if last_error_class in (ErrorClass.ELEMENT_NOT_FOUND, ErrorClass.STALE_ELEMENT):
                    if self._snapshot_fn:
                        logger.info("BrowserErrorRecovery: re-snapshotting page after %s", last_error_class)
                        await self._snapshot_fn()

                if attempt < MAX_RETRIES:
                    logger.info("BrowserErrorRecovery: retrying in %.1fs...", RETRY_DELAY_S)
                    await asyncio.sleep(RETRY_DELAY_S)

        # All retries exhausted — capture screenshot and return FailureReport
        screenshot_path = None
        if self._screenshot_fn:
            try:
                screenshot_path = await self._screenshot_fn(f"failure_{action_name}")
            except Exception:
                pass

        return FailureReport(
            action_name=action_name,
            error_class=last_error_class,
            retries_attempted=MAX_RETRIES,
            error_message=str(last_error) if last_error else "Unknown error",
            compact_ref=compact_ref,
            last_screenshot_path=screenshot_path,
        )

    async def _recover_crashed_page(self) -> None:
        logger.error("BrowserErrorRecovery: PAGE_CRASH detected — attempting recovery")
        # Caller handles actual page recovery; we just log here
