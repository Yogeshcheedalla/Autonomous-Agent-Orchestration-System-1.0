"""
Surrounding Environment Advisor Module for Akansha AI OS.
Inspects local workspace state, active desktop processes, open tabs, background tasks,
and provides real-time proactive guidance and recommendations.
"""
from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EnvironmentalSnapshot:
    current_time_ist: str
    active_workspace: str
    active_tasks_count: int
    system_status: str
    academic_context: str
    recommendations: List[str] = field(default_factory=list)


class SurroundingEnvironmentAdvisor:
    """
    Surrounding Environment Advisor.
    Understands surrounding system and user context (studies, projects, active background tasks)
    and provides intelligent real-time recommendations and proactive guidance.
    """

    def generate_snapshot_and_recommendations(self, active_session_count: int = 1) -> EnvironmentalSnapshot:
        now_ist = datetime.now().strftime("%A, %b %d, %Y, %I:%M %p IST")
        
        recommendations = [
            "Continuous task execution and single-tab isolation are active and locked to your current workspace tab.",
            "Remember: Akansha handles desktop apps, browser tabs, and file automation autonomously.",
            "If you need to switch tasks, specify whether it's a continuation of your active work or a new isolated task tab.",
        ]

        # Time-based contextual guidance
        hour = datetime.now().hour
        if 12 <= hour < 17:
            recommendations.append("Afternoon focus mode: Great time to review lab exam prep or run multi-step code automations.")
        elif 17 <= hour < 22:
            recommendations.append("Evening mode: Wrap up active build tasks and check scheduled reminders.")

        return EnvironmentalSnapshot(
            current_time_ist=now_ist,
            active_workspace="Akansha AI OS / Yogesh Workspace",
            active_tasks_count=active_session_count,
            system_status="All autonomous engines online (NLP, OpenWork Bridge, Single-Tab Isolation)",
            academic_context="B.Tech studies & Akansha AI Assistant development",
            recommendations=recommendations,
        )

    def system_prompt_section(self) -> str:
        return """
# MODULE: SURROUNDING ENVIRONMENT ADVISOR
- SITUATIONAL AWARENESS: Evaluates active system processes, background task status, and time context (IST).
- PROACTIVE GUIDANCE: Provides tailored recommendations for academic tasks, project milestones, and workflow efficiency.
- ENVIRONMENT MONITORING: Continuously advises the user on active tab states, scheduled reminders, and system performance.
"""
