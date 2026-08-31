"""
SessionContextBubble — Per-session isolated context store for Akansha AI OS.
Created for every ContinuousJarvisSession; destroyed when session reaches
'completed' or 'cancelled' status.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextInjection:
    """A single user-supplied preference or correction captured mid-task."""
    injection_type: str          # "language", "strategy", "account", "correction", "other"
    key: str                     # e.g. "language_preference"
    value: Any                   # e.g. "Java"
    utterance: str               # original user text
    timestamp: float = field(default_factory=time.time)


@dataclass
class CheckpointRecord:
    """Tracks a single asked-and-answered (or timed-out) checkpoint per step."""
    step_id: str
    question_text: str
    response: Optional[str] = None
    responded_at: Optional[float] = None
    timed_out: bool = False


@dataclass
class SessionContextBubble:
    """
    Isolated per-session context store.
    Created at session initialization; destroyed when session reaches
    'completed' or 'cancelled' status.
    """
    session_id: str

    # Preference fields (inheritable by ForkedSession — language + account only)
    language_preference: str = "telugu_english"
    strategy_preference: str = "optimal"
    account_context: Dict[str, Any] = field(default_factory=dict)
    # e.g. {"browser": "Chrome", "login_status": "logged_in", "account": "leetcode_main"}

    # History fields (NOT inherited by ForkedSession)
    corrections: List[str] = field(default_factory=list)
    checkpoint_responses: Dict[str, CheckpointRecord] = field(default_factory=dict)
    injections: List[ContextInjection] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)

    def add_injection(self, injection: ContextInjection) -> None:
        """Write a ContextInjection and update shortcut preference fields."""
        self.injections.append(injection)
        if injection.injection_type == "language":
            self.language_preference = str(injection.value)
        elif injection.injection_type == "strategy":
            self.strategy_preference = str(injection.value)
        elif injection.injection_type == "account":
            if isinstance(injection.value, dict):
                self.account_context.update(injection.value)
        elif injection.injection_type == "correction":
            self.corrections.append(str(injection.value))

    def record_checkpoint(self, record: CheckpointRecord) -> None:
        self.checkpoint_responses[record.step_id] = record

    def has_answered_checkpoint(self, step_id: str) -> bool:
        rec = self.checkpoint_responses.get(step_id)
        return rec is not None and rec.response is not None

    def is_login_confirmed(self, account_key: str = "login_status") -> bool:
        return self.account_context.get(account_key) == "logged_in"

    def merge_into_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of params enriched with all current bubble fields."""
        merged = dict(params)
        merged["language_preference"] = self.language_preference
        merged["strategy_preference"] = self.strategy_preference
        if self.account_context:
            merged["account_context"] = dict(self.account_context)
        return merged

    def fork_copy(self, new_session_id: str) -> "SessionContextBubble":
        """Produce a ForkedSession bubble: copy only language + account fields."""
        return SessionContextBubble(
            session_id=new_session_id,
            language_preference=self.language_preference,
            strategy_preference="optimal",      # reset to default
            account_context=copy.deepcopy(self.account_context),
            corrections=[],
            checkpoint_responses={},
            injections=[],
        )
