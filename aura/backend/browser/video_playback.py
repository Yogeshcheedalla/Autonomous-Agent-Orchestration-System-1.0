from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VIDEO_SELECTOR = "video.html5-main-video"
VIDEO_TIMEOUT_MS = 10_000


@dataclass
class PlaybackResult:
    success: bool
    action: str
    video_state: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class VideoPlaybackController:
    """Controls video player via Playwright page.evaluate().

    Targets YouTube's video element (video.html5-main-video).
    All controls use JavaScript evaluate() so they work regardless
    of keyboard focus or window state.
    """

    async def _get_video_state(self, page: Any) -> Dict[str, Any]:
        """Read current video element state."""
        try:
            return await page.evaluate(f"""() => {{
                const v = document.querySelector('{VIDEO_SELECTOR}');
                if (!v) return {{}};
                return {{
                    paused: v.paused,
                    currentTime: v.currentTime,
                    duration: v.duration,
                    playbackRate: v.playbackRate,
                    muted: v.muted,
                    ended: v.ended
                }};
            }}""")
        except Exception:
            return {}

    async def _wait_for_video(self, page: Any) -> bool:
        """Wait up to VIDEO_TIMEOUT_MS for video element to appear.

        Req 7.7: If VIDEO_SELECTOR not found in 10s, return failure result.
        """
        try:
            await page.wait_for_selector(VIDEO_SELECTOR, timeout=VIDEO_TIMEOUT_MS)
            return True
        except Exception:
            return False

    async def play(self, page: Any) -> PlaybackResult:
        """Play the video.

        Req 7.1: click play or call videoElement.play() via evaluate().
        """
        if not await self._wait_for_video(page):
            return PlaybackResult(
                success=False,
                action="play",
                error=f"Video element '{VIDEO_SELECTOR}' not found within {VIDEO_TIMEOUT_MS}ms",
            )
        try:
            await page.evaluate(f"document.querySelector('{VIDEO_SELECTOR}').play()")
            await asyncio.sleep(0.5)
            state = await self._get_video_state(page)
            logger.info("VideoPlaybackController: play() - paused=%s", state.get("paused"))
            return PlaybackResult(success=True, action="play", video_state=state)
        except Exception as e:
            return PlaybackResult(success=False, action="play", error=str(e))

    async def pause(self, page: Any) -> PlaybackResult:
        """Pause the video.

        Req 7.2: click pause or call videoElement.pause() via evaluate().
        """
        if not await self._wait_for_video(page):
            return PlaybackResult(
                success=False,
                action="pause",
                error=f"Video element '{VIDEO_SELECTOR}' not found within {VIDEO_TIMEOUT_MS}ms",
            )
        try:
            await page.evaluate(f"document.querySelector('{VIDEO_SELECTOR}').pause()")
            await asyncio.sleep(0.3)
            state = await self._get_video_state(page)
            logger.info("VideoPlaybackController: pause() - paused=%s", state.get("paused"))
            return PlaybackResult(success=True, action="pause", video_state=state)
        except Exception as e:
            return PlaybackResult(success=False, action="pause", error=str(e))

    async def seek(self, page: Any, position_seconds: float) -> PlaybackResult:
        """Seek to position_seconds.

        Req 7.3: set videoElement.currentTime via evaluate().
        """
        if not await self._wait_for_video(page):
            return PlaybackResult(
                success=False,
                action="seek",
                error=f"Video element '{VIDEO_SELECTOR}' not found within {VIDEO_TIMEOUT_MS}ms",
            )
        try:
            await page.evaluate(
                f"document.querySelector('{VIDEO_SELECTOR}').currentTime = {position_seconds}"
            )
            await asyncio.sleep(0.3)
            state = await self._get_video_state(page)
            logger.info(
                "VideoPlaybackController: seek(%.1f) - currentTime=%.1f",
                position_seconds,
                state.get("currentTime", 0),
            )
            return PlaybackResult(success=True, action="seek", video_state=state)
        except Exception as e:
            return PlaybackResult(success=False, action="seek", error=str(e))

    async def set_speed(self, page: Any, rate: float) -> PlaybackResult:
        """Set playback rate (e.g. 0.5, 1.0, 1.5, 2.0).

        Req 7.5: set videoElement.playbackRate via evaluate().
        """
        if not await self._wait_for_video(page):
            return PlaybackResult(
                success=False,
                action="speed",
                error=f"Video element '{VIDEO_SELECTOR}' not found within {VIDEO_TIMEOUT_MS}ms",
            )
        try:
            await page.evaluate(
                f"document.querySelector('{VIDEO_SELECTOR}').playbackRate = {rate}"
            )
            state = await self._get_video_state(page)
            logger.info("VideoPlaybackController: set_speed(%.1f)", rate)
            return PlaybackResult(success=True, action="speed", video_state=state)
        except Exception as e:
            return PlaybackResult(success=False, action="speed", error=str(e))

    async def fullscreen(self, page: Any) -> PlaybackResult:
        """Enter fullscreen via YouTube fullscreen button.

        Req 7.4: click fullscreen button in the video player controls.
        """
        try:
            fs_selectors = [
                ".ytp-fullscreen-button",
                "button.ytp-fullscreen-button",
                "[title='Full screen']",
                "[aria-label='Full screen']",
            ]
            for sel in fs_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        state = await self._get_video_state(page)
                        logger.info("VideoPlaybackController: fullscreen() via selector=%s", sel)
                        return PlaybackResult(success=True, action="fullscreen", video_state=state)
                except Exception:
                    continue
            return PlaybackResult(
                success=False,
                action="fullscreen",
                error="Fullscreen button not found in any known selector",
            )
        except Exception as e:
            return PlaybackResult(success=False, action="fullscreen", error=str(e))

    async def set_language(self, page: Any, language: str) -> PlaybackResult:
        """Open YouTube settings and select audio/subtitle language.

        Req 7.6: open settings menu and select language.
        """
        try:
            # Click the settings gear button
            await page.click(".ytp-settings-button")
            await asyncio.sleep(0.5)

            # Look for menu items containing the language name
            items = await page.query_selector_all(".ytp-menuitem")
            for item in items:
                text = await item.inner_text()
                if language.lower() in text.lower():
                    await item.click()
                    await asyncio.sleep(0.3)
                    state = await self._get_video_state(page)
                    logger.info("VideoPlaybackController: set_language(%s)", language)
                    return PlaybackResult(success=True, action="language", video_state=state)

            # Close settings menu if the language was not found
            await page.keyboard.press("Escape")
            return PlaybackResult(
                success=False,
                action="language",
                error=f"Language '{language}' not found in player settings menu",
            )
        except Exception as e:
            return PlaybackResult(success=False, action="language", error=str(e))
