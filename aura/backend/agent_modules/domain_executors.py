from __future__ import annotations

import re
import asyncio
import logging as _logging
from typing import Any, Dict, List, Optional
from .browser_automation import BrowserAutomationModule
from .desktop_automation import DesktopAutomationModule
from .task_scheduler import TaskSchedulerModule
from .openwork_bridge import OpenWorkCapabilityBridge
from .continuous_jarvis_engine import ContinuousVoiceJarvisEngine

_logger = _logging.getLogger(__name__)


def _try_import_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


PLAYWRIGHT_AVAILABLE = _try_import_playwright()

if PLAYWRIGHT_AVAILABLE:
    from ..browser.playwright_driver import PlaywrightDriver, NavigationResult as _NavResult
    from ..browser.dom_snapshot import LiveDOMSnapshot
    from ..browser.compact_ref_registry import CompactRefRegistry
    from ..browser.multilingual_input import MultilingualInputHandler
    from ..browser.wait_strategy import WaitStrategy
    from ..browser.browser_scroll import BrowserScrollAction
    from ..browser.video_playback import VideoPlaybackController
    from ..browser.screenshot_service import ScreenshotService
    from ..browser.error_recovery import BrowserErrorRecovery
    from ..browser.skills.youtube_skill import YouTubeAutomationSkill
    from ..browser.skills.generic_site_skill import GenericSiteSkill


class DesktopDomainExecutor:
    """Isolated Domain Executor for Desktop & File System Operations."""

    def __init__(self, desktop_module: Optional[DesktopAutomationModule] = None) -> None:
        self.desktop = desktop_module or DesktopAutomationModule()

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action in ("switch_window", "desktop_switch_window"):
            win = self.desktop.switch_window(params.get("title", "Main"), params.get("pid"))
            return {"status": "success", "domain": "desktop", "window": win.title, "pid": win.pid}
        elif action in ("edit_document", "write_file"):
            res = self.desktop.edit_document(params.get("file_path", ""), params.get("content", ""), params.get("append", False))
            return {"status": "success" if res.success else "failed", "domain": "desktop", "details": res.details}
        elif action in ("inspect_directory", "inspect_filesystem", "list_folder"):
            target = params.get("target_path") or params.get("path") or params.get("folder") or "Downloads"
            res = self.desktop.inspect_directory(target, params.get("max_items", 50))
            return {"status": res.get("status", "success"), "domain": "desktop", "target_path": res.get("target_path"), "markdown": res.get("markdown", "")}
        elif action in ("accessibility_click", "desktop_click"):
            res = self.desktop.accessibility_action(params.get("element_id", ""), "click", params.get("pid"))
            return {"status": "success", "domain": "desktop", "result": res}
        elif action in ("desktop_type"):
            res = self.desktop.accessibility_action(params.get("element_id", "input"), "type", params.get("pid"))
            return {"status": "success", "domain": "desktop", "result": res, "text_typed": params.get("text", "")}
        else:
            cli_res = self.desktop.execute_cli_action(params.get("command", "dir"))
            return {"status": cli_res.get("status", "success"), "domain": "desktop", "result": cli_res}



