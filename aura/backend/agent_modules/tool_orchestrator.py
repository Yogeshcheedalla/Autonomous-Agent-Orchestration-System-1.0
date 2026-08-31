from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RiskAssessment:
    action_type: str
    is_reversible: bool
    risk_score: float  # 0.0 (safe) to 1.0 (critical/destructive)
    requires_user_confirmation: bool
    safety_notes: str


@dataclass
class ToolAction:
    tool_name: str
    action_name: str
    parameters: Dict[str, Any]
    risk: RiskAssessment


class ToolOrchestrator:
    """
    Module: Tool Orchestrator
    Objectives:
    - Select the optimal tool (Browser, Desktop, CLI, API) based on request intent
    - Coordinate tool execution reliably in multi-step automation pipelines
    - Perform risk evaluation prior to action execution (prefer reversible actions)
    - Require user confirmation for high-risk or irreversible destructive operations
    """

    DESTRUCTIVE_KEYWORDS = {"rm", "del", "delete", "drop", "overwrite", "format", "shutdown", "reboot", "wipe", "force"}

    def __init__(self) -> None:
        self.registered_tools: Dict[str, List[str]] = {
            "browser": ["open_tab", "navigate", "fill_form", "scrape_content", "search_web", "close_tab"],
            "desktop": ["switch_window", "list_directory", "read_file", "edit_document", "accessibility_click"],
            "cli": ["execute_command", "run_script", "git_status"],
            "api": ["http_request", "webhook_trigger", "json_api_call"],
            "scheduler": ["schedule_task", "cancel_schedule", "list_scheduled_tasks"],
        }
        self.execution_log: List[ToolAction] = []

    def evaluate_action_risk(self, tool_name: str, action_name: str, parameters: Dict[str, Any]) -> RiskAssessment:
        param_str = str(parameters).lower()
        is_destructive = any(kw in param_str for kw in self.DESTRUCTIVE_KEYWORDS) or "delete" in action_name or "overwrite" in action_name

        if tool_name == "cli" and is_destructive:
            return RiskAssessment(
                action_type=f"{tool_name}:{action_name}",
                is_reversible=False,
                risk_score=0.9,
                requires_user_confirmation=True,
                safety_notes="Destructive CLI command execution detected.",
            )
        elif tool_name == "desktop" and is_destructive:
            return RiskAssessment(
                action_type=f"{tool_name}:{action_name}",
                is_reversible=False,
                risk_score=0.75,
                requires_user_confirmation=True,
                safety_notes="File modification/deletion requires confirmation.",
            )
        elif tool_name == "browser":
            return RiskAssessment(
                action_type=f"{tool_name}:{action_name}",
                is_reversible=True,
                risk_score=0.1,
                requires_user_confirmation=False,
                safety_notes="Standard web interaction (reversible).",
            )
        else:
            return RiskAssessment(
                action_type=f"{tool_name}:{action_name}",
                is_reversible=True,
                risk_score=0.2,
                requires_user_confirmation=False,
                safety_notes="Safe automation action.",
            )

    def select_tool_for_intent(self, intent: str) -> str:
        intent_lower = intent.lower()
        if re.search(r"\b(schedule|scheduler|cron|timer|recurring|reminder|job)\b", intent_lower):
            return "scheduler"
        elif re.search(r"\b(cmd|cli|shell|terminal|command|bash|powershell|script)\b", intent_lower):
            return "cli"
        elif re.search(r"\b(browser|web|url|scrape|search|tab|website|online)\b", intent_lower):
            return "browser"
        elif re.search(r"\b(window|app|file|document|desktop|pywinauto|folder)\b", intent_lower):
            return "desktop"
        else:
            return "api"

    def orchestrate_action(self, tool_name: str, action_name: str, parameters: Dict[str, Any]) -> ToolAction:
        risk = self.evaluate_action_risk(tool_name, action_name, parameters)
        action = ToolAction(
            tool_name=tool_name,
            action_name=action_name,
            parameters=parameters,
            risk=risk,
        )
        self.execution_log.append(action)
        return action

    def system_prompt_section(self) -> str:
        return """
# MODULE: TOOL ORCHESTRATOR
- RELIABLE TOOL SELECTION: Select the correct tool modality (Browser, Desktop, CLI, API, Task Scheduler) for each subtask.
- RISK-AWARE EXECUTION: Prefer reversible actions. Before executing high-risk or irreversible destructive operations (file deletion, formatting, system modifications), pause and request explicit user confirmation.
- MULTI-TOOL COORDINATION: Seamlessly pipe output from one tool engine into another (e.g. Browser search output -> Desktop document editor -> Task Scheduler notification).
"""
