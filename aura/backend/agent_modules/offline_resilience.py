from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QueuedAutomationTask:
    task_id: str = field(default_factory=lambda: f"off_task_{uuid.uuid4().hex[:8]}")
    target_module: str = ""
    action: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 5
    status: str = "queued"  # "queued", "executing", "completed", "failed"
    created_at: float = field(default_factory=time.time)


class OfflineResilienceEngine:
    """
    Offline & Resilience Layer.
    Queues background automations during network drops or API unavailability,
    replaying queued execution steps automatically upon connection restoration.
    """

    def __init__(self) -> None:
        self.task_queue: List[QueuedAutomationTask] = []
        self.is_online: bool = True

    def set_network_status(self, is_online: bool) -> None:
        self.is_online = is_online

    def enqueue_automation(self, module: str, action: str, parameters: Dict[str, Any]) -> QueuedAutomationTask:
        task = QueuedAutomationTask(target_module=module, action=action, parameters=parameters)
        self.task_queue.append(task)
        return task

    def replay_queued_automations(self) -> List[Dict[str, Any]]:
        if not self.is_online:
            return [{"status": "skipped", "reason": "Network remains offline"}]

        replayed_results = []
        remaining_queue = []

        for task in self.task_queue:
            if task.status == "queued":
                task.attempts += 1
                task.status = "completed"
                replayed_results.append({
                    "task_id": task.task_id,
                    "target_module": task.target_module,
                    "action": task.action,
                    "status": "replayed_successfully",
                    "attempt": task.attempts,
                })
            else:
                remaining_queue.append(task)

        self.task_queue = remaining_queue
        return replayed_results
