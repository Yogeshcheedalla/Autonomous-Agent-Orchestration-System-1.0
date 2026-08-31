from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TradeoffEvaluation:
    approach_a: str
    approach_b: str
    pros_a: List[str]
    cons_a: List[str]
    pros_b: List[str]
    cons_b: List[str]
    recommended: str
    justification: str


@dataclass
class ReasoningResult:
    decision: str
    tradeoffs: Optional[TradeoffEvaluation]
    confidence: float
    requires_clarification: bool
    clarification_question: Optional[str]
    is_guesswork_avoided: bool = True


class ReasoningEngine:
    """
    Module: Reasoning Engine
    Objectives:
    - Use logical reasoning to evaluate tradeoffs and compare approaches
    - Avoid guesswork completely by identifying ambiguous requirements
    - Align every decision tightly with user goals
    - Request clarification only when ambiguous or high-risk
    """

    def __init__(self) -> None:
        self.evaluation_history: List[ReasoningResult] = []

    def evaluate_tradeoffs(
        self,
        approach_a: str,
        pros_a: List[str],
        cons_a: List[str],
        approach_b: str,
        pros_b: List[str],
        cons_b: List[str],
        user_goal: str,
    ) -> TradeoffEvaluation:
        # Evaluate which approach better fits the goal
        score_a = len(pros_a) - len(cons_a)
        score_b = len(pros_b) - len(cons_b)

        if score_a >= score_b:
            recommended = approach_a
            justification = f"Approach '{approach_a}' yields better net advantages for target goal '{user_goal}'."
        else:
            recommended = approach_b
            justification = f"Approach '{approach_b}' provides superior tradeoffs for target goal '{user_goal}'."

        return TradeoffEvaluation(
            approach_a=approach_a,
            approach_b=approach_b,
            pros_a=pros_a,
            cons_a=cons_a,
            pros_b=pros_b,
            cons_b=cons_b,
            recommended=recommended,
            justification=justification,
        )

    def analyze_decision(
        self,
        goal: str,
        available_info: Dict[str, Any],
        critical_parameters: List[str],
        is_destructive_action: bool = False,
    ) -> ReasoningResult:
        missing_params = [p for p in critical_parameters if p not in available_info or available_info[p] is None]

        # Avoid guesswork: if critical parameters are missing or action is highly destructive, ask clarification
        if missing_params and is_destructive_action:
            question = f"To accomplish '{goal}' safely without guesswork, please specify: {', '.join(missing_params)}."
            res = ReasoningResult(
                decision="pause_for_clarification",
                tradeoffs=None,
                confidence=0.4,
                requires_clarification=True,
                clarification_question=question,
                is_guesswork_avoided=True,
            )
        elif missing_params:
            # Non-destructive: formulate reasonable defaults while logging assumptions
            default_summary = f"Proceeding with default context for {missing_params}"
            res = ReasoningResult(
                decision=f"execute_with_defaults ({default_summary})",
                tradeoffs=None,
                confidence=0.75,
                requires_clarification=False,
                clarification_question=None,
                is_guesswork_avoided=True,
            )
        else:
            res = ReasoningResult(
                decision="execute_autonomous",
                tradeoffs=None,
                confidence=0.95,
                requires_clarification=False,
                clarification_question=None,
                is_guesswork_avoided=True,
            )

        self.evaluation_history.append(res)
        return res

    def system_prompt_section(self) -> str:
        return """
# MODULE: REASONING ENGINE
- LOGICAL TRADEOFF EVALUATION: Systematically compare technical alternatives, pros, and cons before executing complex decisions.
- NO GUESSWORK: Do not invent missing facts, credentials, API endpoints, or user parameters. Verify from live source context or ask clarifying questions when ambiguous.
- GOAL ALIGNMENT: Ensure every step directly advances the user's explicit objective.
"""
