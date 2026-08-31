"""
Akansha Modular Agent Engine Framework.
Provides modular sub-engines for Thinking, Reasoning, User Understanding,
Memory, Tool Orchestration, Browser Automation, Desktop Automation, Task Scheduling,
System Prompt Compilation, Fast Domain Routing, Targeted Memory Retrieval,
Selective Risk Verification, Parallel Task Execution, Tiered Execution,
Habit Shortcuts, Adaptive Interaction Pace Tuning, 3-Lane Execution Engine,
Continuous Session State, State-Aware Intent Routing, Self-Healing Recovery,
Multilingual Code-Switching Engine, Unified Context Manager, Module Cost Justifier,
Background Preloader, AI OS Plugin Manager, Observability Tracer,
AI OS Workspace Manager, Context Compressor, Offline Resilience Engine,
AI Governor Engine, Universal Task Graph, AI OS Event Bus, Semantic Action History,
Automation Recorder, Universal Search Engine, Multi-Agent Collaborator,
Model Compatibility Layer, OpenWork Capability Bridge, Continuous Voice Jarvis Engine,
and Diagnostics Engine.
"""

from .thinking_engine import ThinkingEngine, SubTask, ThinkingResult
from .reasoning_engine import ReasoningEngine, TradeoffEvaluation, ReasoningResult
from .user_understanding import UserUnderstandingModule, UserPreference, FeedbackEntry
from .memory_module import MemoryModule, TaskState, ProjectContext
from .tool_orchestrator import ToolOrchestrator, ToolAction, RiskAssessment
from .browser_automation import BrowserAutomationModule, TabState, NavigationResult
from .desktop_automation import DesktopAutomationModule, WindowState, FileOperationResult
from .task_scheduler import TaskSchedulerModule, ScheduledTask, Checkpoint
from .system_prompt_builder import AkanshaSystemPromptBuilder
from .openwork_bridge import OpenWorkCapabilityBridge, CapabilityDefinition
from .continuous_jarvis_engine import ContinuousVoiceJarvisEngine, ContinuousJarvisSession, ContinuousSubtaskNode
from .domain_executors import (
    FastDomainRouter,
    CapabilityMap,
    DesktopDomainExecutor,
    BrowserDomainExecutor,
    SchedulerDomainExecutor,
    ConversationalDomainExecutor,
    JarvisDomainExecutor,
)
from .retrieval_memory import RelevantMemoryRetriever
from .risk_verifier import SelectiveRiskVerifier, VerificationDecision
from .parallel_planner import ParallelTaskRunner
from .tiered_execution import (
    TieredExecutionEngine,
    Tier0FastGate,
    Tier1Classifier,
    Tier2DomainPlanner,
    Tier3DeepReasoning,
    ExecutionResult,
)
from .habit_shortcuts import HabitShortcutEngine, Shortcut
from .adaptive_interaction import AdaptiveInteractionEngine, PaceProfile
from .three_lanes import (
    ThreeLaneExecutionEngine,
    ModuleResponsibilityMap,
    IntentExecutorTable,
    FastLane,
    StandardLane,
    ReasoningLane,
    LaneExecutionResult,
)
from .ai_os_core import ContinuousSessionState, ConversationManager, WorkflowState, TurnRecord
from .ai_os_router import StateAwareIntentRouter, StateAwareRouteResult
from .self_healing import SelfHealingRecoveryModule, RecoveryAttempt
from .multilingual_engine import MultilingualEngine, LanguageDetectionResult
from .unified_context import UnifiedContextManager, ModuleCostManager, UnifiedContextState
from .background_preloader import BackgroundPreloader
from .plugin_system import AIOSPlugin, AIOSPluginManager
from .observability import ObservabilityTracer, TraceSpan
from .workspace_manager import AIWorkspaceManager, AIWorkspace, QuickAction, SmartSuggestion
from .context_compressor import ContextCompressor, CompressedContext
from .offline_resilience import OfflineResilienceEngine, QueuedAutomationTask
from .cross_cutting import (
    AIGovernorEngine,
    PolicyEvaluation,
    UniversalTaskGraph,
    TaskNode,
    AIOSEventBus,
    SemanticActionHistory,
    ActionHistoryRecord,
    AutomationRecorder,
    RecordedStep,
    UniversalSearchEngine,
    MultiAgentCollaborator,
    AgentRole,
    ModelCompatibilityLayer,
    DiagnosticsEngine,
)
from .session_context_bubble import SessionContextBubble, ContextInjection, CheckpointRecord
from .improvisation_engine import ImprovisationEngine, ImprovRequest, PlanMutation, MutationType
from .task_fork_manager import TaskForkManager, LaneRegistry
from .checkpoint_gate import CheckpointGate, CheckpointQuestion
from .voice_feedback_loop import VoiceFeedbackLoop, StatusNarration, AudioEvent
from .conversational_orchestrator import (
    ConversationalOrchestrator,
    IntentResolver,
    OrchestrationState,
    RoutingAction,
    RoutingDecision,
)

