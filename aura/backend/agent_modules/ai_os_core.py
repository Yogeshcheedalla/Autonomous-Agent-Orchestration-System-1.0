from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from .thinking_engine import SubTask


@dataclass
class WorkflowState:
    workflow_id: str = field(default_factory=lambda: f"wf_{uuid.uuid4().hex[:8]}")
    title: str = ""
    subtasks: List[SubTask] = field(default_factory=list)
    current_subtask_index: int = 0
    variables: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class TurnRecord:
    turn_id: str
    user_input: str
    assistant_response: str
    active_domain: str
    timestamp: float = field(default_factory=time.time)


class ContinuousSessionState:
    """
    Continuous Session State Manager for AI Operating System.
    Maintains persistent active session context, state variables, turn records,
    and ongoing multi-turn workflows across turns so reasoning does NOT restart from scratch each time.
    """

    def __init__(self, session_id: str = "default") -> None:
        self.session_id = session_id
        self.active_workflow: Optional[WorkflowState] = None
        self.session_variables: Dict[str, Any] = {}
        self.turn_history: List[TurnRecord] = []
        self.last_active_domain: str = "conversational"

    def start_workflow(self, title: str, subtasks: List[SubTask], initial_vars: Optional[Dict[str, Any]] = None) -> WorkflowState:
        wf = WorkflowState(
            title=title,
            subtasks=subtasks,
            variables=initial_vars or {},
            is_active=True,
        )
        self.active_workflow = wf
        return wf

    def get_active_subtask(self) -> Optional[SubTask]:
        if not self.active_workflow or not self.active_workflow.is_active:
            return None
        idx = self.active_workflow.current_subtask_index
        if idx < len(self.active_workflow.subtasks):
            return self.active_workflow.subtasks[idx]
        return None

    def advance_subtask(self, result: Optional[Any] = None) -> Optional[SubTask]:
        if not self.active_workflow or not self.active_workflow.is_active:
            return None
        current = self.get_active_subtask()
        if current:
            current.status = "completed"
            current.result = result

        self.active_workflow.current_subtask_index += 1
        self.active_workflow.updated_at = time.time()
        next_st = self.get_active_subtask()

        if not next_st:
            self.active_workflow.is_active = False

        return next_st

    def record_turn(self, user_input: str, response: str, domain: str = "conversational") -> TurnRecord:
        record = TurnRecord(
            turn_id=f"turn_{len(self.turn_history)+1}",
            user_input=user_input,
            assistant_response=response,
            active_domain=domain,
        )
        self.turn_history.append(record)
        self.last_active_domain = domain
        return record

    def get_session_summary(self) -> str:
        parts = [f"Session ID: {self.session_id}", f"Last Active Domain: {self.last_active_domain}"]
        if self.active_workflow and self.active_workflow.is_active:
            st = self.get_active_subtask()
            st_title = st.title if st else "Completed"
            parts.append(f"Active Workflow: '{self.active_workflow.title}' (Step {self.active_workflow.current_subtask_index+1}/{len(self.active_workflow.subtasks)}: {st_title})")
        return " | ".join(parts)


class ConversationManager:
    """
    Conversation Manager.
    Ensures natural, human-like assistant interactions rather than robotic chatbot responses.
    Handles mode adaptations, fluid turn transitions, and tone consistency.
    """

    def format_human_assistant_response(self, text: str, mode: str = "text") -> str:
        text_clean = text.strip()
        if mode == "voice":
            # Remove all markdown formatting for human-like acoustic speech
            text_clean = re.sub(r"[#\*_`\-\>\+\=\[\]\(\)]", "", text_clean)
            return " ".join(text_clean.split())
        return text_clean
