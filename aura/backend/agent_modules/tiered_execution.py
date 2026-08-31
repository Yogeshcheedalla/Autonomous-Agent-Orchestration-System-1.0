from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from .domain_executors import FastDomainRouter
from .reasoning_engine import ReasoningEngine
from .thinking_engine import ThinkingEngine


@dataclass
class ExecutionResult:
    tier: int  # 0, 1, 2, 3
    action: str
    result: Dict[str, Any]
    execution_time_ms: float = 0.0
    bypassed_planner: bool = False


class Tier0FastGate:
    """
    Tier 0: Fast Intent Gate.
    Direct regex and pattern matching for instant, zero-latency execution of direct commands
    (e.g., "Open Spotify", "Volume 50%", "Launch Chrome", "Pause", "Mute", "Play")
    without initiating classifier or planner overhead.
    """

    DIRECT_PATTERNS = [
        (r"^\s*(?:open|launch|run)\s+(spotify|chrome|notepad|vscode|calculator|discord|slack|browser)\b", "open_app"),
        (r"^\s*(?:volume|vol)\s+(\d+%\b|\d+\b)", "set_volume"),
        (r"^\s*(?:mute|unmute|pause|play|stop|next|previous)\b", "media_control"),
        (r"^\s*(?:what\s+time\s+is\s+it|time|date|today)\b", "get_time"),
    ]

    def try_fast_execution(self, user_input: str) -> Optional[Dict[str, Any]]:
        input_lower = user_input.strip().lower()
        for pattern, action_type in self.DIRECT_PATTERNS:
            match = re.search(pattern, input_lower, re.IGNORECASE)
            if match:
                extracted = match.group(1) if match.groups() else ""
                return {
                    "matched": True,
                    "action_type": action_type,
                    "target": extracted,
                    "tier": 0,
                    "message": f"Directly executed Tier 0 fast gate action: '{action_type}' for target '{extracted}'.",
                }
        return None


class Tier1Classifier:
    """
    Tier 1: Lightweight Classifier.
    Determines category and dispatches directly to domain executors without invoking planner loops.
    """

    def __init__(self, router: Optional[FastDomainRouter] = None) -> None:
        self.router = router or FastDomainRouter()

    def try_tier1_execution(self, user_input: str) -> Optional[Dict[str, Any]]:
        if self.router.is_simple_command(user_input):
            res = self.router.route_and_execute(user_input, "direct_qa", {"response": "Direct execution via Tier 1 domain executor."})
            res["tier"] = 1
            res["bypassed_planner"] = True
            return res
        return None


class Tier2DomainPlanner:
    """
    Tier 2: Domain Planner.
    Activated ONLY for multi-step tasks requiring subtask breakdown and option generation.
    """

    def __init__(self, thinking_engine: Optional[ThinkingEngine] = None) -> None:
        self.thinking = thinking_engine or ThinkingEngine()

    def plan_and_execute(self, user_input: str) -> Dict[str, Any]:
        thinking_res = self.thinking.decompose_goal(user_input)
        return {
            "tier": 2,
            "goal": user_input,
            "subtasks_count": len(thinking_res.subtasks),
            "selected_option": thinking_res.selected_option.name if thinking_res.selected_option else "Default",
            "uncertainty": thinking_res.uncertainty_level,
        }


class Tier3DeepReasoning:
    """
    Tier 3: Deep Reasoning Engine.
    Activated ONLY for complex, ambiguous, or high-risk requests requiring tradeoff evaluation.
    """

    def __init__(self, reasoning_engine: Optional[ReasoningEngine] = None) -> None:
        self.reasoning = reasoning_engine or ReasoningEngine()

    def reason_and_execute(self, user_input: str, critical_params: Optional[List[str]] = None) -> Dict[str, Any]:
        critical_params = critical_params or []
        res = self.reasoning.analyze_decision(
            goal=user_input,
            available_info={},
            critical_parameters=critical_params,
            is_destructive_action=True,
        )
        return {
            "tier": 3,
            "decision": res.decision,
            "confidence": res.confidence,
            "requires_clarification": res.requires_clarification,
            "question": res.clarification_question,
        }


class TieredExecutionEngine:
    """
    Hierarchical 4-Tier Execution Engine.
    Routes requests through Tier 0 -> Tier 1 -> Tier 2 -> Tier 3.
    Stops at the lowest effective tier to guarantee minimal latency.
    """

    def __init__(self) -> None:
        self.tier0 = Tier0FastGate()
        self.tier1 = Tier1Classifier()
        self.tier2 = Tier2DomainPlanner()
        self.tier3 = Tier3DeepReasoning()

    def execute_request(self, user_input: str, force_tier: Optional[int] = None) -> ExecutionResult:
        if force_tier == 0 or (force_tier is None and (t0_res := self.tier0.try_fast_execution(user_input))):
            t0_res = t0_res or {"matched": True, "action_type": "fast_override", "tier": 0}
            return ExecutionResult(tier=0, action=t0_res.get("action_type", "fast_gate"), result=t0_res, bypassed_planner=True)

        if force_tier == 1 or (force_tier is None and (t1_res := self.tier1.try_tier1_execution(user_input))):
            t1_res = t1_res or {"tier": 1}
            return ExecutionResult(tier=1, action="domain_dispatch", result=t1_res, bypassed_planner=True)

        if force_tier == 2 or (force_tier is None and not any(kw in user_input.lower() for kw in ["delete", "remove", "wipe", "format", "risk", "critical"])):
            t2_res = self.tier2.plan_and_execute(user_input)
            return ExecutionResult(tier=2, action="subtask_planning", result=t2_res, bypassed_planner=False)

        t3_res = self.tier3.reason_and_execute(user_input)
        return ExecutionResult(tier=3, action="deep_reasoning", result=t3_res, bypassed_planner=False)
