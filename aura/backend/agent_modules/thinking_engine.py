from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SubTask:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str = ""
    description: str = ""
    target_tool: Optional[str] = None
    status: str = "pending"  # pending, in_progress, completed, failed, skipped
    result: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class PlanOption:
    name: str
    steps: List[str]
    estimated_risk: float  # 0.0 to 1.0
    estimated_uncertainty: float  # 0.0 to 1.0
    rationale: str


@dataclass
class ThinkingResult:
    goal: str
    subtasks: List[SubTask]
    options: List[PlanOption]
    selected_option: Optional[PlanOption]
    uncertainty_level: float
    requires_replanning: bool = False
    replan_reason: Optional[str] = None


class ThinkingEngine:
    """
    Module: Thinking Engine
    Objectives:
    - Think before acting
    - Break problems into concrete subtasks
    - Generate multiple solution options
    - Estimate uncertainty for each path
    - Select optimal execution approach
    - Replan dynamically when new info or errors occur
    """

    def __init__(self) -> None:
        self.history: List[ThinkingResult] = []

    def decompose_goal(self, goal: str, context: Optional[Dict[str, Any]] = None) -> ThinkingResult:
        context = context or {}

        # Heuristic subtask breakdown based on goal domain analysis
        goal_lower = goal.lower()
        subtasks: List[SubTask] = []
        options: List[PlanOption] = []

        if any(kw in goal_lower for kw in ["search", "web", "browser", "url", "scrape", "site", "online"]):
            subtasks = [
                SubTask(title="Formulate Search Strategy", description="Identify search terms or URL targets", target_tool="browser_search"),
                SubTask(title="Execute Web Action", description="Navigate page, inspect DOM or fetch content", target_tool="browser_automation"),
                SubTask(title="Synthesize Results", description="Extract key facts and verify outcomes", target_tool="reasoning_engine"),
            ]
            options = [
                PlanOption(
                    name="Direct Browser Navigation & Extraction",
                    steps=["Open target browser tab", "Fill required search form / navigate", "Scrape content & verify"],
                    estimated_risk=0.1,
                    estimated_uncertainty=0.2,
                    rationale="Direct interactive web automation with live validation."
                ),
                PlanOption(
                    name="API / Fast Search Fallback",
                    steps=["Execute query against search API", "Parse JSON response"],
                    estimated_risk=0.05,
                    estimated_uncertainty=0.3,
                    rationale="Fast non-interactive fallback if browser DOM is unneeded."
                ),
            ]

        elif any(kw in goal_lower for kw in ["file", "folder", "directory", "edit", "write", "code", "script"]):
            subtasks = [
                SubTask(title="Inspect Target File System", description="Verify file existence and paths", target_tool="desktop_automation"),
                SubTask(title="Perform File Operation", description="Read, edit, or create files cleanly", target_tool="desktop_automation"),
                SubTask(title="Validate Output State", description="Ensure content integrity and run syntax/test checks", target_tool="reasoning_engine"),
            ]
            options = [
                PlanOption(
                    name="Safe Incremental File Edit",
                    steps=["Read existing file", "Apply surgical edits", "Run verification"],
                    estimated_risk=0.15,
                    estimated_uncertainty=0.1,
                    rationale="Minimizes collateral file changes and maintains safety."
                ),
            ]

        elif any(kw in goal_lower for kw in ["schedule", "reminder", "cron", "recurring", "timer"]):
            subtasks = [
                SubTask(title="Parse Timing & Recurrence", description="Extract interval, time, and trigger criteria", target_tool="task_scheduler"),
                SubTask(title="Register Scheduled Job", description="Add recurring task entry with retries and checkpoints", target_tool="task_scheduler"),
                SubTask(title="Configure Progress Notifications", description="Setup progress callbacks for user updates", target_tool="task_scheduler"),
            ]
            options = [
                PlanOption(
                    name="Task Scheduler Registration",
                    steps=["Parse cron / duration", "Add task to scheduler database", "Verify next trigger time"],
                    estimated_risk=0.05,
                    estimated_uncertainty=0.1,
                    rationale="Reliable, persisted scheduling execution."
                ),
            ]

        else:
            subtasks = [
                SubTask(title="Analyze User Intent", description="Deconstruct request into logical requirements", target_tool="reasoning_engine"),
                SubTask(title="Coordinate Tool Layer", description="Select best tool or generate direct answer", target_tool="tool_orchestrator"),
                SubTask(title="Verify Outcome", description="Ensure answer satisfies user goals and format", target_tool="user_understanding"),
            ]
            options = [
                PlanOption(
                    name="Direct Reasoning & Synthesized Response",
                    steps=["Evaluate context", "Formulate clear response", "Present formatted output"],
                    estimated_risk=0.05,
                    estimated_uncertainty=0.15,
                    rationale="Standard conversational reasoning loop."
                ),
            ]

        selected_option = options[0] if options else None
        uncertainty = selected_option.estimated_uncertainty if selected_option else 0.2

        result = ThinkingResult(
            goal=goal,
            subtasks=subtasks,
            options=options,
            selected_option=selected_option,
            uncertainty_level=uncertainty,
        )
        self.history.append(result)
        return result

    def replan(self, current_result: ThinkingResult, new_info: str, error: Optional[str] = None) -> ThinkingResult:
        """
        Dynamically replan when new information appears or an execution step fails.
        """
        updated_subtasks = [st for st in current_result.subtasks]
        if error:
            # Mark failing subtask and inject recovery step
            for st in updated_subtasks:
                if st.status == "in_progress":
                    st.status = "failed"
                    st.error = error

            recovery_subtask = SubTask(
                title=f"Recover from Error: {error[:40]}...",
                description=f"Analyze error '{error}' and switch to alternative execution path.",
                target_tool="tool_orchestrator",
                status="pending"
            )
            updated_subtasks.append(recovery_subtask)

        # Select backup option if available
        new_option = current_result.selected_option
        if current_result.options and len(current_result.options) > 1:
            new_option = current_result.options[1]

        replan_reason_parts = []
        if new_info:
            replan_reason_parts.append(f"New info: {new_info}")
        if error:
            replan_reason_parts.append(f"Execution error: {error}")
        replan_reason_str = "; ".join(replan_reason_parts) if replan_reason_parts else "Replan triggered"

        replanned_result = ThinkingResult(
            goal=current_result.goal,
            subtasks=updated_subtasks,
            options=current_result.options,
            selected_option=new_option,
            uncertainty_level=min(1.0, current_result.uncertainty_level + 0.15),
            requires_replanning=True,
            replan_reason=replan_reason_str,
        )
        self.history.append(replanned_result)
        return replanned_result

    def system_prompt_section(self) -> str:
        return """
# MODULE: THINKING ENGINE
- THINK BEFORE ACTING: Decompose complex user goals into concrete subtasks before taking actions.
- OPTION GENERATION & UNCERTAINTY: Generate alternative approaches, estimate uncertainty/risk for each, and select the optimal execution path.
- DYNAMIC REPLANNING: Continuously observe environment feedback. If a step fails or new information appears, immediately adjust the plan without stalling.
"""
