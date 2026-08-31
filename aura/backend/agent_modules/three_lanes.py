from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from .domain_executors import FastDomainRouter
from .reasoning_engine import ReasoningEngine
from .thinking_engine import ThinkingEngine

logger = logging.getLogger("akansha.runtime")


@dataclass
class LaneExecutionResult:
    lane: str  # "FAST", "STANDARD", "REASONING"
    executor: str
    action: str
    result: Dict[str, Any]
    bypassed_modules: List[str]
    active_modules: List[str]
    latency_ms: float = 0.0


class ModuleResponsibilityMap:
    """
    Module Responsibility Map:
    Defines which modules are active vs safely BYPASSED for each execution lane.
    """

    RESPONSIBILITY_MATRIX = {
        "FAST": {
            "active": ["FastLaneGate", "OSDomainExecutor"],
            "bypassed": ["ThinkingEngine", "ReasoningEngine", "MemoryAnalysis", "SelectiveRiskVerifier", "ParallelTaskRunner"],
        },
        "STANDARD": {
            "active": ["StandardLaneClassifier", "DomainExecutor", "RelevantMemoryRetriever"],
            "bypassed": ["ThinkingEngine", "ReasoningEngine", "ParallelTaskRunner"],
        },
        "REASONING": {
            "active": ["ThinkingEngine", "ReasoningEngine", "SelectiveRiskVerifier", "ParallelTaskRunner", "RelevantMemoryRetriever"],
            "bypassed": [],
        },
    }

    @classmethod
    def get_bypass_map(cls, lane: str) -> Dict[str, List[str]]:
        return cls.RESPONSIBILITY_MATRIX.get(lane.upper(), cls.RESPONSIBILITY_MATRIX["STANDARD"])


class IntentExecutorTable:
    """
    Intent-to-Executor Table:
    Maps intent categories directly to a single domain executor without scanning.
    """

    INTENT_MAP = {
        "open_app": "DesktopDomainExecutor",
        "set_volume": "DesktopDomainExecutor",
        "media_control": "DesktopDomainExecutor",
        "get_time": "ConversationalDomainExecutor",
        "file_op": "DesktopDomainExecutor",
        "web_search": "BrowserDomainExecutor",
        "web_scrape": "BrowserDomainExecutor",
        "schedule_job": "SchedulerDomainExecutor",
        "direct_qa": "ConversationalDomainExecutor",
    }

    @classmethod
    def get_executor(cls, intent: str) -> str:
        return cls.INTENT_MAP.get(intent.lower(), "ConversationalDomainExecutor")


class FastLane:
    """
    Fast Lane:
    Executes basic OS/app actions instantly without planning or memory overhead.
    """

    FAST_PATTERNS = [
        (r"^\s*(?:open|launch|run|open करो|खोलो|ఓపెన్|లాంచ్)\s+(spotify|chrome|notepad|vscode|calculator|discord|slack|browser)\b", "open_app"),
        (r"^\s*(?:volume|vol|వాల్యూమ్|वॉल्यूम)\s+(\d+%\b|\d+\b)", "set_volume"),
        (r"^\s*(?:mute|unmute|pause|play|stop|next|previous|పాజ్|ప్లే|ఆపు)\b", "media_control"),
        (r"^\s*(?:what\s+time\s+is\s+it|time|date|today|సమయం\s+ఎంత|టైమ్\s+ఎంత|समय\s+क्या\s+हुआ)\b", "get_time"),
    ]

    def try_execute(self, user_input: str) -> Optional[Dict[str, Any]]:
        input_lower = user_input.strip().lower()
        for pattern, intent in self.FAST_PATTERNS:
            match = re.search(pattern, input_lower, re.IGNORECASE)
            if match:
                target = match.group(1) if match.groups() else ""
                executor = IntentExecutorTable.get_executor(intent)
                return {
                    "intent": intent,
                    "target": target,
                    "executor": executor,
                    "message": f"Fast Lane direct OS execution ({intent}) for target '{target}'.",
                }
        return None


class StandardLane:
    """
    Standard Lane:
    Uses lightweight classification to dispatch Q&A and single-domain tasks directly to one executor.
    """

    def __init__(self, router: Optional[FastDomainRouter] = None) -> None:
        self.router = router or FastDomainRouter()

    def execute(self, user_input: str) -> Dict[str, Any]:
        input_lower = user_input.lower()
        if re.search(r"\b(window|notepad|file|dir|edit|desktop|click)\b", input_lower):
            intent = "file_op"
        elif re.search(r"\b(browser|url|scrape|search|tab|website)\b", input_lower):
            intent = "web_search"
        elif re.search(r"\b(schedule|cron|timer|reminder)\b", input_lower):
            intent = "schedule_job"
        else:
            intent = "direct_qa"

        executor = IntentExecutorTable.get_executor(intent)
        res = self.router.route_and_execute(user_input, "direct_qa", {"response": "Standard Lane execution."})
        return {
            "intent": intent,
            "executor": executor,
            "result": res,
        }


