from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Checkpoint:
    checkpoint_id: str
    step_name: str
    state_snapshot: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class ScheduledTask:
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    title: str = ""
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    is_recurring: bool = False
    max_retries: int = 3
    retry_count: int = 0
    status: str = "scheduled"  # scheduled, running, completed, failed, paused
    next_run_at: float = field(default_factory=time.time)
    checkpoints: List[Checkpoint] = field(default_factory=list)
    progress_percent: float = 0.0
    last_notification: Optional[str] = None


class TaskSchedulerModule:
    """
    Module: Task Scheduler
    Objectives:
    - Support recurring tasks (cron or time interval)
    - Handle retries with backoff and state checkpoints
    - Track task progress and emit progress notifications
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, ScheduledTask] = {}
        self.notifications_log: List[Dict[str, Any]] = []

    def schedule_task(
        self,
        title: str,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        max_retries: int = 3,
    ) -> ScheduledTask:
        is_recurring = (cron_expression is not None) or (interval_seconds is not None and interval_seconds > 0)
        task = ScheduledTask(
            title=title,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            is_recurring=is_recurring,
            max_retries=max_retries,
            next_run_at=time.time() + (interval_seconds or 0),
        )
        self.tasks[task.id] = task
        return task

    def create_checkpoint(self, task_id: str, step_name: str, state_snapshot: Dict[str, Any]) -> Optional[Checkpoint]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        cp = Checkpoint(
            checkpoint_id=f"cp_{len(task.checkpoints)+1}",
            step_name=step_name,
            state_snapshot=state_snapshot,
        )
        task.checkpoints.append(cp)
        return cp

    def update_progress(self, task_id: str, progress_percent: float, status_message: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        task.progress_percent = min(100.0, max(0.0, progress_percent))
        task.last_notification = status_message
        self.notifications_log.append({
            "task_id": task_id,
            "title": task.title,
            "progress": task.progress_percent,
            "message": status_message,
            "timestamp": time.time(),
        })

    def handle_task_failure(self, task_id: str, error_message: str) -> bool:
        """Handle task retry logic with checkpoint restoration capability. Returns True if retry scheduled."""
        task = self.tasks.get(task_id)
        if not task:
            return False

        task.retry_count += 1
        if task.retry_count <= task.max_retries:
            task.status = "scheduled"
            # Exponential backoff: 2^retry_count seconds
            task.next_run_at = time.time() + (2 ** task.retry_count)
            self.update_progress(
                task_id,
                task.progress_percent,
                f"Retrying task (attempt {task.retry_count}/{task.max_retries}) after error: {error_message}"
            )
            return True
        else:
            task.status = "failed"
            self.update_progress(task_id, task.progress_percent, f"Task failed after {task.max_retries} retries: {error_message}")
            return False

    def system_prompt_section(self) -> str:
        return """
# MODULE: TASK SCHEDULER
- RECURRING TASKS & CRON: Support scheduled execution of recurring tasks, timers, and periodic automation.
- RETRIES & CHECKPOINTS: Maintain state checkpoints during multi-step runs. On failure, execute automatic retries with state restoration.
- PROGRESS NOTIFICATIONS: Communicate progress clearly to the user with progress metrics and milestone notifications.
"""
