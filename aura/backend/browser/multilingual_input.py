from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUPPORTED_SCRIPTS = ["Telugu", "Hindi", "English", "Arabic", "Tamil", "Kannada", "Malayalam"]


@dataclass
class FillResult:
    success: bool
    selector: str
    text_intended: str
    text_verified: str = ""
    used_fallback: bool = False
    error: Optional[str] = None


class MultilingualInputHandler:
    """Types Unicode text into browser inputs via Playwright page.fill().

    Supports all Unicode scripts including Telugu (బొమ్మరిల్లు), Hindi (देवनागरी),
    Arabic, Tamil, Kannada, Malayalam without IME conversion or character substitution.

    Req 8.1: page.fill() supports full Unicode without IME issues.
    Req 8.2: All Unicode codepoints preserved exactly, no transliteration.
    Req 8.3: Supports Telugu, Hindi, English, Arabic, Tamil, Kannada, Malayalam.
    Req 8.4: After fill, verifies element.value matches typed text via page.evaluate().
    Req 8.5: On mismatch, retries once with page.evaluate("el => el.value = text").
    """

    async def fill(self, page: Any, selector: str, text: str) -> FillResult:
        """Fill an input field with Unicode text and verify the result.

        Steps:
        1. Wait for the element to be visible (up to 5 s).
        2. Clear field and type text via page.fill() — full Unicode, no IME.
        3. Read back element.value via page.evaluate().
        4. On mismatch, retry once via evaluate-based fallback (Req 8.5).
        """
        try:
            # Wait for element to be ready (Req 3.5 / 8.1)
            await page.wait_for_selector(selector, state="visible", timeout=5000)

            # page.fill() handles full Unicode natively — no IME conversion (Req 8.1, 8.2)
            await page.fill(selector, text)

            # Verify the fill worked (Req 8.4)
            actual: str = await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.value : \"\"; }",
                selector,
            )
            if actual == text:
                logger.info(
                    "MultilingualInputHandler: filled '%s' into %s",
                    text[:30],
                    selector,
                )
                return FillResult(
                    success=True,
                    selector=selector,
                    text_intended=text,
                    text_verified=actual,
                )

            # Mismatch — retry with evaluate fallback (Req 8.5)
            logger.warning(
                "Fill mismatch for %s, using evaluate fallback. Got: '%s'",
                selector,
                actual[:30],
            )
            await page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.sel);
                    if (el) {
                        el.value = args.val;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""",
                {"sel": selector, "val": text},
            )
            verified: str = await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.value : \"\"; }",
                selector,
            )
            success = verified == text
            if not success:
                logger.error("Fallback fill also failed for %s", selector)
            return FillResult(
                success=success,
                selector=selector,
                text_intended=text,
                text_verified=verified,
                used_fallback=True,
                error=None if success else f"Value mismatch after fallback: got '{verified[:30]}'",
            )
        except Exception as exc:
            logger.error(
                "MultilingualInputHandler.fill failed for %s: %s", selector, exc
            )
            return FillResult(
                success=False,
                selector=selector,
                text_intended=text,
                error=str(exc),
            )

    async def verify_fill(self, page: Any, selector: str, expected: str) -> bool:
        """Return True if element.value == expected (Req 8.4).

        Args:
            page: Playwright Page object.
            selector: CSS selector of the input element.
            expected: The string value to compare against.

        Returns:
            True if the live element value matches *expected*, False otherwise.
        """
        try:
            actual: Optional[str] = await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.value : null; }",
                selector,
            )
            return actual == expected
        except Exception as exc:
            logger.warning("verify_fill failed for %s: %s", selector, exc)
            return False
