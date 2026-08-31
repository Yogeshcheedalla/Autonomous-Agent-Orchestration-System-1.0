"""
Browser automation package — Playwright-based real browser driver and skills.
"""
from .playwright_driver import PlaywrightDriver, NavigationResult
from .dom_snapshot import LiveDOMSnapshot, SnapshotElement
from .compact_ref_registry import CompactRefRegistry
from .exceptions import WaitTimeoutError, BrowserAutomationError
from .owned_session import LazyOwnedDriver, close_session, run_owned, session

__all__ = [
    "PlaywrightDriver",
    "NavigationResult",
    "LiveDOMSnapshot",
    "SnapshotElement",
    "CompactRefRegistry",
    "WaitTimeoutError",
    "BrowserAutomationError",
    "LazyOwnedDriver",
    "close_session",
    "run_owned",
    "session",
]
