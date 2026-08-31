from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..browser_scroll import BrowserScrollAction, ScrollResult
from ..video_playback import VideoPlaybackController, PlaybackResult
from ..multilingual_input import MultilingualInputHandler
from ..wait_strategy import WaitStrategy
from ..screenshot_service import ScreenshotService, ScreenshotResult

logger = logging.getLogger(__name__)

BASE_URL = "https://www.youtube.com"
SEARCH_SELECTOR = "input#search"
SEARCH_SUBMIT = "button#search-icon-legacy"
VIDEO_CARD_SELECTOR = "ytd-video-renderer"
FILTER_BTN_SELECTOR = "button[aria-label='Search filters']"
SEARCH_TIMEOUT_MS = 15_000


@dataclass
class VideoCard:
    """Represents a single video result card on the YouTube results page."""

    index: int
    title: str
    channel: str
    view_count: str
    selector: str


class YouTubeAutomationSkill:
    """Site-specific automation skill for YouTube.

    Implements the complete end-to-end flow:
        navigate → search → (optional filter) → scroll → play → pause

    All DOM interactions use real Playwright page controls via the injected
    driver.  Dependencies (scroll, video, multilingual, wait, screenshot) are
    injected so they can be replaced with mocks during testing.

    Requirements covered:
        Req 4.1  – Navigate to YouTube and wait for search box
        Req 4.2  – Locate input#search and type the query
        Req 4.3  – Press Enter and wait for results
        Req 4.4  – Read video cards (title, channel, view count)
        Req 4.5  – Apply language filter (e.g. Telugu)
        Req 4.6  – Scroll results via BrowserScrollAction
        Req 4.7  – Click video thumbnail and wait for player
        Req 4.8  – Verify video player element is present
        Req 4.9  – Set playback language via player settings
        Req 4.10 – Fallback search-box detection via aria-label
    """

    def __init__(
        self,
        driver: Any = None,
        scroll: Optional[BrowserScrollAction] = None,
        video: Optional[VideoPlaybackController] = None,
        multilingual: Optional[MultilingualInputHandler] = None,
        wait: Optional[WaitStrategy] = None,
        screenshot: Optional[ScreenshotService] = None,
    ) -> None:
        self.driver = driver
        self.scroll = scroll or BrowserScrollAction()
        self.video = video or VideoPlaybackController()
        self.multilingual = multilingual or MultilingualInputHandler()
        self.wait = wait or WaitStrategy()
        self.screenshot = screenshot or ScreenshotService()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_page(self) -> Any:
        """Return the live Playwright Page from the injected driver."""
        if self.driver is None:
            return None
        return await self.driver.get_page()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_full_flow(
        self,
        query: str,
        language_filter: Optional[str] = None,
        scroll_results: bool = True,
        card_index: int = 0,
        pause_after_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute the complete YouTube automation flow end-to-end.

        Example invocation::

            await skill.run_full_flow(
                query="AR Rahman songs",
                language_filter="Telugu",
                pause_after_seconds=30,
            )

        Returns a result dict with ``success``, ``error``, and ``steps`` keys.
        Each step appends a small dict to ``steps`` so callers can audit progress.
        """
        results: Dict[str, Any] = {"steps": [], "success": False, "error": None}

        try:
            # ── Step 1: Navigate to YouTube (Req 4.1) ────────────────────────
            logger.info("YouTubeSkill: navigating to %s", BASE_URL)
            nav = await self.driver.navigate(BASE_URL)
            results["steps"].append({"step": "navigate", "success": nav.success})
            if not nav.success:
                results["error"] = f"Navigation failed: {nav.error}"
                return results

            page = await self._get_page()
            await self.screenshot.capture(page, "youtube_home")

            # ── Step 2: Search (Req 4.2, 4.3, 4.4, 4.5) ─────────────────────
            cards = await self.search(query, language_filter)
            results["steps"].append({"step": "search", "cards_found": len(cards)})
            logger.info("YouTubeSkill: found %d video cards", len(cards))

            # ── Step 3: Scroll results (Req 4.6) ─────────────────────────────
            if scroll_results and cards:
                scroll_res = await self.scroll_results(600)
                results["steps"].append(
                    {"step": "scroll", "scroll_y": scroll_res.scroll_y}
                )
                await asyncio.sleep(1)  # allow lazy-loaded cards to render

            # ── Step 4: Play video (Req 4.7, 4.8) ────────────────────────────
            if cards:
                play_res = await self.play_video(card_index)
                results["steps"].append(
                    {"step": "play", "success": play_res.success}
                )
                page = await self._get_page()
                await self.screenshot.capture(page, "youtube_playing")

                # ── Step 5: Pause after N seconds if requested ────────────────
                if pause_after_seconds is not None and play_res.success:
                    logger.info(
                        "YouTubeSkill: waiting %.0fs before pause", pause_after_seconds
                    )
                    await asyncio.sleep(pause_after_seconds)
                    pause_res = await self.video.pause(page)
                    results["steps"].append(
                        {
                            "step": "pause",
                            "success": pause_res.success,
                            "paused_at": pause_res.video_state.get("currentTime", 0),
                        }
                    )
                    await self.screenshot.capture(page, "youtube_paused")

            results["success"] = True

        except Exception as exc:
            logger.error("YouTubeSkill.run_full_flow failed: %s", exc)
            results["error"] = str(exc)

        return results

    async def search(
        self, query: str, language_filter: Optional[str] = None
    ) -> List[VideoCard]:
        """Search YouTube for *query* and return the parsed video cards.

        Steps:
        1. Find the search box (primary selector or aria-label fallback).
        2. Fill the query via MultilingualInputHandler (supports Telugu, etc.).
        3. Press Enter and wait for the results page.
        4. Optionally apply a language filter (Req 4.5).
        5. Parse and return all visible VideoCard objects (Req 4.4).
        """
        page = await self._get_page()
        if page is None:
            logger.error("YouTubeSkill.search: no page available")
            return []

        # Req 4.2: locate search box
        search_selector = await self._find_search_box(page)
        if not search_selector:
            logger.error("YouTubeSkill.search: could not find search box")
            return []

        # Fill query — Unicode-safe (Req 8.1 / 8.2)
        fill_res = await self.multilingual.fill(page, search_selector, query)
        if not fill_res.success:
            logger.error(
                "YouTubeSkill.search: failed to fill search box: %s", fill_res.error
            )
            return []

        # Req 4.3: submit and wait for results
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        try:
            await self.wait.wait_for_element(
                page, VIDEO_CARD_SELECTOR, timeout_ms=SEARCH_TIMEOUT_MS
            )
        except Exception as exc:
            logger.error(
                "YouTubeSkill.search: results page did not load: %s", exc
            )
            return []

        # Req 4.5: apply language filter if requested
        if language_filter:
            await self._apply_language_filter(page, language_filter)

        # Req 4.4: parse cards
        cards = await self._parse_video_cards(page)
        await self.screenshot.capture(page, "youtube_search_results")
        return cards

    async def scroll_results(self, pixels: int = 600) -> ScrollResult:
        """Scroll the results page by *pixels* downward (Req 4.6)."""
        page = await self._get_page()
        if page is None:
            return ScrollResult(
                success=False,
                scroll_y=0.0,
                action="scrollBy",
                error="No page available",
            )
        return await self.scroll.scroll(page, direction="down", pixels=pixels)

    async def play_video(self, card_index: int = 0) -> PlaybackResult:
        """Click the video card at *card_index* and wait for the player (Req 4.7, 4.8).

        Falls back to index 0 if *card_index* is out of range.
        After clicking, waits for network idle and the video element, then
        calls VideoPlaybackController.play() which verifies the player is
        present (Req 4.8) and starts playback.
        """
        page = await self._get_page()
        if page is None:
            return PlaybackResult(
                success=False, action="play", error="No page available"
            )

        try:
            elements = await page.query_selector_all(VIDEO_CARD_SELECTOR)
            if not elements:
                return PlaybackResult(
                    success=False, action="play", error="No video cards found"
                )

            # Clamp index to available range
            idx = card_index if card_index < len(elements) else 0
            card = elements[idx]

            # Prefer thumbnail link; fall back to title link then the card itself
            click_target = await card.query_selector("a#thumbnail")
            if click_target is None:
                click_target = await card.query_selector("#video-title")
            if click_target is not None:
                await click_target.click()
            else:
                await card.click()

            # Wait for the player page to settle
            await self.wait.wait_for_network_idle(page, timeout_ms=20_000)
            await asyncio.sleep(2)  # YouTube's JS needs a moment to initialise the player

            # Req 4.8: verify player and start playback
            play_res = await self.video.play(page)
            logger.info(
                "YouTubeSkill.play_video(card_index=%d) success=%s",
                idx,
                play_res.success,
            )
            return play_res

        except Exception as exc:
            logger.error("YouTubeSkill.play_video failed: %s", exc)
            return PlaybackResult(success=False, action="play", error=str(exc))

    async def set_playback_language(self, language: str) -> PlaybackResult:
        """Switch the audio/subtitle track to *language* (Req 4.9)."""
        page = await self._get_page()
        if page is None:
            return PlaybackResult(
                success=False, action="language", error="No page available"
            )
        return await self.video.set_language(page, language)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _find_search_box(self, page: Any) -> Optional[str]:
        """Return a CSS selector for the YouTube search box.

        Primary:   ``input#search``  (Req 4.2)
        Secondary: ``ytd-searchbox input`` or ``input[name='search_query']``
        Fallback:  any *input* element whose ``aria-label`` contains "Search"
                   (Req 4.10) — explicitly restricts to input/textarea to avoid
                   matching the search *button*.
        Returns ``None`` if no strategy succeeds.
        """
        # Primary attempt
        try:
            await self.wait.wait_for_element(
                page, SEARCH_SELECTOR, timeout_ms=SEARCH_TIMEOUT_MS
            )
            return SEARCH_SELECTOR
        except Exception:
            logger.warning(
                "YouTubeSkill: primary search selector '%s' not found, "
                "trying secondary selectors",
                SEARCH_SELECTOR,
            )

        # Secondary: known YouTube search-box selectors
        for secondary in (
            "input[name='search_query']",
            "ytd-searchbox input",
            "ytd-searchbox textarea",
        ):
            try:
                el = await page.query_selector(secondary)
                if el:
                    logger.info(
                        "YouTubeSkill: found search box via secondary selector '%s'",
                        secondary,
                    )
                    return secondary
            except Exception:
                continue

        # Req 4.10 fallback: restrict to input/textarea with aria-label containing "Search"
        try:
            # Query all inputs and textareas; pick the first whose aria-label
            # mentions "search" (case-insensitive) to avoid matching the button.
            candidates = await page.query_selector_all("input, textarea")
            for el in candidates:
                try:
                    aria = (await el.get_attribute("aria-label") or "").strip()
                    placeholder = (await el.get_attribute("placeholder") or "").strip()
                    if "search" in aria.lower() or "search" in placeholder.lower():
                        # Build a unique selector using id/name, or fall back to
                        # a strict input[aria-label] selector.
                        el_id = (await el.get_attribute("id") or "").strip()
                        el_name = (await el.get_attribute("name") or "").strip()
                        if el_id:
                            fallback_sel = f"input#{el_id}"
                        elif el_name:
                            fallback_sel = f"input[name='{el_name}']"
                        else:
                            safe_aria = aria.replace("'", "\\'")
                            fallback_sel = f"input[aria-label='{safe_aria}']"
                        logger.info(
                            "YouTubeSkill: fallback search box found: %s", fallback_sel
                        )
                        return fallback_sel
                except Exception:
                    continue
        except Exception as exc:
            logger.error(
                "YouTubeSkill: aria-label fallback search also failed: %s", exc
            )

        return None

    async def _apply_language_filter(self, page: Any, language: str) -> None:
        """Open YouTube's filter panel and select *language* (Req 4.5).

        Silently logs a warning and returns if the filter button or the
        matching language option cannot be found — the search results are
        still useful without the filter applied.
        """
        try:
            # Try the primary filter button, then a more permissive fallback
            filter_btn = await page.query_selector(FILTER_BTN_SELECTOR)
            if filter_btn is None:
                filter_btn = await page.query_selector(
                    "button[aria-label*='filter' i]"
                )
            if filter_btn is None:
                logger.warning(
                    "YouTubeSkill: filter button not found, skipping language filter"
                )
                return

            await filter_btn.click()
            await asyncio.sleep(0.7)  # wait for filter panel animation

            # Search for the language option inside the filter panel
            options = await page.query_selector_all("ytd-search-filter-renderer")
            for opt in options:
                text = await opt.inner_text()
                if language.lower() in text.lower():
                    await opt.click()
                    await asyncio.sleep(1)  # wait for filtered results to load
                    logger.info(
                        "YouTubeSkill: applied language filter '%s'", language
                    )
                    return

            logger.warning(
                "YouTubeSkill: language option '%s' not found in filter panel",
                language,
            )
        except Exception as exc:
            logger.warning(
                "YouTubeSkill._apply_language_filter failed: %s", exc
            )

    async def _parse_video_cards(self, page: Any) -> List[VideoCard]:
        """Read the first 20 visible video-card elements and return them as VideoCard objects.

        Gracefully skips any card that raises an exception during extraction so
        that a single malformed card does not abort the entire parse (Req 4.4).
        """
        cards: List[VideoCard] = []
        try:
            elements = await page.query_selector_all(VIDEO_CARD_SELECTOR)
            for i, el in enumerate(elements[:20]):
                try:
                    # Title
                    title_el = await el.query_selector("#video-title")
                    title = (
                        (await title_el.inner_text()).strip() if title_el else ""
                    )

                    # Channel name
                    channel_el = await el.query_selector("ytd-channel-name")
                    channel = (
                        (await channel_el.inner_text()).strip() if channel_el else ""
                    )

                    # View count (first metadata span)
                    views_el = await el.query_selector(
                        "#metadata-line span:first-child"
                    )
                    view_count = (
                        (await views_el.inner_text()).strip() if views_el else ""
                    )

                    if title:  # only keep cards that have at least a title
                        cards.append(
                            VideoCard(
                                index=i,
                                title=title[:80],
                                channel=channel[:50],
                                view_count=view_count[:30],
                                selector=f"{VIDEO_CARD_SELECTOR}:nth-of-type({i + 1})",
                            )
                        )
                except Exception as card_exc:
                    logger.debug(
                        "YouTubeSkill: skipping card %d due to parse error: %s",
                        i,
                        card_exc,
                    )
                    continue

        except Exception as exc:
            logger.error("YouTubeSkill._parse_video_cards failed: %s", exc)

        return cards
