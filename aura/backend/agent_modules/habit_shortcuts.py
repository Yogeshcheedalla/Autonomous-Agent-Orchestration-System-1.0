from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Shortcut:
    trigger: str
    expanded_command: str
    created_at: float = field(default_factory=time.time)
    usage_count: int = 1
    is_user_defined: bool = False


class HabitShortcutEngine:
    """
    Module: Habit & Shortcut Builder Engine
    Objectives:
    - Track command usage frequency and identify user habit patterns
    - Automatically create and register custom shortcuts
    - Instant shortcut expansion for Tier 0 fast gate triggers
    """

    def __init__(self, habit_threshold: int = 3) -> None:
        self.habit_threshold = habit_threshold
        self.command_frequency: Dict[str, int] = defaultdict(int)
        self.shortcuts: Dict[str, Shortcut] = {}

    def record_command(self, user_input: str) -> Optional[Shortcut]:
        cmd_clean = user_input.strip().lower()
        self.command_frequency[cmd_clean] += 1
        count = self.command_frequency[cmd_clean]

        # Auto-create shortcut if repeated habit threshold reached
        if count >= self.habit_threshold and cmd_clean not in self.shortcuts:
            trigger_alias = f"shortcut_{len(self.shortcuts)+1}"
            shortcut = Shortcut(
                trigger=trigger_alias,
                expanded_command=user_input,
                usage_count=count,
                is_user_defined=False,
            )
            self.shortcuts[trigger_alias] = shortcut
            return shortcut
        elif cmd_clean in self.shortcuts:
            self.shortcuts[cmd_clean].usage_count += 1

        return None

    def register_custom_shortcut(self, trigger: str, expanded_command: str) -> Shortcut:
        """Register explicit user-defined shortcut."""
        trig_clean = trigger.strip().lower()
        shortcut = Shortcut(
            trigger=trig_clean,
            expanded_command=expanded_command,
            usage_count=1,
            is_user_defined=True,
        )
        self.shortcuts[trig_clean] = shortcut
        return shortcut

    def expand_shortcut(self, user_input: str) -> str:
        """Expand trigger alias into full command string if matched."""
        trig_clean = user_input.strip().lower()
        if trig_clean in self.shortcuts:
            sc = self.shortcuts[trig_clean]
            sc.usage_count += 1
            return sc.expanded_command
        return user_input

    def get_habit_profile(self) -> Dict[str, Any]:
        return {
            "total_commands_recorded": sum(self.command_frequency.values()),
            "top_habits": sorted(self.command_frequency.items(), key=lambda x: x[1], reverse=True)[:5],
            "active_shortcuts": [
                {"trigger": sc.trigger, "expanded": sc.expanded_command, "uses": sc.usage_count}
                for sc in self.shortcuts.values()
            ],
        }
