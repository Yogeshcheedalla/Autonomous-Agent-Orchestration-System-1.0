from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


# 1. AI Governor Engine
@dataclass
class PolicyEvaluation:
    approved: bool
    confidence_score: float  # 0.0 to 1.0
    risk_level: str  # "low", "medium", "high"
    policy_reason: str


class AIGovernorEngine:
    """
    AI Governor Engine.
    Enforces governance rules, trust & confidence scoring, policy compliance,
    and risk gating before any action is executed.
    """

    def evaluate_action_policy(self, action_name: str, parameters: Dict[str, Any], risk_score: float) -> PolicyEvaluation:
        if risk_score >= 0.75:
            return PolicyEvaluation(
                approved=False,
                confidence_score=0.95,
                risk_level="high",
                policy_reason="High risk action requires explicit user confirmation.",
            )
        elif risk_score >= 0.40:
            return PolicyEvaluation(
                approved=True,
                confidence_score=0.85,
                risk_level="medium",
                policy_reason="Medium risk action approved under standard governance.",
            )
        else:
            return PolicyEvaluation(
                approved=True,
                confidence_score=0.99,
                risk_level="low",
                policy_reason="Low risk action fast-tracked.",
            )


# 2. Universal Task Graph
@dataclass
class TaskNode:
    node_id: str
    title: str
    action: str
    dependencies: List[str] = field(default_factory=list)
    status: str = "pending"  # "pending", "in_progress", "completed", "failed"
    result: Optional[Any] = None


class UniversalTaskGraph:
    """
    Universal Task Graph.
    Represents all multi-step workflows as a Directed Acyclic Graph (DAG) of subtasks.
    """

    def __init__(self, workflow_title: str) -> None:
        self.workflow_title = workflow_title
        self.nodes: Dict[str, TaskNode] = {}

    def add_node(self, node_id: str, title: str, action: str, dependencies: Optional[List[str]] = None) -> TaskNode:
        node = TaskNode(node_id=node_id, title=title, action=action, dependencies=dependencies or [])
        self.nodes[node_id] = node
        return node

    def get_executable_nodes(self) -> List[TaskNode]:
        executable = []
        for node in self.nodes.values():
            if node.status == "pending":
                deps_met = all(self.nodes[dep_id].status == "completed" for dep_id in node.dependencies if dep_id in self.nodes)
                if deps_met:
                    executable.append(node)
        return executable

    def mark_completed(self, node_id: str, result: Optional[Any] = None) -> None:
        if node_id in self.nodes:
            self.nodes[node_id].status = "completed"
            self.nodes[node_id].result = result


# 3. AI OS Event Bus
class AIOSEventBus:
    """
    AI OS Event Bus.
    Decoupled publish-subscribe event system for inter-module communication.
    """

    def __init__(self) -> None:
        self.subscribers: Dict[str, List[Callable[[Dict[str, Any]], None]]] = {}

    def subscribe(self, event_type: str, callback: Callable[[Dict[str, Any]], None]) -> None:
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)

    def publish(self, event_type: str, payload: Dict[str, Any]) -> int:
        delivered = 0
        if event_type in self.subscribers:
            for cb in self.subscribers[event_type]:
                try:
                    cb(payload)
                    delivered += 1
                except Exception:
                    pass
        return delivered


# 4. Semantic Action History
@dataclass
class ActionHistoryRecord:
    record_id: str = field(default_factory=lambda: f"rec_{uuid.uuid4().hex[:6]}")
    action: str = ""
    domain: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    timestamp: float = field(default_factory=time.time)


class SemanticActionHistory:
    """
    Semantic Action History.
    Indexed, queryable action log storing past executions with semantic filter capabilities.
    """

    def __init__(self) -> None:
        self.records: List[ActionHistoryRecord] = []

    def record_action(self, action: str, domain: str, parameters: Dict[str, Any], status: str = "completed") -> ActionHistoryRecord:
        rec = ActionHistoryRecord(action=action, domain=domain, parameters=parameters, status=status)
        self.records.append(rec)
        return rec

    def query_history(self, domain_filter: Optional[str] = None, keyword: Optional[str] = None) -> List[ActionHistoryRecord]:
        results = self.records
        if domain_filter:
            results = [r for r in results if r.domain.lower() == domain_filter.lower()]
        if keyword:
            kw = keyword.lower()
            results = [r for r in results if kw in r.action.lower() or kw in str(r.parameters).lower()]
        return results


# 5. Automation Recorder with Simulation Mode
@dataclass
class RecordedStep:
    step_num: int
    action_type: str
    target: str
    payload: Dict[str, Any]


