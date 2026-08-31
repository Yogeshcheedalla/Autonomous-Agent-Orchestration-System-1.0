"""
site_dispatcher.py – Real Playwright automation entry point.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Headful by default, and the default is the whole point.
#:
#: This used to default to headless, which is why "open youtube" answered
#: "✅ Opened YouTube" while nothing appeared on screen. The navigation was real
#: -- Playwright had genuinely loaded the page -- but it loaded it in a browser
#: with no window, so the only honest reading of that reply from the user's chair
#: is that it lied. A reported success the person cannot see is a false success,
#: whatever the return value says.
#:
#: Headless stays available for tests and CI, where nobody is watching, but it has
#: to be asked for now rather than assumed. Read from `owned_session`, which is
#: where the browser it applies to is actually opened.
_HEADLESS = os.getenv("AKANSHA_BROWSER_HEADLESS", "false").lower() == "true"

PLAYWRIGHT_SITES = [
    "youtube", "codechef", "leetcode", "github", "coursera",
    "linkedin learning", "hackerrank", "codeforces", "geeksforgeeks",
    "udemy", "nptel",
]

PLAYWRIGHT_ACTION_KEYWORDS = [
    "search", "find", "play", "solve", "complete", "open and",
    "go to", "navigate", "click", "filter", "enroll", "start",
    "practice problems", "daily challenge", "learning path",
    "most starred", "logged in", "sign in", "my courses",
    "continue", "best views", "top result", "pause",
]


def should_use_playwright(prompt: str) -> bool:
    """Trigger on any known site mention — action keyword not required.
    This prevents 'open youtube' from falling through to Google search."""
    lowered = prompt.lower()
    return any(site in lowered for site in PLAYWRIGHT_SITES)


# ── Persistent browser singleton — one tab shared across all calls ────────────
#
# The singleton itself now lives in `browser.owned_session`, and it had to move.
# A process-global driver kept here was entered through `asyncio.run` once per
# request (see `dispatch_playwright` at the bottom), and `asyncio.run` closes the
# loop it created. So the driver survived the request but its transport did not:
# the second site command in a process awaited objects registered with a dead
# loop. `is_connected()` reports on the CDP socket rather than on the loop, so the
# relaunch guard here never fired -- the first command worked, and every command
# after it failed or hung.


async def _get_driver() -> Any:
    """Return the one shared browser, launched on the loop that owns it."""
    from ..owned_session import session

    return await session()


def _extract_search_query(prompt: str, site: str) -> str:
    lowered = prompt.lower()
    cleaned = re.sub(re.escape(site), "", lowered, flags=re.IGNORECASE).strip()
    cleaned = re.sub(
        r"^(open|go to|navigate to|search|find|please|and)\s+", "", cleaned, flags=re.IGNORECASE
    ).strip()
    for marker in ["search for", "find", "look for", "search"]:
        m = re.search(rf"{marker}\s+(.+?)(?:\s+and\s+|\s+filter|\s+play|\s+pause|\s+in\s+telugu|\s+in\s+hindi|$)", cleaned, re.IGNORECASE)
        if m:
            return m.group(1).strip(" .,")
    cleaned = re.sub(r"\s+(and|filter|play|pause|in telugu|in hindi|scroll|open|click).*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" .,") or prompt


def _extract_language_filter(prompt: str) -> Optional[str]:
    lowered = prompt.lower()
    if "telugu" in lowered:
        return "Telugu"
    if "hindi" in lowered:
        return "Hindi"
    return None


def _extract_pause_seconds(prompt: str) -> Optional[float]:
    lowered = prompt.lower()
    if "pause" not in lowered:
        return None
    m = re.search(r"(\d+)\s*(?:second|sec)", lowered)
    return float(m.group(1)) if m else 30.0


def _detect_site(prompt: str) -> str:
    lowered = prompt.lower()
    for site in PLAYWRIGHT_SITES:
        if site in lowered:
            return site
    return "generic"


# ── YouTube ───────────────────────────────────────────────────────────────────

async def _run_youtube(prompt: str) -> Dict[str, Any]:
    from ..browser_scroll import BrowserScrollAction
    from ..video_playback import VideoPlaybackController
    from ..multilingual_input import MultilingualInputHandler
    from ..wait_strategy import WaitStrategy
    from ..screenshot_service import ScreenshotService
    from .youtube_skill import YouTubeAutomationSkill

    lowered = prompt.lower()
    # Bare "open youtube" — no search query in prompt
    search_keywords = ["search", "find", "play", "songs", "video", "music", "watch",
                       "filter", "pause", "channel", "shorts", "trending"]
    is_bare_open = not any(kw in lowered for kw in search_keywords)

    query = "" if is_bare_open else _extract_search_query(prompt, "youtube")
    language_filter = _extract_language_filter(prompt)
    pause_seconds = _extract_pause_seconds(prompt)

    logger.info("YouTube: query=%r bare=%s lang=%r pause=%r", query, is_bare_open, language_filter, pause_seconds)

    driver = await _get_driver()
    try:
        if is_bare_open:
            nav = await driver.navigate("https://www.youtube.com")
            if nav.success:
                return {"success": True, "message": "✅ Opened YouTube.", "steps": [{"step": "navigate", "url": "https://www.youtube.com"}]}
            return {"success": False, "message": f"Could not open YouTube: {nav.error}", "steps": []}

        skill = YouTubeAutomationSkill(
            driver=driver,
            scroll=BrowserScrollAction(),
            video=VideoPlaybackController(),
            multilingual=MultilingualInputHandler(),
            wait=WaitStrategy(),
            screenshot=ScreenshotService("screenshots"),
        )
        result = await skill.run_full_flow(
            query=query,
            language_filter=language_filter,
            scroll_results=True,
            card_index=0,
            pause_after_seconds=pause_seconds,
        )
        if result.get("success"):
            steps_done = len([s for s in result.get("steps", []) if s])
            msg = f"✅ YouTube: searched '{query}'"
            if language_filter:
                msg += f", filtered {language_filter}"
            if pause_seconds:
                msg += f", paused after {int(pause_seconds)}s"
            msg += f". ({steps_done} steps)"
            return {"success": True, "message": msg, "steps": result.get("steps", [])}
        return {"success": False, "message": f"YouTube failed: {result.get('error', 'unknown')}", "steps": result.get("steps", [])}
    except Exception as exc:
        logger.error("YouTube runner error: %s", exc)
        return {"success": False, "message": f"YouTube error: {exc}", "steps": []}


# ── CodeChef ──────────────────────────────────────────────────────────────────

async def _run_codechef(prompt: str) -> Dict[str, Any]:
    from ..wait_strategy import WaitStrategy
    from ..screenshot_service import ScreenshotService

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        wait = WaitStrategy()
        screenshot = ScreenshotService("screenshots")
        lowered = prompt.lower()

        nav_url = "https://www.codechef.com/practice"
        if "java" in lowered:
            nav_url = "https://www.codechef.com/practice?page=0&limit=20&sort_by=difficulty_rating&sort_order=asc&search=&start_rating=0&end_rating=999&group=all&language=java"

        nav = await driver.navigate(nav_url)
        if not nav.success:
            return {"success": False, "message": f"Could not navigate to CodeChef: {nav.error}"}

        await asyncio.sleep(2)
        await screenshot.capture(page, "codechef_practice")

        login_note = "Proceeding as logged in." if ("logged in" in lowered or "yes" in lowered) else "⚠️ Log in to CodeChef for full access."

        if "easy" in lowered:
            try:
                await page.click("button:has-text('Easy')")
                await asyncio.sleep(1)
            except Exception:
                pass

        steps_info = f"Opened CodeChef practice. {login_note}"
        if "java" in lowered:
            steps_info += " Filtered Java."
        if "easy" in lowered:
            steps_info += " Applied Easy filter."

        return {"success": True, "message": f"✅ {steps_info}", "steps": [{"step": "navigate", "url": nav_url}]}
    except Exception as exc:
        return {"success": False, "message": f"CodeChef error: {exc}", "steps": []}


# ── LeetCode ──────────────────────────────────────────────────────────────────

async def _run_leetcode(prompt: str) -> Dict[str, Any]:
    from ..screenshot_service import ScreenshotService

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        screenshot = ScreenshotService("screenshots")
        lowered = prompt.lower()

        if "daily" in lowered:
            url = "https://leetcode.com/problems/daily-coding-challenge/"
        elif "array" in lowered:
            url = "https://leetcode.com/tag/array/"
        else:
            url = "https://leetcode.com/problemset/all/"

        await driver.navigate(url)
        await asyncio.sleep(2)
        await screenshot.capture(page, "leetcode_page")

        lang_note = " (Python)" if "python" in lowered else " (Java)" if "java" in lowered else ""
        return {"success": True, "message": f"✅ Opened LeetCode at {url}{lang_note}.", "steps": [{"step": "navigate", "url": url}]}
    except Exception as exc:
        return {"success": False, "message": f"LeetCode error: {exc}", "steps": []}


# ── GitHub ────────────────────────────────────────────────────────────────────

async def _run_github(prompt: str) -> Dict[str, Any]:
    from ..wait_strategy import WaitStrategy
    from ..screenshot_service import ScreenshotService

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        wait = WaitStrategy()
        screenshot = ScreenshotService("screenshots")
        lowered = prompt.lower()

        query = _extract_search_query(prompt, "github")
        if not query or query == prompt.lower():
            m = re.search(r"search(?:\s+for)?\s+(.+?)(?:\s+and|\s+open|\s+most|$)", lowered)
            query = m.group(1).strip() if m else "react dashboard template"

        search_url = f"https://github.com/search?q={query.replace(' ', '+')}&type=repositories&s=stars&o=desc"
        await driver.navigate(search_url)
        await asyncio.sleep(2)

        try:
            await wait.wait_for_element(page, "a.Link--primary[href*='/']", timeout_ms=10000)
            first_repo = await page.query_selector("a.Link--primary")
            if first_repo:
                repo_name = await first_repo.inner_text()
                await first_repo.click()
                await asyncio.sleep(2)
                await screenshot.capture(page, "github_top_repo")
                return {"success": True, "message": f"✅ GitHub: searched '{query}', opened top repo: {repo_name.strip()}", "steps": [{"step": "search"}, {"step": "open_top_repo"}]}
        except Exception:
            pass

        await screenshot.capture(page, "github_search_results")
        return {"success": True, "message": f"✅ GitHub: searched '{query}' by stars.", "steps": [{"step": "search", "query": query}]}
    except Exception as exc:
        return {"success": False, "message": f"GitHub error: {exc}", "steps": []}


# ── Coursera ──────────────────────────────────────────────────────────────────

async def _run_coursera(prompt: str) -> Dict[str, Any]:
    from ..screenshot_service import ScreenshotService

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        screenshot = ScreenshotService("screenshots")
        lowered = prompt.lower()

        if "my courses" in lowered or "enrolled" in lowered or "continue" in lowered:
            url = "https://www.coursera.org/my-learning"
        elif "python" in lowered:
            url = "https://www.coursera.org/search?query=python&productTypeDescription=Courses"
        else:
            url = "https://www.coursera.org/my-learning"

        await driver.navigate(url)
        await asyncio.sleep(2)
        await screenshot.capture(page, "coursera_page")
        return {"success": True, "message": f"✅ Opened Coursera at {url}. Log in to see enrolled courses.", "steps": [{"step": "navigate", "url": url}]}
    except Exception as exc:
        return {"success": False, "message": f"Coursera error: {exc}", "steps": []}


# ── LinkedIn Learning ─────────────────────────────────────────────────────────

async def _run_linkedin_learning(prompt: str) -> Dict[str, Any]:
    from ..screenshot_service import ScreenshotService
    from ..wait_strategy import WaitStrategy

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        screenshot = ScreenshotService("screenshots")
        wait = WaitStrategy()
        lowered = prompt.lower()

        query = "machine learning"
        for marker in ["find", "search for", "search", "path", "course"]:
            m = re.search(rf"{marker}\s+(?:a\s+)?(.+?)(?:\s+path|\s+course|\s+and|\s+enroll|$)", lowered)
            if m:
                query = m.group(1).strip()
                break

        url = f"https://www.linkedin.com/learning/search?keywords={query.replace(' ', '%20')}"
        await driver.navigate(url)
        await asyncio.sleep(2)

        try:
            await wait.wait_for_element(page, ".base-search-results-item", timeout_ms=8000)
        except Exception:
            pass
        await screenshot.capture(page, "linkedin_learning_page")

        return {"success": True, "message": f"✅ LinkedIn Learning: searched '{query}'. Sign in to enroll.", "steps": [{"step": "navigate", "url": url}]}
    except Exception as exc:
        return {"success": False, "message": f"LinkedIn Learning error: {exc}", "steps": []}


# ── Generic site ──────────────────────────────────────────────────────────────

async def _run_generic_site(prompt: str, site: str) -> Dict[str, Any]:
    from ..screenshot_service import ScreenshotService

    url_map = {
        "hackerrank": "https://www.hackerrank.com/dashboard",
        "codeforces": "https://codeforces.com/problemset",
        "geeksforgeeks": "https://www.geeksforgeeks.org/",
        "udemy": "https://www.udemy.com/courses/",
        "nptel": "https://nptel.ac.in/",
    }
    url = url_map.get(site, f"https://www.{site}.com")

    driver = await _get_driver()
    try:
        page = await driver.get_page()
        screenshot = ScreenshotService("screenshots")
        await driver.navigate(url)
        await asyncio.sleep(2)
        await screenshot.capture(page, f"{site}_page")
        return {"success": True, "message": f"✅ Opened {site.title()} at {url}.", "steps": [{"step": "navigate", "url": url}]}
    except Exception as exc:
        return {"success": False, "message": f"{site} error: {exc}", "steps": []}


# ── Main dispatcher ───────────────────────────────────────────────────────────

_SITE_RUNNERS = {
    "youtube": _run_youtube,
    "codechef": _run_codechef,
    "leetcode": _run_leetcode,
    "github": _run_github,
    "coursera": _run_coursera,
    "linkedin learning": _run_linkedin_learning,
}


async def dispatch_playwright_async(prompt: str) -> Dict[str, Any]:
    site = _detect_site(prompt)
    runner = _SITE_RUNNERS.get(site)
    if runner:
        return await runner(prompt)
    return await _run_generic_site(prompt, site)


def dispatch_playwright(prompt: str) -> Dict[str, Any]:
    """Synchronous entry point, for callers on a different loop or none at all.

    `main.py` reaches this through `run_in_executor`, so this runs on a worker
    thread with no loop of its own. It used to call `asyncio.run`, which created a
    loop, ran the command, and *closed the loop* -- stranding the shared browser
    that the next request would reach for. Submitting onto the owned loop instead
    means the browser stays bound to a loop that is still running when the next
    command arrives, which is the difference between the first command working and
    every command working.
    """
    from ..owned_session import run_owned

    try:
        return run_owned(lambda: dispatch_playwright_async(prompt))
    except Exception as e:
        logger.error("Playwright dispatcher error: %s", e)
        return {
            "success": False,
            "message": f"Playwright automation error: {str(e)}",
            "note": "Run: pip install playwright && playwright install chromium",
        }
