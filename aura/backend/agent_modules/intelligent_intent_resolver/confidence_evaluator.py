"""ConfidenceEvaluator — scores resolution certainty and triggers disambiguation."""
from __future__ import annotations
from typing import Any, Dict
from .models import ConfidenceScore, IntentResolutionResult, CLARIFICATION_THRESHOLD


class ConfidenceEvaluator:
    def evaluate(self, resolution: IntentResolutionResult, context: Dict[str, Any]) -> ConfidenceScore:
        # Entity confidence (40%)
        entity_confidence = 0.9 if resolution.target_entity else 0.3
        alternatives = resolution.alternatives or []
        if len(alternatives) > 1:
            entity_confidence *= max(0.6, 1.0 - 0.15 * len(alternatives))
        # Boost if entity in user history
        frequent = context.get("frequent_entities", [])
        recent = [e[0] if isinstance(e, (list, tuple)) else e
                  for e in context.get("recent_entities", [])]
        all_history = list(frequent) + recent
        if resolution.target_entity and any(
            resolution.target_entity.lower() in str(h).lower() for h in all_history
        ):
            entity_confidence = min(1.0, entity_confidence * 1.1)
        entity_confidence = min(1.0, entity_confidence)

        # Parameter confidence (30%)
        params = resolution.parameters or {}
        required = self._required_params(resolution.entity_type, resolution.resolved_intent)
        param_confidence = len([p for p in required if p in params]) / len(required) if required else 1.0

        # Context alignment (20%)
        context_score = 0.5
        workflow = context.get("active_workflow")
        if workflow and resolution.target_entity:
            context_score = 0.9
        elif resolution.target_entity and any(
            str(e).lower() in (resolution.target_entity or "").lower()
            for e in recent
        ):
            context_score = max(context_score, 0.8)

        # LLM certainty (10%)
        llm_score = getattr(resolution, "confidence", 0.5)

        overall = (
            0.4 * entity_confidence +
            0.3 * param_confidence +
            0.2 * context_score +
            0.1 * llm_score
        )
        overall = min(1.0, max(0.0, overall))
        requires_clarification = overall < CLARIFICATION_THRESHOLD
        risk_level = "low"
        if requires_clarification and self._is_irreversible(resolution):
            risk_level = "high"
        elif requires_clarification:
            risk_level = "medium"
        return ConfidenceScore(
            overall=overall,
            entity_confidence=entity_confidence,
            parameter_confidence=param_confidence,
            context_alignment=context_score,
            requires_clarification=requires_clarification,
            risk_level=risk_level,
        )

    def _required_params(self, entity_type: str, intent: str) -> list:
        intent_lower = (intent or "").lower()
        if entity_type == "website" and "search" in intent_lower:
            return ["search_query"]
        if entity_type == "action" and "file" in intent_lower:
            return ["file_path"]
        return []

    def _is_irreversible(self, resolution: IntentResolutionResult) -> bool:
        irreversible_keywords = ["delete", "submit", "publish", "payment", "deploy"]
        text = (resolution.resolved_intent or "").lower()
        return any(kw in text for kw in irreversible_keywords)
