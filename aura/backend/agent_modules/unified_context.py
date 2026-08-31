from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VoiceState:
    is_active: bool = False
    language_preference: str = "telugu_english"
    audio_buffer_size: int = 0
    last_transcript: str = ""


@dataclass
class BrowserState:
    active_url: str = ""
    active_tab_title: str = ""
    open_tabs_count: int = 0


@dataclass
class DesktopState:
    focused_window_title: str = ""
    active_process_name: str = ""


@dataclass
class UnifiedContextState:
    session_id: str = "default"
    voice: VoiceState = field(default_factory=VoiceState)
    browser: BrowserState = field(default_factory=BrowserState)
    desktop: DesktopState = field(default_factory=DesktopState)
    active_subtasks: List[str] = field(default_factory=list)
    memory_facts: List[Dict[str, Any]] = field(default_factory=list)
    user_preferences: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class UnifiedContextManager:
    """
    Unified Context Manager for World-Class AI OS.
    Voice, Browser Automation, Desktop Actions, Task Queues, and Memory
    all share the EXACT SAME state seamlessly.
    """

    def __init__(self, session_id: str = "default") -> None:
        self.state = UnifiedContextState(session_id=session_id)

    def update_voice_state(self, is_active: bool, transcript: str = "", lang: str = "telugu_english") -> None:
        self.state.voice.is_active = is_active
        self.state.voice.last_transcript = transcript
        self.state.voice.language_preference = lang
        self.state.updated_at = time.time()

    def update_browser_state(self, url: str, tab_title: str = "", tabs_count: int = 1) -> None:
        self.state.browser.active_url = url
        self.state.browser.active_tab_title = tab_title
        self.state.browser.open_tabs_count = tabs_count
        self.state.updated_at = time.time()

    def update_desktop_state(self, window_title: str, process_name: str = "") -> None:
        self.state.desktop.focused_window_title = window_title
        self.state.desktop.active_process_name = process_name
        self.state.updated_at = time.time()

    def sync_memory_facts(self, facts: List[Dict[str, Any]]) -> None:
        self.state.memory_facts = facts
        self.state.updated_at = time.time()

    def get_unified_snapshot(self) -> Dict[str, Any]:
        return {
            "session_id": self.state.session_id,
            "voice_active": self.state.voice.is_active,
            "browser_url": self.state.browser.active_url,
            "desktop_window": self.state.desktop.focused_window_title,
            "subtask_count": len(self.state.active_subtasks),
            "memory_fact_count": len(self.state.memory_facts),
            "last_updated": self.state.updated_at,
        }


class ModuleCostManager:
    """
    Module Cost Justifier.
    Evaluates request complexity score (0.0 to 1.0) and ensures heavy modules
    (Deep Reasoning Engine, Full Memory Scans, Multi-Tool Planners) activate
    ONLY when explicitly justified by request complexity.
    """

    HEAVY_MODULE_COSTS = {
        "DeepReasoningEngine": 0.85,
        "FullMemoryTableScan": 0.70,
        "MultiToolSubtaskPlanner": 0.60,
        "BrowserDOMScraper": 0.40,
        "FastLaneGate": 0.05,
    }

    @classmethod
    def calculate_request_complexity(cls, user_input: str) -> float:
        input_lower = user_input.strip().lower()
        if len(input_lower) < 15 and not any(kw in input_lower for kw in ["and then", "first", "analyze"]):
            return 0.1  # Trivial / Fast lane command

        score = 0.3
        if "and then" in input_lower or "after that" in input_lower:
            score += 0.3
        if "delete" in input_lower or "wipe" in input_lower or "drop" in input_lower:
            score += 0.4
        if len(input_lower.split()) > 20:
            score += 0.2
        return min(score, 1.0)

    @classmethod
    def is_module_activation_justified(cls, module_name: str, user_input: str) -> bool:
        complexity = cls.calculate_request_complexity(user_input)
        cost_threshold = cls.HEAVY_MODULE_COSTS.get(module_name, 0.1)
        return complexity >= cost_threshold
