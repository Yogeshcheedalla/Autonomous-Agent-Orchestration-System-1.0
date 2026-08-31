from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Tuple
from .thinking_engine import SubTask, ThinkingResult


class ParallelTaskRunner:
    """
    Parallel Task Runner.
    Identifies independent subtasks in a ThinkingResult and executes them concurrently
    via asyncio.gather to minimize latency in multi-step workflows.
    """

    async def execute_subtasks_parallel(
        self,
        subtasks: List[SubTask],
        executor_func: Callable[[SubTask], Any],
    ) -> List[Tuple[SubTask, Any]]:
        if not subtasks:
            return []

        async def run_single(st: SubTask) -> Tuple[SubTask, Any]:
            st.status = "in_progress"
            try:
                if asyncio.iscoroutinefunction(executor_func):
                    res = await executor_func(st)
                else:
                    res = executor_func(st)
                st.status = "completed"
                st.result = res
                return st, res
            except Exception as e:
                st.status = "failed"
                st.error = str(e)
                return st, None

        tasks = [run_single(st) for st in subtasks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    def group_independent_subtasks(self, subtasks: List[SubTask]) -> List[List[SubTask]]:
        """
        Groups subtasks that have no sequential dependencies together for parallel execution.
        Subtasks with different target tools or independent inputs run in the same phase.
        """
        groups: List[List[SubTask]] = []
        current_group: List[SubTask] = []
        seen_tools = set()

        for st in subtasks:
            tool = st.target_tool or "default"
            if tool in seen_tools:
                # Dependency or tool conflict: start new phase
                if current_group:
                    groups.append(current_group)
                current_group = [st]
                seen_tools = {tool}
            else:
                current_group.append(st)
                seen_tools.add(tool)

        if current_group:
            groups.append(current_group)
        return groups
