from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class QuickAction:
    action_id: str
    title: str
    command: str
    category: str = "automation"


@dataclass
class SmartSuggestion:
    suggestion_id: str
    title: str
    rationale: str
    action_command: str


@dataclass
class AIWorkspace:
    workspace_id: str = field(default_factory=lambda: f"ws_{uuid.uuid4().hex[:8]}")
    name: str = "Default Workspace"
    goal_description: str = ""
    chat_session_ids: List[str] = field(default_factory=list)
    automation_ids: List[str] = field(default_factory=list)
    associated_files: List[str] = field(default_factory=list)
    active_browser_urls: List[str] = field(default_factory=list)
    quick_actions: List[QuickAction] = field(default_factory=list)
    smart_suggestions: List[SmartSuggestion] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class AIWorkspaceManager:
    """
    AI OS Workspace Manager.
    Bundles chats, project goals, automations, active browser sessions, and workspace files.
    Enables active workflow resumption, quick actions, and smart predictive suggestions.
    """

    def __init__(self) -> None:
        self.workspaces: Dict[str, AIWorkspace] = {}
        # Create default workspace
        def_ws = AIWorkspace(workspace_id="ws_default", name="Primary Workspace", goal_description="General Development & Assistance")
        self.workspaces[def_ws.workspace_id] = def_ws

    def create_workspace(self, name: str, goal_description: str = "") -> AIWorkspace:
        ws = AIWorkspace(name=name, goal_description=goal_description)
        self.workspaces[ws.workspace_id] = ws
        return ws

    def add_chat_session(self, workspace_id: str, session_id: str) -> None:
        if workspace_id in self.workspaces:
            if session_id not in self.workspaces[workspace_id].chat_session_ids:
                self.workspaces[workspace_id].chat_session_ids.append(session_id)
                self.workspaces[workspace_id].updated_at = time.time()

    def register_quick_action(self, workspace_id: str, title: str, command: str) -> QuickAction:
        action = QuickAction(action_id=f"qa_{uuid.uuid4().hex[:6]}", title=title, command=command)
        if workspace_id in self.workspaces:
            self.workspaces[workspace_id].quick_actions.append(action)
        return action

    def generate_smart_suggestions(self, workspace_id: str) -> List[SmartSuggestion]:
        if workspace_id not in self.workspaces:
            return []

        ws = self.workspaces[workspace_id]
        suggestions = []
        if ws.active_browser_urls:
            suggestions.append(
                SmartSuggestion(
                    suggestion_id="sug_1",
                    title="Summarize Open Web Tabs",
                    rationale="You have open browser tabs associated with this workspace.",
                    action_command="summarize active browser tabs",
                )
            )

        if len(ws.chat_session_ids) > 1:
            suggestions.append(
                SmartSuggestion(
                    suggestion_id="sug_2",
                    title="Synthesize Project Knowledge",
                    rationale="Multiple chat sessions exist for this workspace goal.",
                    action_command="synthesize project chat knowledge",
                )
            )

        ws.smart_suggestions = suggestions
        return suggestions