class ReasoningLane:
    """
    Reasoning Lane:
    Activated ONLY for genuine multi-step requests requiring subtask decomposition and deep tradeoff reasoning.
    """

    def __init__(
        self,
        thinking_engine: Optional[ThinkingEngine] = None,
        reasoning_engine: Optional[ReasoningEngine] = None,
    ) -> None:
        self.thinking = thinking_engine or ThinkingEngine()
        self.reasoning = reasoning_engine or ReasoningEngine()

    def execute(self, user_input: str) -> Dict[str, Any]:
        thinking_res = self.thinking.decompose_goal(user_input)
        reasoning_res = self.reasoning.analyze_decision(user_input, {}, [], is_destructive_action=False)
        return {
            "intent": "multi_step_reasoning",
            "executor": "ReasoningEngine",
            "subtasks": [st.title for st in thinking_res.subtasks],
            "decision": reasoning_res.decision,
        }


class ThreeLaneExecutionEngine:
    """
    Decoupled 3-Lane Execution Engine.
    Routes requests through Fast Lane -> Standard Lane -> Reasoning Lane.
    Offloads background services asynchronously and logs runtime execution paths.
    """

    def __init__(self) -> None:
        self.fast_lane = FastLane()
        self.standard_lane = StandardLane()
        self.reasoning_lane = ReasoningLane()

    def is_genuine_multistep(self, user_input: str) -> bool:
        """Audited planner trigger check: activates ONLY for genuine multi-step requests."""
        input_lower = user_input.strip().lower()
        has_multistep_kw = any(kw in input_lower for kw in ["and then", "after that", "first ", "then ", "multiple", "complex plan", "steps"])
        has_multiple_actions = len(re.findall(r"\b(open|search|write|delete|schedule|edit)\b", input_lower)) >= 2
        return has_multistep_kw or has_multiple_actions

    def process_request(self, user_input: str) -> LaneExecutionResult:
        start_time = time.time()

        # 1. Fast Lane Evaluation
        fast_res = self.fast_lane.try_execute(user_input)
        if fast_res:
            latency = (time.time() - start_time) * 1000
            resp_map = ModuleResponsibilityMap.get_bypass_map("FAST")
            log_str = f"[LANE: FAST | EXECUTOR: {fast_res['executor']} | BYPASSED: {', '.join(resp_map['bypassed'])}]"
            logger.info(log_str)
            print(log_str)
            return LaneExecutionResult(
                lane="FAST",
                executor=fast_res["executor"],
                action=fast_res["intent"],
                result=fast_res,
                bypassed_modules=resp_map["bypassed"],
                active_modules=resp_map["active"],
                latency_ms=latency,
            )

        # 2. Planner Trigger Audit: Check if request is genuine multi-step for Reasoning Lane
        if self.is_genuine_multistep(user_input):
            reasoning_res = self.reasoning_lane.execute(user_input)
            latency = (time.time() - start_time) * 1000
            resp_map = ModuleResponsibilityMap.get_bypass_map("REASONING")
            log_str = f"[LANE: REASONING | EXECUTOR: ReasoningEngine | ACTIVE: {', '.join(resp_map['active'])}]"
            logger.info(log_str)
            print(log_str)
            return LaneExecutionResult(
                lane="REASONING",
                executor="ReasoningEngine",
                action="multi_step_planning",
                result=reasoning_res,
                bypassed_modules=resp_map["bypassed"],
                active_modules=resp_map["active"],
                latency_ms=latency,
            )

        # 3. Standard Lane Fallback (Single Domain Direct Dispatch)
        standard_res = self.standard_lane.execute(user_input)
        latency = (time.time() - start_time) * 1000
        resp_map = ModuleResponsibilityMap.get_bypass_map("STANDARD")
        log_str = f"[LANE: STANDARD | EXECUTOR: {standard_res['executor']} | BYPASSED: {', '.join(resp_map['bypassed'])}]"
        logger.info(log_str)
        print(log_str)
        return LaneExecutionResult(
            lane="STANDARD",
            executor=standard_res["executor"],
            action=standard_res["intent"],
            result=standard_res,
            bypassed_modules=resp_map["bypassed"],
            active_modules=resp_map["active"],
            latency_ms=latency,
        )

    def dispatch_background_services_async(self, user_input: str, result: LaneExecutionResult) -> None:
        """Offload background habit recording, memory logging, and audit tracking asynchronously."""
        async def _bg_task():
            await asyncio.sleep(0.01)  # Non-blocking yield
            # Simulates background habit & memory indexing
            pass

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_bg_task())
        except Exception:
            pass
