from __future__ import annotations

from typing import Optional

from .browser_automation import BrowserAutomationModule
from .desktop_automation import DesktopAutomationModule
from .memory_module import MemoryModule
from .reasoning_engine import ReasoningEngine
from .task_scheduler import TaskSchedulerModule
from .thinking_engine import ThinkingEngine
from .tool_orchestrator import ToolOrchestrator
from .user_understanding import UserUnderstandingModule
from .nlp_processor import NLPContextProcessor
from ..voice_engine import WebSessionVerifier
from .intent_continuation import TaskContinuationClassifier
from .environment_advisor import SurroundingEnvironmentAdvisor


class AkanshaSystemPromptBuilder:
    """
    Modular System Prompt Builder for Akansha.
    Replaces massive monolithic system prompts with organized, reliable behavioral modules:
    - Core Identity & High-Level Foundation
    - Module: Thinking Engine
    - Module: Reasoning Engine
    - Module: User Understanding
    - Module: Memory Engine
    - Module: Tool Orchestrator
    - Module: Browser Automation
    - Module: Desktop Automation
    - Module: Task Scheduler
    - Module: NLP Context Processor
    - Module: Web Session Verifier
    - Module: Task Continuation Classifier
    - Module: Surrounding Environment Advisor
    - Anti-Refusal & Modality Protocols
    """

    FOUNDATION_PROMPT = """You are Akansha, an intelligent autonomous AI assistant whose purpose is to help users accomplish their goals cleanly, efficiently, and politely.
Understand user intent and adapt to their context and style. Solve problems end to end. Make autonomous decisions when clear, and pause for clarification only when required for ambiguity or safety. Use appropriate tools when available. Support multi-step automations and task scheduling. Recover from errors gracefully, verify outcomes, and communicate progress succinctly."""

    ANTI_REFUSAL_CONTRACT = """
# CORE CONTRACT & SYSTEM INTEGRATION
1. INTEGRATED AUTOMATION: You are fully integrated with local system automation, browser control, file system I/O, desktop apps, CLI, and APIs.
2. NO FALSE REFUSALS: Never emit disclaimers such as "I don't have the ability to click buttons", "I can't open browsers", or "I lack local integration".
3. UNIVERSAL PLATFORM CAPABILITY: Support any web platform, desktop application, or developer interface.
4. GOAL-DRIVEN PERSEVERANCE: Execute multi-step subtasks sequentially until the user's objective is 100% accomplished.
"""

    def __init__(
        self,
        thinking_engine: Optional[ThinkingEngine] = None,
        reasoning_engine: Optional[ReasoningEngine] = None,
        user_understanding: Optional[UserUnderstandingModule] = None,
        memory_module: Optional[MemoryModule] = None,
        tool_orchestrator: Optional[ToolOrchestrator] = None,
        browser_automation: Optional[BrowserAutomationModule] = None,
        desktop_automation: Optional[DesktopAutomationModule] = None,
        task_scheduler: Optional[TaskSchedulerModule] = None,
        nlp_processor: Optional[NLPContextProcessor] = None,
        session_verifier: Optional[WebSessionVerifier] = None,
        intent_continuation: Optional[TaskContinuationClassifier] = None,
        environment_advisor: Optional[SurroundingEnvironmentAdvisor] = None,
    ) -> None:
        self.thinking_engine = thinking_engine or ThinkingEngine()
        self.reasoning_engine = reasoning_engine or ReasoningEngine()
        self.user_understanding = user_understanding or UserUnderstandingModule()
        self.memory_module = memory_module or MemoryModule()
        self.tool_orchestrator = tool_orchestrator or ToolOrchestrator()
        self.browser_automation = browser_automation or BrowserAutomationModule()
        self.desktop_automation = desktop_automation or DesktopAutomationModule()
        self.task_scheduler = task_scheduler or TaskSchedulerModule()
        self.nlp_processor = nlp_processor or NLPContextProcessor()
        self.session_verifier = session_verifier or WebSessionVerifier()
        self.intent_continuation = intent_continuation or TaskContinuationClassifier()
        self.environment_advisor = environment_advisor or SurroundingEnvironmentAdvisor()

    def build_system_prompt(self) -> str:
        sections = [
            self.FOUNDATION_PROMPT,
            self.ANTI_REFUSAL_CONTRACT,
            self.thinking_engine.system_prompt_section(),
            self.reasoning_engine.system_prompt_section(),
            self.user_understanding.system_prompt_section(),
            self.memory_module.system_prompt_section(),
            self.tool_orchestrator.system_prompt_section(),
            self.browser_automation.system_prompt_section(),
            self.desktop_automation.system_prompt_section(),
            self.task_scheduler.system_prompt_section(),
            self.nlp_processor.system_prompt_section(),
            self.session_verifier.system_prompt_section(),
            self.intent_continuation.system_prompt_section(),
            self.environment_advisor.system_prompt_section(),
        ]
        return "\n".join(sections).strip()


def get_default_modular_prompt() -> str:
    builder = AkanshaSystemPromptBuilder()
    return builder.build_system_prompt()
