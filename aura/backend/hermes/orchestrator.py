from __future__ import annotations

from typing import Any

from .action_platform import (
    ActionPlatformMetrics,
    AutonomousBookingEngine,
    AutonomousCommerceEngine,
    DigitalConciergeEngine,
    LifeAutomationEngine,
    MultiServiceExecutionBus,
    PersonalBuyingIntelligence,
    VerificationRecheckEngine,
)
from .agents.coordinator import Coordinator
from .agents.dynamic_hiring import DynamicHiringEngine
from .agents.final_output import FinalOutputBuilder
from .agents.result_merger import ResultMerger
from .agents.task_analyzer import TaskAnalyzerAgent
from .agents.validation_agent import ValidationAgent
from .background.jobs import BackgroundJobEngine
from .database.store import CognitiveStore, get_cognitive_store
from .evolution.self_evolution import SelfEvolutionEngine
from .experiments.experiment_lab import AIExperimentLab
from .explainability.explainable_ai import ExplainableAIEngine
from .goals.decision_simulation import DecisionSimulationEngine
from .goals.executive_brain import DigitalExecutiveBrain
from .goals.goal_graph import GoalGraphEngine
from .goals.opportunity_detection import OpportunityDetectionEngine
from .goals.personal_os import PersonalOperatingSystem
from .goals.project_manager import AutonomousProjectManager
from .knowledge.knowledge_graph import KnowledgeGraphEngine
from .learning.experience_engine import ExperienceEngine
from .learning.failure_analysis import FailureAnalysisEngine
from .learning.reflection_engine import ReflectionEngine
from .memory.cognitive_compression import CognitiveCompressionEngine
from .memory.long_term import LongTermMemory
from .memory.memory_compression import MemoryCompressionEngine
from .multimodal.context_graph import MultimodalContextGraph
from .memory.retrieval_engine import MemoryRetrievalEngine
from .memory.shared_bus import SharedMemoryBus
from .memory.short_term import ShortTermMemory
from .monitoring.metrics import HermesMetrics
from .observatory.dashboard import CognitiveObservatoryDashboard
from .operating_system import (
    CognitiveHealthEngine,
    ProactiveEventEngine,
    SelfHealingEngine,
    UncertaintyCollaborationEngine,
    UniversalAutomationLayer,
    UniversalAutonomousExecutionEngine,
)
from .predictive.predictive_assistant import PredictiveAssistant
from .reasoning.planner import CognitivePlanner
from .replay.time_machine import TimeMachineReplayEngine
from .safety.validator import SafetyValidator
from .skills.marketplace import SkillMarketplace
from .skills.skill_generator import SkillGenerator
from .skills.skill_optimizer import SkillOptimizer
from .skills.skill_retirement import SkillRetirement
from .skills.skill_registry import SkillRegistry
from .skills.skill_validator import SkillValidator
from .tools.universal_tool_layer import UniversalToolLayer
from .testing.autonomous_testing import AutonomousTestingEngine
from .ui.adaptive_ui import AdaptiveUIEngine
from .twin import CognitiveDigitalTwinEngine, FutureSimulationEngine, PredictiveRecommendationEngine
from .workflows.workflow_generator import WorkflowGenerator
from .world.world_model import WorldModelEngine
from ..agent_modules import (
    AkanshaSystemPromptBuilder,
    BrowserAutomationModule,
    DesktopAutomationModule,
    MemoryModule,
    ReasoningEngine,
    TaskSchedulerModule,
    ThinkingEngine,
    ToolOrchestrator,
    UserUnderstandingModule,
)