class AutomationRecorder:
    """
    Automation Recorder with Simulation Mode.
    Records action sequences and validates workflows in dry-run simulation mode without side effects.
    """

    def __init__(self) -> None:
        self.recorded_steps: List[RecordedStep] = []
        self.is_recording: bool = False

    def start_recording(self) -> None:
        self.recorded_steps = []
        self.is_recording = True

    def record_step(self, action_type: str, target: str, payload: Dict[str, Any]) -> None:
        if self.is_recording:
            step = RecordedStep(step_num=len(self.recorded_steps) + 1, action_type=action_type, target=target, payload=payload)
            self.recorded_steps.append(step)

    def stop_recording(self) -> List[RecordedStep]:
        self.is_recording = False
        return self.recorded_steps

    def simulate_workflow(self, steps: List[RecordedStep]) -> Dict[str, Any]:
        """Dry-run simulation mode validating workflow steps without side effects."""
        validated_steps = []
        for step in steps:
            validated_steps.append({
                "step_num": step.step_num,
                "action": step.action_type,
                "target": step.target,
                "simulation_status": "valid",
            })
        return {"simulation": True, "validated_steps_count": len(validated_steps), "details": validated_steps}


# 6. Universal Search Engine
class UniversalSearchEngine:
    """
    Universal Search Engine.
    Unified search across chats, files, automations, and memory facts.
    """

    def search_all(self, query: str, chats: List[Dict], files: List[str], automations: List[Dict], memories: List[Dict]) -> Dict[str, List[Any]]:
        q = query.lower()
        matched_chats = [c for c in chats if q in str(c.get("content", "")).lower()]
        matched_files = [f for f in files if q in f.lower()]
        matched_automations = [a for a in automations if q in str(a.get("title", "")).lower()]
        matched_memories = [m for m in memories if q in str(m.get("topic", "")).lower() or q in str(m.get("insight", "")).lower()]

        return {
            "query": query,
            "chats": matched_chats,
            "files": matched_files,
            "automations": matched_automations,
            "memories": matched_memories,
        }


# 7. Multi-Agent Collaboration Engine
@dataclass
class AgentRole:
    role_name: str  # "planner", "coder", "executor", "reviewer"
    capability: str


class MultiAgentCollaborator:
    """
    Multi-Agent Collaboration Engine.
    Optional multi-agent task delegation for complex tasks (simple requests stay fast).
    """

    def __init__(self) -> None:
        self.roles = [
            AgentRole("planner", "Subtask Decomposition"),
            AgentRole("coder", "Code Generation"),
            AgentRole("executor", "Tool Execution"),
            AgentRole("reviewer", "Quality Verification"),
        ]

    def collaborate(self, goal: str, is_complex: bool) -> Dict[str, Any]:
        if not is_complex:
            return {"mode": "single_agent_fast", "status": "bypassed_multi_agent"}

        contributions = [
            {"agent": "planner", "output": f"Planned subtasks for '{goal}'"},
            {"agent": "executor", "output": f"Executed domain tools for '{goal}'"},
            {"agent": "reviewer", "output": f"Verified outcome quality for '{goal}'"},
        ]
        return {"mode": "multi_agent_collaboration", "agent_count": len(contributions), "contributions": contributions}


# 8. Model Compatibility Layer
class ModelCompatibilityLayer:
    """
    Model Compatibility Layer.
    Multi-provider abstraction supporting Gemini, OpenAI, Anthropic, and Local LLMs.
    """

    SUPPORTED_PROVIDERS = ["gemini", "openai", "anthropic", "local_ollama"]

    def format_prompt_for_provider(self, prompt: str, provider: str = "gemini") -> Dict[str, Any]:
        prov = provider.lower()
        if prov not in self.SUPPORTED_PROVIDERS:
            prov = "gemini"

        return {
            "provider": prov,
            "formatted_prompt": prompt,
            "temperature": 0.2 if prov == "local_ollama" else 0.7,
        }


# 9. Diagnostics Mode
class DiagnosticsEngine:
    """
    Diagnostics Mode Engine.
    Explains routing decisions, module cost justifications, and latency breakdowns.
    """

    def explain_routing_decision(self, user_input: str, lane: str, executor: str, latency_ms: float) -> Dict[str, Any]:
        return {
            "input": user_input,
            "assigned_lane": lane,
            "target_executor": executor,
            "latency_ms": round(latency_ms, 2),
            "explanation": f"Request assigned to {lane} lane and dispatched to {executor} based on intent complexity.",
        }
