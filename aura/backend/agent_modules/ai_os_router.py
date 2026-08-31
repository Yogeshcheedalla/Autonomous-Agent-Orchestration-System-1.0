from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from .ai_os_core import ContinuousSessionState
from .domain_executors import FastDomainRouter


@dataclass
class StateAwareRouteResult:
    routed_domain: str
    is_workflow_continuation: bool
    target_action: str
    parameters: Dict[str, Any]
    reasoning_bypassed: bool


class StateAwareIntentRouter:
    """
    State-Aware Intent Router.
    Routes user intents using active session state context.
    Prevents restarting reasoning from scratch when an active workflow or multi-turn interaction is underway.
    """

    def __init__(
        self,
        domain_router: Optional[FastDomainRouter] = None,
        session_state: Optional[ContinuousSessionState] = None,
    ) -> None:
        self.router = domain_router or FastDomainRouter()
        self.session = session_state or ContinuousSessionState()

    def route_turn(self, user_input: str) -> StateAwareRouteResult:
        input_lower = user_input.strip().lower()

        # 1. Active Workflow Continuation Check
        if self.session.active_workflow and self.session.active_workflow.is_active:
            active_st = self.session.get_active_subtask()
            if active_st:
                target_domain = self.router.capability_map.get_domain(active_st.target_tool or "direct_qa")
                return StateAwareRouteResult(
                    routed_domain=target_domain,
                    is_workflow_continuation=True,
                    target_action=active_st.target_tool or "continue_subtask",
                    parameters={"subtask_id": active_st.id, "input": user_input},
                    reasoning_bypassed=True,
                )

        # 2. Fast Lane OS / App Action Check
        if self.router.is_simple_command(user_input):
            if re.search(r"\b(window|notepad|file|dir|edit|desktop)\b", input_lower):
                domain = "desktop"
                action = "desktop_action"
            elif re.search(r"\b(browser|url|scrape|search|tab|website)\b", input_lower):
                domain = "browser"
                action = "browser_action"
            elif re.search(r"\b(schedule|cron|timer|reminder)\b", input_lower):
                domain = "scheduler"
                action = "schedule_action"
            else:
                domain = "conversational"
                action = "direct_qa"

            return StateAwareRouteResult(
                routed_domain=domain,
                is_workflow_continuation=False,
                target_action=action,
                parameters={"input": user_input},
                reasoning_bypassed=True,
            )

        # 3. New Workflow / Multi-Step Intent
        return StateAwareRouteResult(
            routed_domain="reasoning",
            is_workflow_continuation=False,
            target_action="plan_new_workflow",
            parameters={"input": user_input},
            reasoning_bypassed=False,
        )