class HermesCognitiveOS:
    def __init__(self, store: CognitiveStore | None = None) -> None:
        self.store = store or get_cognitive_store()
        self.thinking_engine = ThinkingEngine()
        self.reasoning_engine = ReasoningEngine()
        self.user_understanding = UserUnderstandingModule()
        self.memory_module = MemoryModule()
        self.tool_orchestrator = ToolOrchestrator()
        self.browser_automation = BrowserAutomationModule()
        self.desktop_automation = DesktopAutomationModule()
        self.task_scheduler = TaskSchedulerModule()
        self.prompt_builder = AkanshaSystemPromptBuilder(
            thinking_engine=self.thinking_engine,
            reasoning_engine=self.reasoning_engine,
            user_understanding=self.user_understanding,
            memory_module=self.memory_module,
            tool_orchestrator=self.tool_orchestrator,
            browser_automation=self.browser_automation,
            desktop_automation=self.desktop_automation,
            task_scheduler=self.task_scheduler,
        )
        self.short_term = ShortTermMemory(self.store)
        self.long_term = LongTermMemory(self.store)
        self.retrieval = MemoryRetrievalEngine(self.store)
        self.memory_bus = SharedMemoryBus(self.store)
        self.compression = MemoryCompressionEngine(self.store)
        self.cognitive_compression = CognitiveCompressionEngine(self.store)
        self.experiences = ExperienceEngine(self.store)
        self.reflection = ReflectionEngine(self.store)
        self.skills = SkillRegistry(self.store)
        self.marketplace = SkillMarketplace(self.store)
        self.skill_generator = SkillGenerator(self.store)
        self.skill_optimizer = SkillOptimizer(self.store)
        self.skill_validator = SkillValidator()
        self.skill_retirement = SkillRetirement(self.store)
        self.tools = UniversalToolLayer(self.store)
        self.world_model = WorldModelEngine(self.store)
        self.predictive = PredictiveAssistant(self.store)
        self.adaptive_ui = AdaptiveUIEngine()
        self.multimodal = MultimodalContextGraph(self.store)
        self.workflow_generator = WorkflowGenerator(self.store)
        self.background_jobs = BackgroundJobEngine(self.store)
        self.goal_graph = GoalGraphEngine(self.store)
        self.project_manager = AutonomousProjectManager(self.store)
        self.opportunities = OpportunityDetectionEngine(self.store)
        self.decisions = DecisionSimulationEngine(self.store)
        self.personal_os = PersonalOperatingSystem(self.store)
        self.executive_brain = DigitalExecutiveBrain(self.store)
        self.verification_recheck = VerificationRecheckEngine(self.store)
        self.buying_intelligence = PersonalBuyingIntelligence(self.store)
        self.commerce = AutonomousCommerceEngine(self.store)
        self.booking = AutonomousBookingEngine(self.store)
        self.life_automation = LifeAutomationEngine(self.store)
        self.concierge = DigitalConciergeEngine(self.store)
        self.execution_bus = MultiServiceExecutionBus(self.store)
        self.action_metrics = ActionPlatformMetrics(self.store)
        self.observatory = CognitiveObservatoryDashboard(self.store)
        self.universal_execution = UniversalAutonomousExecutionEngine(self.store)
        self.uncertainty_collaboration = UncertaintyCollaborationEngine(self.store)
        self.self_healing = SelfHealingEngine(self.store)
        self.proactive_events = ProactiveEventEngine(self.store)
        self.universal_automation = UniversalAutomationLayer(self.store)
        self.cognitive_health = CognitiveHealthEngine(self.store)
        self.self_evolution = SelfEvolutionEngine(self.store)
        self.explainability = ExplainableAIEngine(self.store)
        self.replay = TimeMachineReplayEngine(self.store)
        self.experiments = AIExperimentLab(self.store)
        self.knowledge_graph = KnowledgeGraphEngine(self.store)
        self.autonomous_testing = AutonomousTestingEngine(self.store)
        self.digital_twin = CognitiveDigitalTwinEngine(self.store)
        self.future_simulation = FutureSimulationEngine(self.store)
        self.predictive_recommendations = PredictiveRecommendationEngine(self.store)
        self.planner = CognitivePlanner(self.store)
        self.coordinator = Coordinator(self.store)
        self.task_analyzer = TaskAnalyzerAgent()
        self.hiring_engine = DynamicHiringEngine()
        self.result_merger = ResultMerger()
        self.validation_agent = ValidationAgent()
        self.final_output = FinalOutputBuilder()
        self.failures = FailureAnalysisEngine(self.store)
        self.metrics = HermesMetrics()
        self.safety = SafetyValidator()

    def process_task(self, task: str, approved: bool = False) -> dict[str, Any]:
        with self.metrics.time_block("process_task"):
            analysis = self.task_analyzer.analyze(task).to_dict()
            plan = self.planner.plan(task)
            hiring_plan = self.hiring_engine.hiring_plan(analysis)
            adaptive_ui = self.adaptive_ui.mode_for_task(task, analysis)
            workflow = self.workflow_generator.generate(task)
            tool_plan = self.tools.plan_for_task(task, analysis["tools"], approved=approved)
            world_context = self.world_model.observe_task(task)
            digital_twin = self.digital_twin.observe_task(task, analysis)
            predictions = self.predictive.predict(task, analysis)
            executive_brain = self.executive_brain.preview(task, analysis)
            future_simulation = self.future_simulation.simulate(
                task,
                owner="Yogesh",
                context={
                    "analysis": analysis,
                    "complexity_score": analysis["complexity_score"],
                    "resource_pressure": min(1.0, analysis["complexity_score"] / 100.0),
                    "predictive_assistant": predictions,
                    "executive_brain": executive_brain,
                },
            )
            proactive_recommendations = self.predictive_recommendations.recommend(
                task,
                digital_twin["profile"],
                future_simulation,
                analysis,
            )
            action_platform = self._action_platform_plan(task, analysis, approved)
        safety = self.safety.validate_learning_action(
            {
                "type": analysis["intent"],
                "tools": analysis["tools"],
                "risk_level": analysis["risk_level"],
                "loop_key": analysis["intent"],
                "approved": approved,
            }
        )
        core_agents = self.coordinator.ensure_core_agents()
        workers = []
        if safety["allowed"]:
            workers = self.coordinator.assign_temporary_workers(task, hiring_plan["agent_types"], analysis["complexity_score"])
            self.metrics.increment("tasks_allowed")
        else:
            self.metrics.increment("tasks_blocked")
        skill_routes = self.coordinator.route_skills(task, analysis["intent"])
        allocation = self.coordinator.resource_allocation(analysis["complexity_score"], len(workers))
        memory_snapshot = self.memory_bus.snapshot(task)
        collaboration = self.uncertainty_collaboration.evaluate(
            task=task,
            analysis=analysis,
            tool_plan=tool_plan,
            safety=safety,
            predictions=predictions,
        )
        universal_execution = self.universal_execution.create_execution(
            task=task,
            analysis=analysis,
            workflow=workflow,
            hiring_plan=hiring_plan,
            skill_routes=skill_routes,
            tool_plan=tool_plan,
            safety=safety,
            approved=approved,
            collaboration=collaboration,
        )
        universal_automation = self.universal_automation.plan(task, analysis, tool_plan, approved)
        simulated_worker_results = [
            {
                "worker": worker["agent_name"],
                "result": f"{worker['agent_name']} prepared {worker['specialization']} findings.",
                "confidence": worker["confidence"],
            }
            for worker in workers
        ]
        merged = self.result_merger.merge(simulated_worker_results)
        validation = self.validation_agent.validate({"analysis": analysis, "safety": safety, "merged": merged})
        final = self.final_output.build(analysis, validation, merged)
        self_healing = self.self_healing.recover(
            task=task,
            analysis=analysis,
            validation=validation,
            tool_plan=tool_plan,
            safety=safety,
            collaboration=collaboration,
        )
        proactive_events = self.proactive_events.observe(task, analysis, universal_execution, self_healing)
        cognitive_health = self.cognitive_health.snapshot(self.metrics.snapshot(), analysis, collaboration, self_healing)
        explanation = self.explainability.explain_action(
            task=task,
            analysis=analysis,
            workflow=workflow,
            hiring_plan=hiring_plan,
            skill_routes=skill_routes,
            tool_plan=tool_plan,
            confidence=validation.get("confidence"),
        )
        test_plan = self.autonomous_testing.generate_test_plan(
            scope=analysis["intent"],
            changed_files=[],
            task=task,
        )
        evolution = self.self_evolution.optimize(task, self.metrics.snapshot())
        replay_event = self.replay.record(
            "task",
            plan["plan_id"],
            {
                "task": task,
                "intent": analysis["intent"],
                "workflow_id": workflow.get("id"),
                "worker_count": len(workers),
                "skill_routes": skill_routes,
                "validation": validation,
                "explanation_id": explanation["id"],
            },
            explanation["confidence"],
        )
        knowledge = self.knowledge_graph.ingest_goal_skill_user(
            {
                "goal": task if analysis["intent"] == "goal_management" else "",
                "user": "Yogesh",
                "skills": skill_routes,
            }
        )
        observatory = self.observatory.snapshot(self.metrics.snapshot())
        return {
            "analysis": analysis,
            "plan": plan,
            "hiring_plan": hiring_plan,
            "workflow": workflow,
            "tool_plan": tool_plan,
            "world_context": world_context,
            "predictive_assistant": predictions,
            "digital_twin": digital_twin,
            "future_simulation": future_simulation,
            "predictive_recommendations": proactive_recommendations,
            "executive_brain": executive_brain,
            "action_platform": action_platform,
            "universal_execution": universal_execution,
            "uncertainty_collaboration": collaboration,
            "self_healing": self_healing,
            "proactive_events": proactive_events,
            "universal_automation": universal_automation,
            "cognitive_health": cognitive_health,
            "adaptive_ui": adaptive_ui,
            "safety": safety,
            "persistent_core_agents": core_agents,
            "temporary_workers": workers,
            "skill_routes": skill_routes,
            "resource_allocation": allocation,
            "memory_bus": memory_snapshot,
            "result_merge": merged,
            "validation": validation,
            "final_output": final,
            "explainable_ai": explanation,
            "time_machine_replay": replay_event,
            "self_evolution": evolution,
            "knowledge_graph": knowledge,
            "autonomous_testing": test_plan,
            "cognitive_observatory": observatory,
            "metrics": self.metrics.snapshot(),
            "expected_efficiency": self._expected_efficiency(analysis["complexity_score"], len(workers)),
        }

    def _action_platform_plan(self, task: str, analysis: dict[str, Any], approved: bool) -> dict[str, Any]:
        intent = analysis["intent"]
        if intent == "commerce":
            commerce = self.commerce.plan(task, approved=approved)
            bus = self.execution_bus.plan(
                "commerce",
                {"commerce_request_id": commerce["id"], "needs_documents": False},
                approved=approved,
            )
            return {"active": True, "commerce": commerce, "execution_bus": bus, "metrics": self.action_metrics.snapshot()}
        if intent == "booking":
            booking = self.booking.plan(task, approved=approved)
            bus = self.execution_bus.plan(
                "booking",
                {"booking_request_id": booking["id"]},
                approved=approved,
            )
            return {"active": True, "booking": booking, "execution_bus": bus, "metrics": self.action_metrics.snapshot()}
        if intent == "life_automation":
            plan = self.life_automation.plan(task, approved=approved)
            bus = self.execution_bus.plan(
                "life_automation",
                {"life_automation_plan_id": plan["id"]},
                approved=approved,
            )
            return {"active": True, "life_automation": plan, "execution_bus": bus, "metrics": self.action_metrics.snapshot()}
        if intent == "concierge":
            concierge = self.concierge.plan(task, approved=approved)
            bus = self.execution_bus.plan(
                "concierge",
                {"concierge_plan_id": concierge["id"], "needs_documents": True},
                approved=approved,
            )
            return {"active": True, "concierge": concierge, "execution_bus": bus, "metrics": self.action_metrics.snapshot()}
        return {"active": False, "metrics": self.action_metrics.snapshot()}

    def _expected_efficiency(self, complexity: float, worker_count: int) -> dict[str, Any]:
        if worker_count == 0:
            reduction = "40-55%"
        elif worker_count <= 2:
            reduction = "50-62%"
        elif worker_count <= 4:
            reduction = "58-68%"
        else:
            reduction = "62-72%"
        return {
            "token_reduction_estimate": reduction,
            "hallucination_control": "higher because facts route through memory/skill/safety checks",
            "debuggability": "high: coordinator owns final merge and temporary workers cannot spawn recursively",
            "learning_stability": "stable: only coordinator writes durable memory",
        }
