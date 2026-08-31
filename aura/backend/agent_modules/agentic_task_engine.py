"""
AgenticTaskEngine — Universal Research-First Task Planner for Akansha AI OS.

For ANY task the user gives, this engine does:
  1. UNDERSTAND  — extract goal, context, constraints, unknowns, success criteria
  2. RESEARCH    — gather all facts needed before touching anything
  3. PLAN        — build a numbered step-by-step execution plan with verifications
  4. EXECUTE     — run each step, verify results, recover from errors
  5. CONTINUE    — never stop until the goal is verifiably complete

This replaces the old _default_decomposition() heuristics.
It is completely domain-agnostic — the same engine handles LeetCode, GitHub,
research tasks, file operations, creative writing, deployment, and anything else.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Task category keywords ────────────────────────────────────────────────────

_CODING_KWS = {
    "leetcode", "code", "solve", "implement", "debug", "fix", "refactor",
    "write", "algorithm", "function", "program", "class", "test", "java",
    "python", "javascript", "typescript", "c++", "rust", "go", "sql",
    "codechef", "hackerrank", "codeforces", "geeksforgeeks",
}
_BROWSER_KWS = {
    "open", "navigate", "browse", "search", "youtube", "github", "linkedin",
    "coursera", "website", "url", "click", "fill", "form", "login",
    "download", "scrape", "extract", "find on", "go to",
}
_RESEARCH_KWS = {
    "research", "find", "what is", "explain", "summarize", "analyze",
    "compare", "difference", "best", "how to", "why", "when", "who",
    "documentation", "learn", "study", "understand",
}
_FILE_KWS = {
    "file", "folder", "directory", "create", "move", "rename", "delete",
    "copy", "read", "write file", "save", "export", "import", "pdf",
    "excel", "csv", "json", "zip",
}
_DEPLOY_KWS = {
    "deploy", "docker", "kubernetes", "aws", "gcp", "azure", "build",
    "ci/cd", "pipeline", "server", "host", "publish", "release", "git",
    "commit", "push", "pull request", "merge",
}
_AUTOMATION_KWS = {
    "automate", "schedule", "every", "daily", "repeat", "monitor",
    "watch", "trigger", "when", "if then", "workflow", "batch",
}
_CREATIVE_KWS = {
    "write", "generate", "create", "design", "draft", "compose", "make",
    "report", "presentation", "email", "essay", "story", "article",
    "template", "diagram",
}


def detect_task_category(goal: str) -> str:
    g = goal.lower()
    scores: Dict[str, int] = {
        "coding":     sum(1 for kw in _CODING_KWS     if kw in g),
        "browser":    sum(1 for kw in _BROWSER_KWS    if kw in g),
        "research":   sum(1 for kw in _RESEARCH_KWS   if kw in g),
        "file":       sum(1 for kw in _FILE_KWS       if kw in g),
        "deploy":     sum(1 for kw in _DEPLOY_KWS     if kw in g),
        "automation": sum(1 for kw in _AUTOMATION_KWS if kw in g),
        "creative":   sum(1 for kw in _CREATIVE_KWS   if kw in g),
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "general"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class TaskUnderstanding:
    goal_summary: str
    category: str
    constraints: List[str]
    unknowns: List[str]
    success_criteria: List[str]
    estimated_steps: int
    requires_browser: bool
    requires_file_system: bool
    requires_code_execution: bool
    requires_live_web: bool
    language_preference: str
    raw_goal: str


@dataclass
class ResearchResult:
    facts_gathered: List[str]
    relevant_context: str
    constraints_resolved: List[str]
    open_questions: List[str]
    research_sources: List[str]


@dataclass
class AgenticStep:
    id: str
    index: int
    title: str
    description: str
    action: str
    params: Dict[str, Any]
    depends_on: List[str]
    verification: str
    voice_prompt: str
    status: str = "pending"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


@dataclass
class AgenticPlan:
    plan_id: str
    goal: str
    understanding: TaskUnderstanding
    research: ResearchResult
    steps: List[AgenticStep]
    total_steps: int
    category: str
    created_at: float = field(default_factory=time.time)
    status: str = "ready"

    def next_pending_step(self) -> Optional[AgenticStep]:
        return next((s for s in self.steps if s.status == "pending"), None)

    def all_complete(self) -> bool:
        return all(s.status in ("completed", "skipped") for s in self.steps)

    def has_failures(self) -> bool:
        return any(s.status == "failed" for s in self.steps)

    def summary(self) -> str:
        done = sum(1 for s in self.steps if s.status == "completed")
        return f"{done}/{self.total_steps} steps completed"


# ── LLM call ─────────────────────────────────────────────────────────────────

def _call_llm(prompt: str, temperature: float = 0.3, max_tokens: int = 2000) -> str:
    try:
        from openai import OpenAI
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            return ""
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            timeout=20.0,
            max_retries=0,
        )
        model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("AgenticTaskEngine LLM call failed: %s", exc)
        return ""


def _parse_json_from_llm(text: str) -> Any:
    match = re.search(r'```json\s*([\s\S]+?)\s*```', text)
    if match:
        text = match.group(1)
    else:
        match = re.search(r'(\{[\s\S]+\}|\[[\s\S]+\])', text)
        if match:
            text = match.group(1)
    try:
        return json.loads(text)
    except Exception:
        return None


# ── UNDERSTAND phase ──────────────────────────────────────────────────────────

_UNDERSTAND_PROMPT = '''You are an expert AI task analyst. Analyze this user task and return a JSON object.

USER TASK: "{goal}"

Return JSON with these exact fields:
{{
  "goal_summary": "one clear sentence describing the goal",
  "category": "one of: coding, browser, research, file, deploy, automation, creative, general",
  "constraints": ["list every constraint, rule, or requirement mentioned"],
  "unknowns": ["things we must clarify or research before executing — be specific"],
  "success_criteria": ["specific measurable conditions that confirm task is done"],
  "estimated_steps": 5,
  "requires_browser": false,
  "requires_file_system": false,
  "requires_code_execution": false,
  "requires_live_web": false,
  "language_preference": ""
}}

Examples of good constraints for different task types:
- LeetCode: "non-premium account", "Java only", "skip already-solved", "only problems 2000-3000"
- Research: "use official sources only", "include citations", "current year data"
- Deploy: "do not break production", "run tests before pushing", "feature branch only"
- File task: "do not overwrite originals", "create backup first"
- Browser: "stay logged in as current user", "do not change account settings"

Return ONLY the JSON object.'''


def understand_task(goal: str, context: Optional[str] = None) -> TaskUnderstanding:
    prompt = _UNDERSTAND_PROMPT.format(goal=goal)
    if context:
        prompt += f"\n\nADDITIONAL CONTEXT: {context}"
    raw = _call_llm(prompt, temperature=0.2, max_tokens=1200)
    data = _parse_json_from_llm(raw)
    if not data or not isinstance(data, dict):
        category = detect_task_category(goal)
        return TaskUnderstanding(
            goal_summary=goal, category=category, constraints=[],
            unknowns=["Proceeding with best effort — no LLM research was run"],
            success_criteria=["Task completed without errors"], estimated_steps=5,
            requires_browser=category == "browser",
            requires_file_system=category == "file",
            requires_code_execution=category in ("coding", "deploy"),
            requires_live_web=category in ("browser", "research"),
            language_preference="", raw_goal=goal,
        )
    return TaskUnderstanding(
        goal_summary=data.get("goal_summary", goal),
        category=data.get("category", detect_task_category(goal)),
        constraints=data.get("constraints", []),
        unknowns=data.get("unknowns", []),
        success_criteria=data.get("success_criteria", ["Task completed"]),
        estimated_steps=int(data.get("estimated_steps", 5)),
        requires_browser=bool(data.get("requires_browser", False)),
        requires_file_system=bool(data.get("requires_file_system", False)),
        requires_code_execution=bool(data.get("requires_code_execution", False)),
        requires_live_web=bool(data.get("requires_live_web", False)),
        language_preference=str(data.get("language_preference", "")),
        raw_goal=goal,
    )


# ── RESEARCH phase ────────────────────────────────────────────────────────────

_RESEARCH_PROMPT = '''You are an expert AI researcher. Based on the task analysis below, identify all facts and context needed to execute this task correctly.

TASK GOAL: {goal}
CATEGORY: {category}
CONSTRAINTS: {constraints}
UNKNOWNS TO RESOLVE: {unknowns}
SUCCESS CRITERIA: {success_criteria}
{memory_section}

Return JSON with these exact fields:
{{
  "facts_gathered": ["key facts needed to execute — be specific and actionable"],
  "relevant_context": "a paragraph giving full context for the executor",
  "constraints_resolved": ["constraints that are now fully understood"],
  "open_questions": ["questions that MUST be answered by the user before a specific step can run — only include genuinely ambiguous questions"],
  "research_sources": ["how this info was gathered"]
}}

For any task, always include in facts_gathered:
- What could go wrong and how to prevent it
- How to verify each major step succeeded
- What the final verification should check

Return ONLY the JSON object.'''


def research_task(understanding: TaskUnderstanding, memory_context: str = "") -> ResearchResult:
    memory_section = f"\nMEMORY CONTEXT:\n{memory_context}" if memory_context else ""
    prompt = _RESEARCH_PROMPT.format(
        goal=understanding.goal_summary,
        category=understanding.category,
        constraints=json.dumps(understanding.constraints),
        unknowns=json.dumps(understanding.unknowns),
        success_criteria=json.dumps(understanding.success_criteria),
        memory_section=memory_section,
    )
    raw = _call_llm(prompt, temperature=0.3, max_tokens=1500)
    data = _parse_json_from_llm(raw)
    if not data or not isinstance(data, dict):
        return ResearchResult(
            facts_gathered=[f"Task category: {understanding.category}", f"Goal: {understanding.goal_summary}"],
            relevant_context=f"Executing: {understanding.raw_goal}",
            constraints_resolved=understanding.constraints,
            open_questions=understanding.unknowns,
            research_sources=["heuristic fallback"],
        )
    return ResearchResult(
        facts_gathered=data.get("facts_gathered", []),
        relevant_context=data.get("relevant_context", ""),
        constraints_resolved=data.get("constraints_resolved", []),
        open_questions=data.get("open_questions", []),
        research_sources=data.get("research_sources", []),
    )


# ── PLAN phase ────────────────────────────────────────────────────────────────

_PLAN_PROMPT = '''You are an expert AI task planner. Create an unbreakable execution plan.

TASK GOAL: {goal}
CATEGORY: {category}
CONSTRAINTS: {constraints}
SUCCESS CRITERIA: {success_criteria}
REQUIRES BROWSER: {requires_browser}
REQUIRES CODE EXECUTION: {requires_code_execution}
LANGUAGE: {language_preference}

RESEARCH CONTEXT:
{relevant_context}

KEY FACTS:
{facts}

Return a JSON array of step objects. Each step:
{{
  "title": "short step title",
  "description": "detailed description of exactly what to do",
  "action": "one of: llm_call, browser, shell, file_read, file_write, verify, ask_user, direct_qa",
  "params": {{"key": "value"}},
  "depends_on": ["step_1"],
  "verification": "exactly how to verify this step succeeded",
  "voice_prompt": "natural spoken update <=12 words"
}}

MANDATORY RULES:
1. First step MUST be "verify_prerequisites" — check everything is in place
2. Last step MUST be "verify_completion" — confirm all success criteria are met
3. Include "ask_user" steps ONLY for genuinely ambiguous decisions
4. For coding tasks: include generate-code, run/compile, test, fix-errors, submit steps
5. For browser tasks: navigate, find-element, interact, verify-page-state steps
6. For research tasks: gather-sources, cross-check, synthesize, cite steps
7. Never assume a step succeeded — always verify

Return ONLY the JSON array.'''


def plan_task(understanding: TaskUnderstanding, research: ResearchResult) -> List[AgenticStep]:
    facts_str = "\n".join(f"- {f}" for f in research.facts_gathered[:15])
    prompt = _PLAN_PROMPT.format(
        goal=understanding.goal_summary,
        category=understanding.category,
        constraints=json.dumps(understanding.constraints),
        success_criteria=json.dumps(understanding.success_criteria),
        requires_browser=understanding.requires_browser,
        requires_code_execution=understanding.requires_code_execution,
        language_preference=understanding.language_preference or "any",
        relevant_context=research.relevant_context[:800],
        facts=facts_str,
    )
    raw = _call_llm(prompt, temperature=0.2, max_tokens=2500)
    data = _parse_json_from_llm(raw)
    if not data or not isinstance(data, list):
        return _fallback_plan(understanding, research)
    steps: List[AgenticStep] = []
    for idx, s in enumerate(data):
        step_id = f"step_{idx + 1}"
        steps.append(AgenticStep(
            id=step_id, index=idx + 1,
            title=s.get("title", f"Step {idx + 1}"),
            description=s.get("description", ""),
            action=s.get("action", "direct_qa"),
            params=s.get("params", {}),
            depends_on=s.get("depends_on", []),
            verification=s.get("verification", "Check step output is correct"),
            voice_prompt=s.get("voice_prompt", f"Step {idx + 1}."),
        ))
    return steps if steps else _fallback_plan(understanding, research)


def _fallback_plan(understanding: TaskUnderstanding, research: ResearchResult) -> List[AgenticStep]:
    return [
        AgenticStep(
            id="step_1", index=1, title="Verify Prerequisites",
            description=f"Check all required tools, access, and context for: {understanding.goal_summary}",
            action="verify", params={"check": "prerequisites", "goal": understanding.raw_goal},
            depends_on=[], verification="All required resources are available",
            voice_prompt="Checking prerequisites.",
        ),
        AgenticStep(
            id="step_2", index=2, title="Execute Main Task",
            description=understanding.goal_summary,
            action="direct_qa" if not understanding.requires_browser else "browser",
            params={"goal": understanding.raw_goal, "context": research.relevant_context[:300]},
            depends_on=["step_1"],
            verification="; ".join(understanding.success_criteria[:2]),
            voice_prompt="Working on main task.",
        ),
        AgenticStep(
            id="step_3", index=3, title="Verify Completion",
            description=f"Confirm success criteria: {'; '.join(understanding.success_criteria[:3])}",
            action="verify", params={"criteria": understanding.success_criteria},
            depends_on=["step_2"],
            verification="All success criteria confirmed",
            voice_prompt="Verifying completion.",
        ),
    ]


# ── AgenticTaskEngine ─────────────────────────────────────────────────────────

class AgenticTaskEngine:
    """
    Universal Research-First Task Engine for Akansha.
    Works for ANY task type: coding, browser, research, file, deploy, creative, general.
    Always runs UNDERSTAND → RESEARCH → PLAN before any execution.
    """

    def __init__(self) -> None:
        self._plans: Dict[str, AgenticPlan] = {}

    def create_plan(
        self,
        goal: str,
        context: Optional[str] = None,
        memory_context: str = "",
    ) -> AgenticPlan:
        """Full pipeline: UNDERSTAND → RESEARCH → PLAN. Returns ready-to-execute AgenticPlan."""
        logger.info("AgenticTaskEngine: planning goal=%r", goal[:80])
        start = time.perf_counter()

        understanding = understand_task(goal, context)
        logger.info("AgenticTaskEngine: understood category=%s", understanding.category)

        research = research_task(understanding, memory_context)
        logger.info("AgenticTaskEngine: researched %d facts, %d questions",
                    len(research.facts_gathered), len(research.open_questions))

        steps = plan_task(understanding, research)
        logger.info("AgenticTaskEngine: planned %d steps", len(steps))

        plan = AgenticPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:8]}",
            goal=goal, understanding=understanding, research=research,
            steps=steps, total_steps=len(steps), category=understanding.category,
        )
        self._plans[plan.plan_id] = plan

        elapsed = (time.perf_counter() - start) * 1000
        logger.info("AgenticTaskEngine: plan ready in %.0fms — %d steps", elapsed, len(steps))
        return plan

    def to_jarvis_steps(self, plan: AgenticPlan) -> List[Dict[str, Any]]:
        """Convert AgenticPlan to ContinuousVoiceJarvisEngine step dicts."""
        return [
            {
                "description": f"[{s.title}] {s.description}",
                "action": s.action,
                "params": {
                    **s.params,
                    "_agentic_step_id": s.id,
                    "_verification": s.verification,
                    "_depends_on": s.depends_on,
                    "_category": plan.category,
                    "_goal": plan.goal,
                    "_research_context": plan.research.relevant_context[:300],
                },
                "voice_prompt": s.voice_prompt,
            }
            for s in plan.steps
        ]

    def get_plan_summary(self, plan: AgenticPlan) -> str:
        u = plan.understanding
        lines = [
            f"**Goal:** {u.goal_summary}",
            f"**Category:** {u.category.title()}",
        ]
        if u.constraints:
            lines.append("**Constraints:** " + "; ".join(u.constraints[:3]))
        if plan.research.open_questions:
            lines.append("**Need to clarify:** " + "; ".join(plan.research.open_questions[:2]))
        lines.append(f"**Plan:** {len(plan.steps)} steps")
        for step in plan.steps[:5]:
            lines.append(f"  {step.index}. {step.title}")
        if len(plan.steps) > 5:
            lines.append(f"  ... and {len(plan.steps) - 5} more steps")
        return "\n".join(lines)

    def get_voice_intro(self, plan: AgenticPlan) -> str:
        u = plan.understanding
        n = len(plan.steps)
        parts = [f"Got it. This is a {u.category} task."]
        if u.constraints:
            parts.append(f"I see {len(u.constraints)} constraints to follow.")
        if plan.research.open_questions:
            q = plan.research.open_questions[0]
            parts.append(f"One question: {q}")
            return " ".join(parts)
        parts.append(f"I have a {n}-step plan. Starting now.")
        return " ".join(parts)

    def get_blocking_question(self, plan: AgenticPlan) -> Optional[str]:
        """Return the first question that must be answered before starting."""
        if plan.research.open_questions:
            return plan.research.open_questions[0]
        ask_steps = [s for s in plan.steps if s.action == "ask_user"]
        if ask_steps:
            return ask_steps[0].params.get("question", ask_steps[0].description)
        return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_engine_instance: Optional[AgenticTaskEngine] = None


def get_agentic_engine() -> AgenticTaskEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = AgenticTaskEngine()
    return _engine_instance
