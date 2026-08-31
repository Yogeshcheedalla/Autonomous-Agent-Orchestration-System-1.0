from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

#: The one launch failure a person can actually do something about, in the words
#: they need. Kept next to `app_control`'s wording deliberately: both paths open
#: the same profile, so both have to fail the same way.
PROFILE_IN_USE_DETAIL = (
    "Akansha's browser window is already open -- close the sign-in window, then try again. "
    "Chrome allows one window per profile."
)


@dataclass
class NavigationResult:
    success: bool
    tab_id: str
    url: str
    status_code: int = 200
    extracted_text: str = ""
    dom_snapshot: Dict[str, Any] = field(default_factory=dict)
    screenshot_path: Optional[str] = None
    error: Optional[str] = None


class PlaywrightDriver:
    """Real Chromium browser driver wrapping Microsoft Playwright.

    Headed mode (headless=False) opens a visible browser window.
    All navigation waits for networkidle so JS-heavy pages (YouTube) fully render.
    """

    def __init__(
        self, headless: bool = False, timeout_ms: int = 30_000, persistent: bool = True
    ) -> None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise ImportError(
                "Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        self.headless = headless
        self.timeout_ms = timeout_ms
        #: Whether to open Akansha's own profile. On by default because a browser
        #: that forgets every sign-in is not the browser `/connections` promises.
        #: Tests and CI pass False to get a throwaway profile that cannot collide
        #: with a real one, and nothing else should.
        self.persistent = persistent
        self._playwright = None
        self._browser = None
        self._page = None
        self._pages: Dict[str, Any] = {}  # url -> page
        self._active_url: str = ""
        #: Set by `owned_session.session()` to the loop this driver was launched
        #: on, so a driver stranded by a closed loop can be recognised as dead.
        self._owned_loop: Any = None

    async def launch(self) -> None:
        """Open Akansha's browser window.

        A *persistent context* rather than `launch()` + `new_context()`, on the
        same directory the connections sign-in flow writes to. That single change
        is what makes "sign in once, in a window you can see" true: the cookie the
        person created by hand is in scope for every command afterwards.

        No `user_agent` override any more either. Spoofing a UA string onto a real
        Chrome build is how a profile ends up reported as inconsistent by the
        sites most worth being signed into; the real browser's own string is
        correct by construction.
        """
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        if not self.persistent:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page = await context.new_page()
            logger.info("PlaywrightDriver launched (throwaway profile, headless=%s)", self.headless)
            return

        from .owned_session import browser_executable, profile_dir

        directory = profile_dir()
        directory.mkdir(parents=True, exist_ok=True)
        executable = browser_executable()
        # `launch_persistent_context` returns the context, and the context *is* the
        # browser handle in persistent mode -- there is no separate Browser object.
        # It answers `is_connected()` and `close()`, which is all the rest of this
        # class and the liveness check in `owned_session` ask of it.
        try:
            self._browser = await self._playwright.chromium.launch_persistent_context(
                str(directory),
                executable_path=executable or None,
                headless=self.headless,
                viewport={"width": 1280, "height": 800},
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    # Kept from the old throwaway launch, and it matters more here:
                    # a profile that is signed into real accounts is exactly the one
                    # a site will challenge for looking automated.
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        except Exception as exc:
            # Chrome allows one process per user-data-dir. When the connections
            # sign-in window is still open on this profile, the raw Playwright error
            # names a lock file, which tells the person nothing they can act on.
            message = str(exc)
            await self._playwright.stop()
            self._playwright = None
            if "ProcessSingleton" in message or "user data directory is already in use" in message:
                raise RuntimeError(PROFILE_IN_USE_DETAIL) from exc
            raise
        pages = self._browser.pages
        self._page = pages[0] if pages else await self._browser.new_page()
        logger.info(
            "PlaywrightDriver launched (profile=%s, browser=%s, headless=%s)",
            directory,
            Path(executable).name if executable else "bundled chromium",
            self.headless,
        )

    async def navigate(self, url: str) -> NavigationResult:
        """Navigate to URL and wait for networkidle.

        Reuses the existing page if the URL already matches the active tab (Req 1.3).
        Returns NavigationResult(success=False) on 30s timeout (Req 1.5).
        """
        if self._page is None:
            await self.launch()

        # Req 1.3: reuse existing page if URL matches already-open tab
        if self._active_url == url:
            logger.info("Reusing existing page for %s", url)
            return NavigationResult(
                success=True,
                tab_id="tab_1",
                url=url,
                extracted_text=f"Already on {url}",
            )

        try:
            # Req 1.2: wait for networkidle before returning
            response = await self._page.goto(
                url,
                wait_until="networkidle",
                timeout=self.timeout_ms,
            )
            self._active_url = url
            status = response.status if response else 200
            title = await self._page.title()
            logger.info("Navigated to %s (status=%s, title=%s)", url, status, title)
            return NavigationResult(
                success=True,
                tab_id="tab_1",
                url=url,
                status_code=status,
                extracted_text=f"Loaded: {title}",
            )
        except Exception as e:
            error_msg = str(e)
            logger.error("Navigation failed for %s: %s", url, error_msg)
            # Req 1.5: return NavigationResult(success=False) on timeout or any failure
            return NavigationResult(
                success=False,
                tab_id="tab_1",
                url=url,
                error=error_msg,
            )

    async def close(self) -> None:
        """Close all pages and release Chromium (Req 1.6)."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            self._page = None
            self._browser = None
            self._playwright = None
            self._active_url = ""
            self._owned_loop = None
            logger.info("PlaywrightDriver closed")
        except Exception as e:
            logger.warning("Error closing PlaywrightDriver: %s", e)

    @property
    def current_url(self) -> str:
        """Return URL of the currently active page (Req 1.7)."""
        if self._page:
            try:
                return self._page.url
            except Exception:
                pass
        return self._active_url

    async def get_page(self) -> Any:
        """Return the underlying Playwright Page object."""
        if self._page is None:
            await self.launch()
        return self._page

    async def evaluate(self, script: str) -> Any:
        """Execute JavaScript in the current page context."""
        if self._page is None:
            return None
        try:
            return await self._page.evaluate(script)
        except Exception as e:
            logger.warning("evaluate() failed: %s", e)
            return None

    async def screenshot(self, path: Optional[str] = None) -> bytes:
        """Capture viewport screenshot as PNG bytes."""
        if self._page is None:
            return b""
        try:
            if path:
                return await self._page.screenshot(path=path, full_page=False)
            return await self._page.screenshot(full_page=False)
        except Exception as e:
            logger.warning("screenshot() failed: %s", e)
            return b""
