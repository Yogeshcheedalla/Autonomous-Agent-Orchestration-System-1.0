"""
Custom exceptions for the browser automation module.
"""


class WaitTimeoutError(Exception):
    """Raised when a wait strategy times out before the condition is met.

    Attributes:
        selector: The CSS selector or condition description that was waited on.
        timeout_ms: The configured timeout in milliseconds.
    """

    def __init__(self, selector: str, timeout_ms: int) -> None:
        self.selector = selector
        self.timeout_ms = timeout_ms
        super().__init__(
            f"Timed out waiting for '{selector}' after {timeout_ms}ms"
        )


class BrowserAutomationError(Exception):
    """General-purpose error for browser automation failures.

    Raised when a browser action fails and does not fit a more specific
    exception category (e.g., element not found, navigation error).
    """
    pass
