from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COOKIE_ACCEPT_PATTERNS = ["accept", "agree", "allow all", "i accept", "accept all", "accept cookies", "got it", "ok"]
COOKIE_BUTTON_SELECTORS = [
    "button[id*='accept' i]", "button[id*='agree' i]",
    "button[aria-label*='accept' i]", "button[aria-label*='agree' i]",
    "#accept-button", "#agree-btn", ".cookie-accept",
    "[data-testid*='accept' i]",
]


class GenericSiteSkill:
    """Generic automation skill for any website.
    
    Handles: cookie banners, login walls, modal popups, ref pre-validation.
    """

    async def handle_cookie_banner(self, page: Any) -> bool:
        """Auto-dismiss cookie consent banners. Returns True if banner was found and dismissed."""
        # Try known selectors first
        for sel in COOKIE_BUTTON_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("GenericSiteSkill: dismissed cookie banner via selector %s", sel)
                    await asyncio.sleep(0.5)
                    return True
            except Exception:
                continue

        # Fallback: scan all buttons for acceptance text patterns
        try:
            buttons = await page.query_selector_all("button, a[role='button'], [type='submit']")
            for btn in buttons:
                try:
                    text = (await btn.inner_text()).strip().lower()
                    if any(pattern in text for pattern in COOKIE_ACCEPT_PATTERNS):
                        if await btn.is_visible():
                            await btn.click()
                            logger.info("GenericSiteSkill: dismissed cookie banner by text '%s'", text)
                            await asyncio.sleep(0.5)
                            return True
                except Exception:
                    continue
        except Exception as e:
            logger.debug("GenericSiteSkill: cookie banner scan failed: %s", e)
        return False

    async def detect_login_wall(self, page: Any) -> bool:
        """Return True if page shows a login wall with no other interactive content."""
        try:
            content = await page.content()
            has_password_field = "password" in content.lower()
            has_sign_in = "sign in" in content.lower() or "log in" in content.lower()
            # Check for substantial other content (not just a login form)
            inputs = await page.query_selector_all("input:not([type='hidden'])")
            only_login_inputs = all(
                await inp.get_attribute("type") in ("email", "password", "text", None)
                for inp in inputs
            ) if inputs else False
            return has_password_field and has_sign_in and only_login_inputs
        except Exception:
            return False

    async def handle_modal_popup(self, page: Any) -> bool:
        """Try to close a blocking modal popup. Returns True if closed."""
        # Try pressing Escape first
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Try clicking close buttons
        close_selectors = [
            "button[aria-label*='close' i]", "button[aria-label*='dismiss' i]",
            "[data-testid='close']", ".modal-close", ".close-button",
            "button.close", "[aria-label='Close']",
        ]
        for sel in close_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("GenericSiteSkill: closed modal via selector %s", sel)
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        return False

    def validate_plan_refs(self, plan: List[Dict], registry_keys: List[str]) -> List[str]:
        """Return list of refs in plan that are NOT in the current registry. Empty = all valid."""
        missing = []
        for action in plan:
            ref = action.get("compact_ref") or action.get("element_ref")
            if ref and ref not in registry_keys:
                missing.append(ref)
        return missing

    async def prepare_page(self, page: Any) -> Dict[str, bool]:
        """Run all page preparation steps before main automation flow."""
        cookie_dismissed = await self.handle_cookie_banner(page)
        login_wall = await self.detect_login_wall(page)
        modal_closed = await self.handle_modal_popup(page) if not login_wall else False
        return {
            "cookie_dismissed": cookie_dismissed,
            "login_wall_detected": login_wall,
            "modal_closed": modal_closed,
        }
