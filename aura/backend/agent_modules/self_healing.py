from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class RecoveryAttempt:
    module: str
    action: str
    original_error: str
    fallback_strategy: str
    success: bool
    timestamp: float = field(default_factory=time.time)


class SelfHealingRecoveryModule:
    """
    Self-Healing Recovery Module.
    Manages resilient retries and automatic recovery from selector shifts,
    lost window focus, or API timeouts cleanly without crashing or aborting user workflows.
    """

    def __init__(self, max_recovery_attempts: int = 3) -> None:
        self.max_attempts = max_recovery_attempts
        self.recovery_log: List[RecoveryAttempt] = []

    def execute_with_self_healing(
        self,
        module_name: str,
        action_name: str,
        action_func: Callable[[], Any],
        fallback_func: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        attempts = 0
        last_error = ""

        while attempts < self.max_attempts:
            attempts += 1
            try:
                result = action_func()
                if attempts > 1:
                    rec = RecoveryAttempt(
                        module=module_name,
                        action=action_name,
                        original_error=last_error,
                        fallback_strategy=f"Retry attempt {attempts}",
                        success=True,
                    )
                    self.recovery_log.append(rec)
                return {"status": "success", "result": result, "recovery_attempts": attempts - 1}
            except Exception as e:
                last_error = str(e)
                time.sleep(0.05 * (2 ** (attempts - 1)))  # Exponential backoff

        # Primary retries exhausted: execute fallback if provided
        if fallback_func:
            try:
                fallback_res = fallback_func()
                rec = RecoveryAttempt(
                    module=module_name,
                    action=action_name,
                    original_error=last_error,
                    fallback_strategy="Executed resilient fallback handler",
                    success=True,
                )
                self.recovery_log.append(rec)
                return {"status": "success_via_fallback", "result": fallback_res, "recovery_attempts": attempts}
            except Exception as fe:
                last_error = f"Primary error: {last_error} | Fallback error: {str(fe)}"

        rec = RecoveryAttempt(
            module=module_name,
            action=action_name,
            original_error=last_error,
            fallback_strategy="Exhausted all recovery options",
            success=False,
        )
        self.recovery_log.append(rec)
        return {"status": "failed", "error": last_error, "recovery_attempts": attempts}