class BrowserDomainExecutor:
    """Real Playwright-based browser executor.

    When Playwright is installed (PLAYWRIGHT_AVAILABLE=True), uses real
    Chromium automation. Falls back to legacy BrowserAutomationModule
    (urllib) when Playwright is not installed.
    """

    def __init__(self, browser_module: Optional[BrowserAutomationModule] = None) -> None:
        if PLAYWRIGHT_AVAILABLE:
            # Real Playwright stack. The driver is a handle on the *shared* browser,
            # not a browser of its own: constructing a real `PlaywrightDriver` here
            # opened a second window on a second profile beside the dispatcher's,
            # and did it at construction time whether or not anyone browsed.
            from ..browser.owned_session import LazyOwnedDriver

            self._driver = LazyOwnedDriver()
            self._snapshot = LiveDOMSnapshot()
            self._registry = CompactRefRegistry()
            self._multilingual = MultilingualInputHandler()
            self._wait = WaitStrategy()
            self._scroll = BrowserScrollAction()
            self._video = VideoPlaybackController()
            self._screenshot = ScreenshotService("screenshots")
            self._recovery = BrowserErrorRecovery()
            self._youtube = YouTubeAutomationSkill(
                driver=self._driver,
                scroll=self._scroll,
                video=self._video,
                multilingual=self._multilingual,
                wait=self._wait,
                screenshot=self._screenshot,
            )
            self._generic = GenericSiteSkill()
            _logger.info("BrowserDomainExecutor: using real Playwright driver")
        else:
            _logger.warning(
                "BrowserDomainExecutor: Playwright not installed. "
                "Using legacy urllib stub. Run: pip install playwright && playwright install chromium"
            )
            self.browser = browser_module or BrowserAutomationModule()
        self.playwright_available = PLAYWRIGHT_AVAILABLE

    def _run(self, coro):
        """Run one browser coroutine on the loop that owns the browser.

        This used to be three fallbacks ending in `asyncio.run(coro)`, and every
        one of them created a loop and closed it again -- so the shared driver was
        left holding a transport registered with a dead loop and the *next* call
        failed. `run_owned` submits onto the loop the browser was launched on,
        which stays running for the life of the process.
        """
        from ..browser.owned_session import run_owned

        return run_owned(lambda: coro)

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.playwright_available:
            return self._legacy_execute(action, params)

        action_lower = action.lower()

        # ── YouTube full automation flow ──────────────────────────────
        if action_lower in ("youtube_flow", "youtube_automation"):
            result = self._run(self._youtube.run_full_flow(
                query=params.get("query", ""),
                language_filter=params.get("language_filter"),
                scroll_results=params.get("scroll", True),
                card_index=params.get("card_index", 0),
                pause_after_seconds=params.get("pause_after_seconds"),
            ))
            return {"status": "success" if result.get("success") else "failed",
                    "domain": "browser", "result": result}

        # ── YouTube search ─────────────────────────────────────────────
        elif action_lower in ("youtube_search", "search_youtube"):
            cards = self._run(self._youtube.search(
                params.get("query", ""),
                params.get("language_filter"),
            ))
            return {"status": "success", "domain": "browser",
                    "cards": [{"title": c.title, "channel": c.channel, "views": c.view_count} for c in cards]}

        # ── Navigate ──────────────────────────────────────────────────
        elif action_lower in ("navigate", "browser_navigate", "open_tab"):
            url = params.get("url", "https://www.youtube.com")
            nav = self._run(self._driver.navigate(url))
            return {"status": "success" if nav.success else "failed",
                    "domain": "browser", "url": nav.url, "error": nav.error}

        # ── DOM Snapshot ──────────────────────────────────────────────
        elif action_lower in ("dom_snapshot", "snapshot"):
            page = self._run(self._driver.get_page())
            elements = self._run(self._snapshot.scan(page))
            self._registry.populate(elements)
            return {"status": "success", "domain": "browser",
                    "element_count": len(elements),
                    "elements": [e.to_dict() for e in elements[:20]]}

        # ── Browser click ─────────────────────────────────────────────
        elif action_lower == "browser_click":
            ref = params.get("element_ref", "")
            selector = self._registry.resolve(ref) or ref
            page = self._run(self._driver.get_page())
            try:
                self._run(page.click(selector))
                return {"status": "success", "domain": "browser", "action": "click", "selector": selector}
            except Exception as e:
                return {"status": "failed", "domain": "browser", "error": str(e)}

        # ── Browser fill ──────────────────────────────────────────────
        elif action_lower in ("browser_fill", "fill_form"):
            ref = params.get("element_ref") or params.get("selector", "")
            selector = self._registry.resolve(ref) or ref
            text = params.get("value") or str(params.get("data", ""))
            page = self._run(self._driver.get_page())
            result = self._run(self._multilingual.fill(page, selector, text))
            return {"status": "success" if result.success else "failed",
                    "domain": "browser", "verified": result.text_verified}

        # ── Browser keyboard ──────────────────────────────────────────
        elif action_lower == "browser_key":
            key = params.get("key", "Enter")
            page = self._run(self._driver.get_page())
            try:
                self._run(page.keyboard.press(key))
                return {"status": "success", "domain": "browser", "key": key}
            except Exception as e:
                return {"status": "failed", "domain": "browser", "error": str(e)}

        # ── Browser scroll ────────────────────────────────────────────
        elif action_lower in ("browser_scroll", "scroll_page"):
            page = self._run(self._driver.get_page())
            result = self._run(self._scroll.scroll(
                page,
                direction=params.get("direction", "down"),
                pixels=params.get("pixels", 600),
            ))
            return {"status": "success" if result.success else "failed",
                    "domain": "browser", "scroll_y": result.scroll_y}

        elif action_lower == "browser_scroll_to_element":
            ref = params.get("element_ref", "")
            selector = self._registry.resolve(ref) or ref
            page = self._run(self._driver.get_page())
            result = self._run(self._scroll.scroll_to_element(page, selector))
            return {"status": "success" if result.success else "failed",
                    "domain": "browser", "scroll_y": result.scroll_y}

        # ── Video controls ────────────────────────────────────────────
        elif action_lower in ("video_play",):
            page = self._run(self._driver.get_page())
            res = self._run(self._video.play(page))
            return {"status": "success" if res.success else "failed",
                    "domain": "browser", "video_state": res.video_state, "error": res.error}

        elif action_lower == "video_pause":
            page = self._run(self._driver.get_page())
            res = self._run(self._video.pause(page))
            return {"status": "success" if res.success else "failed",
                    "domain": "browser", "video_state": res.video_state, "error": res.error}

        elif action_lower == "video_seek":
            page = self._run(self._driver.get_page())
            res = self._run(self._video.seek(page, float(params.get("position_seconds", 0))))
            return {"status": "success" if res.success else "failed",
                    "domain": "browser", "video_state": res.video_state}

        elif action_lower == "video_set_speed":
            page = self._run(self._driver.get_page())
            res = self._run(self._video.set_speed(page, float(params.get("rate", 1.0))))
            return {"status": "success" if res.success else "failed",
                    "domain": "browser", "video_state": res.video_state}

        elif action_lower == "video_fullscreen":
            page = self._run(self._driver.get_page())
            res = self._run(self._video.fullscreen(page))
            return {"status": "success" if res.success else "failed", "domain": "browser"}

        elif action_lower == "video_set_language":
            page = self._run(self._driver.get_page())
            res = self._run(self._video.set_language(page, params.get("language", "Telugu")))
            return {"status": "success" if res.success else "failed", "domain": "browser"}

        # ── Screenshot / Verify ───────────────────────────────────────
        elif action_lower == "screenshot":
            page = self._run(self._driver.get_page())
            res = self._run(self._screenshot.capture(page, params.get("label", "screenshot")))
            return {"status": "success" if res.success else "failed",
                    "domain": "browser", "file_path": res.file_path, "data_uri": res.data_uri}

        elif action_lower == "verify_element":
            page = self._run(self._driver.get_page())
            ok = self._run(self._screenshot.verify_element_visible(page, params.get("selector", "")))
            return {"status": "success", "domain": "browser", "visible": ok}

        elif action_lower == "verify_text":
            page = self._run(self._driver.get_page())
            ok = self._run(self._screenshot.verify_text_visible(page, params.get("text", "")))
            return {"status": "success", "domain": "browser", "found": ok}

        # ── Generic search fallback ───────────────────────────────────
        else:
            query = params.get("query", "")
            if query:
                nav = self._run(self._driver.navigate(f"https://www.google.com/search?q={query}"))
                return {"status": "success", "domain": "browser", "url": nav.url}
            return {"status": "success", "domain": "browser", "message": "No action matched"}

    def _legacy_execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy urllib fallback when Playwright is not installed."""
        if action in ("open_tab",):
            tab = self.browser.open_tab(params.get("url", "https://google.com"))
            return {"status": "success", "domain": "browser", "tab_id": tab.tab_id}
        elif action in ("navigate", "browser_navigate"):
            res = self.browser.navigate(params.get("tab_id", "tab_1"), params.get("url", "https://google.com"))
            return {"status": "success" if res.success else "failed", "domain": "browser",
                    "extracted": res.extracted_text, "snapshot": res.dom_snapshot}
        elif action in ("fill_form", "browser_fill"):
            res = self.browser.fill_form(params.get("tab_id", "tab_1"), params.get("data", {}))
            return {"status": "success", "domain": "browser", "result": res}
        elif action in ("browser_click",):
            return {"status": "success", "domain": "browser", "action": "click",
                    "element_ref": params.get("element_ref", ""), "tab_id": params.get("tab_id", "tab_1")}
        else:
            res = self.browser.intelligent_search(params.get("query", ""))
            return {"status": "success", "domain": "browser", "search_results": res}


class SchedulerDomainExecutor:
    """Isolated Domain Executor for Task Scheduling & Timers."""

    def __init__(self, scheduler_module: Optional[TaskSchedulerModule] = None) -> None:
        self.scheduler = scheduler_module or TaskSchedulerModule()

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "schedule_task":
            task = self.scheduler.schedule_task(
                title=params.get("title", "Scheduled Task"),
                cron_expression=params.get("cron"),
                interval_seconds=params.get("interval"),
            )
            return {"status": "success", "domain": "scheduler", "task_id": task.id}
        elif action == "checkpoint":
            cp = self.scheduler.create_checkpoint(params.get("task_id", ""), params.get("step", ""), params.get("snapshot", {}))
            return {"status": "success", "domain": "scheduler", "checkpoint": cp.checkpoint_id if cp else None}
        else:
            self.scheduler.update_progress(params.get("task_id", ""), params.get("progress", 0.0), params.get("message", ""))
            return {"status": "success", "domain": "scheduler", "progress": params.get("progress", 0.0)}


class JarvisDomainExecutor:
    """Domain Executor for Hands-Free Continuous Voice Jarvis Loop Tasks."""

    def __init__(self, jarvis_engine: Optional[ContinuousVoiceJarvisEngine] = None) -> None:
        self.jarvis = jarvis_engine or ContinuousVoiceJarvisEngine()

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "start_continuous_task":
            session = self.jarvis.start_continuous_task(
                goal=params.get("goal", "Continuous Goal"),
                language=params.get("language", "telugu_english")
            )
            return {"status": "success", "domain": "jarvis", "session_id": session.session_id, "subtasks_count": len(session.subtasks)}
        elif action == "execute_next_subtask":
            res = self.jarvis.execute_next_subtask(params.get("session_id", ""))
            return {"status": "success", "domain": "jarvis", "subtask_result": res}
        elif action == "voice_barge_in":
            res = self.jarvis.handle_voice_barge_in(params.get("session_id", ""), params.get("command", ""))
            return {"status": "success", "domain": "jarvis", "barge_in_result": res}
        else:
            return {"status": "success", "domain": "jarvis", "message": "Jarvis engine active"}


class ConversationalDomainExecutor:
    """Fast Path Domain Executor for Direct Natural Language Q&A."""

    def execute(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "success",
            "domain": "conversational",
            "message": params.get("response", "Direct conversational response."),
        }


class CapabilityMap:
    """O(1) Direct Capability Dispatch Hash Map including OpenWork and Jarvis Actions."""

    def __init__(self) -> None:
        self.map: Dict[str, str] = {
            # Desktop capabilities
            "open_app": "desktop",
            "switch_window": "desktop",
            "desktop_switch_window": "desktop",
            "edit_document": "desktop",
            "write_file": "desktop",
            "read_file": "desktop",
            "execute_cli": "desktop",
            "accessibility_click": "desktop",
            "desktop_click": "desktop",
            "desktop_type": "desktop",
            # Browser capabilities
            "open_tab": "browser",
            "navigate": "browser",
            "browser_navigate": "browser",
            "fill_form": "browser",
            "browser_fill": "browser",
            "browser_click": "browser",
            "scrape_web": "browser",
            "search_web": "browser",
            # New Playwright browser capabilities
            "youtube_flow": "browser",
            "youtube_automation": "browser",
            "youtube_search": "browser",
            "search_youtube": "browser",
            "browser_scroll": "browser",
            "browser_scroll_to_element": "browser",
            "browser_key": "browser",
            "video_play": "browser",
            "video_pause": "browser",
            "video_seek": "browser",
            "video_set_speed": "browser",
            "video_fullscreen": "browser",
            "video_set_language": "browser",
            "dom_snapshot": "browser",
            "verify_element": "browser",
            "verify_text": "browser",
            "screenshot": "browser",
            "scroll_page": "browser",
            # Scheduler capabilities
            "schedule_task": "scheduler",
            "add_reminder": "scheduler",
            "checkpoint": "scheduler",
            "update_progress": "scheduler",
            # Continuous Jarvis capabilities
            "start_continuous_task": "jarvis",
            "execute_next_subtask": "jarvis",
            "voice_barge_in": "jarvis",
            "jarvis_continuous_task": "jarvis",
            # Conversational
            "direct_qa": "conversational",
        }

    def get_domain(self, capability: str) -> str:
        return self.map.get(capability.lower(), "conversational")


class FastDomainRouter:
    """
    Fast Router that evaluates intent and routes directly to isolated domain executors,
    bypassing heavy global tool scanning and reducing orchestration latency.
    """

    def __init__(
        self,
        desktop_executor: Optional[DesktopDomainExecutor] = None,
        browser_executor: Optional[BrowserDomainExecutor] = None,
        scheduler_executor: Optional[SchedulerDomainExecutor] = None,
        conversational_executor: Optional[ConversationalDomainExecutor] = None,
        jarvis_executor: Optional[JarvisDomainExecutor] = None,
    ) -> None:
        self.capability_map = CapabilityMap()
        self.openwork_bridge = OpenWorkCapabilityBridge()
        self.jarvis_engine = ContinuousVoiceJarvisEngine(domain_router=self)

        self.executors = {
            "desktop": desktop_executor or DesktopDomainExecutor(),
            "browser": browser_executor or BrowserDomainExecutor(),
            "scheduler": scheduler_executor or SchedulerDomainExecutor(),
            "conversational": conversational_executor or ConversationalDomainExecutor(),
            "jarvis": jarvis_executor or JarvisDomainExecutor(jarvis_engine=self.jarvis_engine),
        }

    def route_and_execute(self, user_input: str, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        domain = self.capability_map.get_domain(action)
        if domain == "conversational":
            input_lower = user_input.lower()
            if re.search(r"\b(window|notepad|file|dir|edit|desktop|click)\b", input_lower):
                domain = "desktop"
            elif re.search(r"\b(youtube|video|watch|play|search.*video|open.*youtube)\b", input_lower):
                domain = "browser"
                if action == "direct_qa":
                    action = "youtube_flow"
                    # Extract query from user input
                    import re as _re
                    query_match = _re.search(r"search(?:\s+for)?\s+(.+?)(?:\s+and|\s+in|\s+filter|$)", input_lower)
                    params["query"] = query_match.group(1) if query_match else user_input
                    if "telugu" in input_lower:
                        params["language_filter"] = "Telugu"
                    if "pause" in input_lower:
                        import re as _re2
                        sec = _re2.search(r"(\d+)\s*(?:second|sec)", input_lower)
                        params["pause_after_seconds"] = float(sec.group(1)) if sec else 30.0
            elif re.search(r"\b(browser|url|scrape|search|tab|website|html)\b", input_lower):
                domain = "browser"
            elif re.search(r"\b(schedule|cron|timer|reminder|recurring)\b", input_lower):
                domain = "scheduler"
            elif re.search(r"\b(jarvis|continuous|multi-step|hands-free|loop)\b", input_lower):
                domain = "jarvis"

        executor = self.executors.get(domain, self.executors["conversational"])
        return executor.execute(action, params)

    def is_simple_command(self, user_input: str) -> bool:
        """Determines if request can bypass multi-step planning and execute directly."""
        input_lower = user_input.strip().lower()
        if len(input_lower.split()) <= 6 and not any(kw in input_lower for kw in ["and then", "after that", "first", "multiple", "complex", "plan"]):
            if re.search(r"\b(what|time|date|hello|hi|open|list|show|dir|read|help|version)\b", input_lower):
                return True
        return False