__all__ = [
    "ThinkingEngine",
    "SubTask",
    "ThinkingResult",
    "ReasoningEngine",
    "TradeoffEvaluation",
    "ReasoningResult",
    "UserUnderstandingModule",
    "UserPreference",
    "FeedbackEntry",
    "MemoryModule",
    "TaskState",
    "ProjectContext",
    "ToolOrchestrator",
    "ToolAction",
    "RiskAssessment",
    "BrowserAutomationModule",
    "TabState",
    "NavigationResult",
    "DesktopAutomationModule",
    "WindowState",
    "FileOperationResult",
    "TaskSchedulerModule",
    "ScheduledTask",
    "Checkpoint",
    "AkanshaSystemPromptBuilder",
    "OpenWorkCapabilityBridge",
    "CapabilityDefinition",
    "ContinuousVoiceJarvisEngine",
    "ContinuousJarvisSession",
    "ContinuousSubtaskNode",
    "FastDomainRouter",
    "CapabilityMap",
    "DesktopDomainExecutor",
    "BrowserDomainExecutor",
    "SchedulerDomainExecutor",
    "ConversationalDomainExecutor",
    "JarvisDomainExecutor",
    "RelevantMemoryRetriever",
    "SelectiveRiskVerifier",
    "VerificationDecision",
    "ParallelTaskRunner",
    "TieredExecutionEngine",
    "Tier0FastGate",
    "Tier1Classifier",
    "Tier2DomainPlanner",
    "Tier3DeepReasoning",
    "ExecutionResult",
    "HabitShortcutEngine",
    "Shortcut",
    "AdaptiveInteractionEngine",
    "PaceProfile",
    "ThreeLaneExecutionEngine",
    "ModuleResponsibilityMap",
    "IntentExecutorTable",
    "FastLane",
    "StandardLane",
    "ReasoningLane",
    "LaneExecutionResult",
    "ContinuousSessionState",
    "ConversationManager",
    "WorkflowState",
    "TurnRecord",
    "StateAwareIntentRouter",
    "StateAwareRouteResult",
    "SelfHealingRecoveryModule",
    "RecoveryAttempt",
    "MultilingualEngine",
    "LanguageDetectionResult",
    "UnifiedContextManager",
    "ModuleCostManager",
    "UnifiedContextState",
    "BackgroundPreloader",
    "AIOSPlugin",
    "AIOSPluginManager",
    "ObservabilityTracer",
    "TraceSpan",
    "AIWorkspaceManager",
    "AIWorkspace",
    "QuickAction",
    "SmartSuggestion",
    "ContextCompressor",
    "CompressedContext",
    "OfflineResilienceEngine",
    "QueuedAutomationTask",
    "AIGovernorEngine",
    "PolicyEvaluation",
    "UniversalTaskGraph",
    "TaskNode",
    "AIOSEventBus",
    "SemanticActionHistory",
    "ActionHistoryRecord",
    "AutomationRecorder",
    "RecordedStep",
    "UniversalSearchEngine",
    "MultiAgentCollaborator",
    "AgentRole",
    "ModelCompatibilityLayer",
    "DiagnosticsEngine",
    # New modules — continuous-conversational-automation
    "SessionContextBubble",
    "ContextInjection",
    "CheckpointRecord",
    "ImprovisationEngine",
    "ImprovRequest",
    "PlanMutation",
    "MutationType",
    "TaskForkManager",
    "LaneRegistry",
    "CheckpointGate",
    "CheckpointQuestion",
    "VoiceFeedbackLoop",
    "StatusNarration",
    "AudioEvent",
    "ConversationalOrchestrator",
    "IntentResolver",
    "OrchestrationState",
    "RoutingAction",
    "RoutingDecision",
]
