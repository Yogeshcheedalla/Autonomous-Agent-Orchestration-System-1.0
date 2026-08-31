from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProjectContext:
    project_name: str
    root_path: str
    active_branch: str = "main"
    architecture_notes: List[str] = field(default_factory=list)
    key_files: List[str] = field(default_factory=list)


@dataclass
class TaskState:
    task_id: str
    goal: str
    current_step: int = 0
    total_steps: int = 1
    state_variables: Dict[str, Any] = field(default_factory=dict)
    is_completed: bool = False
    updated_at: float = field(default_factory=time.time)


class MemoryModule:
    """
    Module: Memory
    Objectives:
    - Retain relevant project info and active task state across execution turns
    - Store short-term conversational context and long-term user memories
    - Provide concise memory string representation for system prompt injection
    """

    def __init__(self) -> None:
        self.project_context: Optional[ProjectContext] = None
        self.active_tasks: Dict[str, TaskState] = {}
        self.memories: List[Dict[str, str]] = []

    def set_project_context(self, name: str, root_path: str, key_files: Optional[List[str]] = None) -> ProjectContext:
        self.project_context = ProjectContext(
            project_name=name,
            root_path=root_path,
            key_files=key_files or [],
        )
        return self.project_context

    def update_task_state(self, task_id: str, goal: str, step: int, total_steps: int, variables: Optional[Dict[str, Any]] = None) -> TaskState:
        tstate = self.active_tasks.get(task_id) or TaskState(task_id=task_id, goal=goal)
        tstate.current_step = step
        tstate.total_steps = total_steps
        if variables:
            tstate.state_variables.update(variables)
        tstate.is_completed = (step >= total_steps)
        tstate.updated_at = time.time()
        self.active_tasks[task_id] = tstate
        return tstate

    def add_memory_fact(self, topic: str, insight: str, category: str = "user_preference") -> Dict[str, str]:
        fact = {"topic": topic, "insight": insight, "category": category}
        self.memories.append(fact)
        return fact

    def get_memory_prompt_summary(self) -> str:
        parts = []
        if self.project_context:
            parts.append(f"Active Project: {self.project_context.project_name} ({self.project_context.root_path})")

        if self.active_tasks:
            active_str = "; ".join(
                f"{t.goal} (Step {t.current_step}/{t.total_steps})"
                for t in self.active_tasks.values() if not t.is_completed
            )
            if active_str:
                parts.append(f"Active Tasks: {active_str}")

        if self.memories:
            facts_str = "; ".join(f"{m['topic']}: {m['insight']}" for m in self.memories[-5:])
            parts.append(f"Retained Facts: {facts_str}")

        return "\n".join(parts) if parts else "No active task or project memory stored."

    def system_prompt_section(self) -> str:
        return f"""
# MODULE: MEMORY ENGINE
- STATE & PROJECT RETENTION: Maintain active task state, step counters, and project context across multi-step automations.
- CONTEXT INTEGRATION:
{self.get_memory_prompt_summary()}
"""
