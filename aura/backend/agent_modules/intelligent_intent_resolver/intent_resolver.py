from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

from .models import (
    CLARIFICATION_THRESHOLD,
    COMMON_ENTITIES,
    IntentResolutionResult,
    DisambiguationQuery,
)

logger = logging.getLogger(__name__)


class IntelligentIntentResolver:
    """LLM-powered intent resolver replacing static PHONETIC_CORRECTIONS dict."""

    def __init__(
        self,
        nlp_processor=None,
        reasoning_engine=None,
        multilingual_engine=None,
        llm_client=None,
        context_manager=None,
        cache=None,
        debug: bool = False,
    ) -> None:
        if nlp_processor is None:
            raise ValueError("nlp_processor is required")
        if reasoning_engine is None:
            raise ValueError("reasoning_engine is required")
        if multilingual_engine is None:
            raise ValueError("multilingual_engine is required")
        if llm_client is None:
            raise ValueError("llm_client is required")
        if context_manager is None:
            raise ValueError("context_manager is required")

        self.nlp = nlp_processor
        self.reasoning = reasoning_engine
        self.multilingual = multilingual_engine
        self.llm = llm_client
        self.context_manager = context_manager
        self.cache = cache
        self.debug = debug
        self._model = "openai/gpt-4o-mini"

        # Metrics
        self._total_resolutions = 0
        self._successful_resolutions = 0
        self._disambiguation_count = 0
        self._fallback_count = 0
        self._total_latency_ms = 0.0

    def resolve_intent(
        self,
        raw_transcript: str,
        user_id: str = "default",
        context: Optional[Dict[str, Any]] = None,
    ) -> IntentResolutionResult:
        """Main resolution method coordinating all steps per the design algorithm."""
        start = time.perf_counter()
        self._total_resolutions += 1

        try:
            # 1. Clean input
            cleaned_result = self.nlp.clean_noise(raw_transcript)
            cleaned = cleaned_result.cleaned_prompt if hasattr(cleaned_result, "cleaned_prompt") else str(cleaned_result)

            # 2. Detect language
            lang_result = self.multilingual.detect_language(cleaned)
            language = lang_result.detected_dialect if hasattr(lang_result, "detected_dialect") else "english"

            # 3. Fast path check
            if self.should_use_fast_path(cleaned):
                result = self._fast_path_resolve(cleaned, language)
                elapsed = (time.perf_counter() - start) * 1000
                result.resolution_time_ms = elapsed
                self._log_resolution(raw_transcript, result, elapsed)
                return result

            # 4. Check cache
            user_ctx = context or self.context_manager.get_user_context(user_id)
            cache_key = self._cache_key(cleaned, user_id)
            if self.cache and self.cache.has(cache_key) and self.cache.should_use_cache(cache_key, user_ctx):
                cached = self.cache.get(cache_key)
                cached.resolved_by = "cache"
                elapsed = (time.perf_counter() - start) * 1000
                cached.resolution_time_ms = elapsed
                return cached

            # 5. Build and call LLM
            prompt = self._build_resolution_prompt(cleaned, language, user_ctx)
            parsed = self._call_llm_with_retry(prompt, cleaned)

            # 6. Extract entities and resolve URL
            if parsed.entity_type == "website" and not parsed.canonical_url:
                from .entity_extractor import EntityExtractor
                extractor = EntityExtractor(context_manager=self.context_manager)
                resolved_url = extractor.resolve_website(parsed.target_entity or cleaned, user_ctx)
                parsed.canonical_url = resolved_url

            # 7. Evaluate confidence
            from .confidence_evaluator import ConfidenceEvaluator
            evaluator = ConfidenceEvaluator()
            confidence_score = evaluator.evaluate(parsed, user_ctx)
            parsed.confidence = confidence_score.overall
            parsed.resolution_confidence = confidence_score.overall
            parsed.requires_clarification = confidence_score.requires_clarification

            # 8. Generate clarification if needed
            if parsed.requires_clarification:
                self._disambiguation_count += 1
                query = self._generate_clarification(parsed, language)
                parsed.clarification_options = query.options

            # 9. Extract parameters
            self._extract_parameters(parsed)

            # 10. Apply redaction in logs
            from .redactor import redact_sensitive_data
            safe_input = redact_sensitive_data(raw_transcript)

            # 11. Cache result
            if self.cache:
                self.cache.set(cache_key, parsed)

            # 12. Record for learning
            self.context_manager.record_resolution(user_id, parsed, confirmed=False)

            elapsed = (time.perf_counter() - start) * 1000
            parsed.resolution_time_ms = elapsed
            self._total_latency_ms += elapsed
            self._successful_resolutions += 1
            self._log_resolution(safe_input, parsed, elapsed)
            return parsed

        except TimeoutError as e:
            logger.error("LLM timeout in intent resolution: %s. Using fallback.", e)
            self._fallback_count += 1
            from .fallback_resolver import FallbackIntentResolver
            result = FallbackIntentResolver().resolve_simple(raw_transcript)
            result.resolution_time_ms = (time.perf_counter() - start) * 1000
            return result
        except Exception as e:
            logger.exception("Unexpected error in intent resolution: %s. Using fallback.", e)
            self._fallback_count += 1
            from .fallback_resolver import FallbackIntentResolver
            result = FallbackIntentResolver().resolve_simple(raw_transcript)
            result.resolution_time_ms = (time.perf_counter() - start) * 1000
            result.reasoning_trace = str(e)
            return result

    def should_use_fast_path(self, text: str) -> bool:
        """Return True for direct URLs or well-known entity keywords."""
        import re
        if re.match(r'https?://', text.strip()):
            return True
        if text.strip().lower() in COMMON_ENTITIES:
            return True
        return False

    def _fast_path_resolve(self, text: str, language: str) -> IntentResolutionResult:
        import re
        stripped = text.strip()
        url_match = re.match(r'https?://\S+', stripped)
        if url_match:
            url = url_match.group(0)
            return IntentResolutionResult(
                resolved_intent=f"navigate to {url}",
                target_entity=url,
                entity_type="website",
                canonical_url=url,
                confidence=0.99,
                resolved_by="fast_path",
                language=language,
            )
        entity = stripped.lower()
        url = COMMON_ENTITIES.get(entity, "")
        return IntentResolutionResult(
            resolved_intent=f"navigate to {entity.title()}",
            target_entity=entity.title(),
            entity_type="website",
            canonical_url=url,
            confidence=0.98,
            resolved_by="fast_path",
            language=language,
        )

    def _build_resolution_prompt(
        self,
        cleaned_input: str,
        language: str,
        user_context: Dict[str, Any],
    ) -> str:
        frequent = user_context.get("frequent_entities", [])[:5]
        recent = [e[0] if isinstance(e, tuple) else e for e in user_context.get("recent_entities", [])[:5]]
        workflow = user_context.get("active_workflow", "None")
        prompt = f"""You are an intelligent intent resolver for a voice assistant.

USER INPUT: "{cleaned_input}"
DETECTED LANGUAGE: {language}

YOUR TASK:
1. Understand the user intent from their speech input
2. Identify any website, application, or action they want to access
3. Resolve entity names to canonical forms (e.g., "you tube" -> "YouTube")
4. Extract any parameters or search terms
5. Provide confidence in your resolution (0.0 to 1.0)

USER CONTEXT:
- Frequently accessed: {frequent}
- Recent activity: {recent}
- Active workflow: {workflow}

OUTPUT FORMAT (JSON only, no extra text):
{{
  "intent": "brief description of user intent",
  "entity_name": "canonical entity name or null",
  "entity_type": "website | application | action | query",
  "canonical_url": "full URL if website, else null",
  "parameters": {{}},
  "confidence": 0.0-1.0,
  "reasoning": "explain your interpretation",
  "alternatives": ["alternative interpretation 1"]
}}

EXAMPLES:
Input: "open code chef"
Output: {{"intent": "navigate to CodeChef", "entity_name": "CodeChef", "entity_type": "website", "canonical_url": "https://www.codechef.com", "parameters": {{}}, "confidence": 0.95, "reasoning": "User wants to open CodeChef website", "alternatives": []}}

Input: "youtube lo cooking videos search chey"
Output: {{"intent": "search cooking videos on YouTube", "entity_name": "YouTube", "entity_type": "website", "canonical_url": "https://www.youtube.com", "parameters": {{"search_query": "cooking videos"}}, "confidence": 0.92, "reasoning": "Telglish: youtube + search cooking videos", "alternatives": []}}"""
        return prompt

    def _call_llm_with_retry(self, prompt: str, raw: str) -> IntentResolutionResult:
        """Call LLM with one retry on JSON parse failure, then fallback."""
        for attempt in range(2):
            try:
                response = self.llm.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=500,
                )
                content = response.choices[0].message.content or ""
                return self._parse_llm_response(content)
            except json.JSONDecodeError as e:
                if attempt == 0:
                    logger.warning("JSON parse error on attempt 1, retrying: %s", e)
                    continue
                logger.error("JSON parse error on retry, using fallback")
                from .fallback_resolver import FallbackIntentResolver
                return FallbackIntentResolver().resolve_simple(raw)
            except Exception as e:
                logger.error("LLM API call failed: %s", e)
                from .fallback_resolver import FallbackIntentResolver
                return FallbackIntentResolver().resolve_simple(raw)
        from .fallback_resolver import FallbackIntentResolver
        return FallbackIntentResolver().resolve_simple(raw)

    def _parse_llm_response(self, content: str) -> IntentResolutionResult:
        """Parse JSON from LLM response into IntentResolutionResult."""
        import re
        # Extract JSON block if wrapped in markdown
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            raise json.JSONDecodeError("No JSON found", content, 0)
        data = json.loads(json_match.group(0))
        return IntentResolutionResult(
            resolved_intent=data.get("intent", ""),
            target_entity=data.get("entity_name"),
            entity_type=data.get("entity_type", "query"),
            canonical_url=data.get("canonical_url"),
            confidence=float(data.get("confidence", 0.5)),
            parameters=data.get("parameters", {}),
            reasoning_trace=data.get("reasoning", "") if self.debug else "",
            alternatives=data.get("alternatives", []),
            resolved_by="llm",
        )

    def _extract_parameters(self, result: IntentResolutionResult) -> None:
        """Ensure target_entity is separate from parameters dict."""
        if result.target_entity and "entity" in result.parameters:
            del result.parameters["entity"]
        # Detect missing required params
        if result.entity_type == "website" and "search" in result.resolved_intent.lower():
            if not result.parameters.get("search_query"):
                result.requires_clarification = True

    def _generate_clarification(
        self, result: IntentResolutionResult, language: str
    ) -> DisambiguationQuery:
        options = []
        if result.target_entity:
            opt = result.target_entity
            if result.canonical_url:
                opt += f" ({result.canonical_url})"
            options.append(opt)
        for alt in result.alternatives[:2]:
            if alt and alt not in options:
                options.append(str(alt))
        if len(options) < 2:
            options.append("Something else (please specify)")

        if "telugu" in language.lower() or "te" in language.lower():
            question = "మీరు ఏది కావాలి? (Which one do you want?)"
        else:
            question = "Did you mean:"
        return DisambiguationQuery(
            question=question,
            options=options[:3],
            original_input=result.resolved_intent,
        )

    def _cache_key(self, text: str, user_id: str) -> str:
        normalized = " ".join(text.lower().split())
        return f"intent:{user_id}:{hashlib.md5(normalized.encode()).hexdigest()}"

    def _log_resolution(
        self, raw: str, result: IntentResolutionResult, elapsed_ms: float
    ) -> None:
        log_data = {
            "raw_input": raw,
            "resolved_intent": result.resolved_intent,
            "confidence": result.confidence,
            "resolution_time_ms": round(elapsed_ms, 2),
            "resolved_by": result.resolved_by,
            "language": result.language,
        }
        if result.confidence < CLARIFICATION_THRESHOLD:
            logger.warning("Low confidence resolution: %s", log_data)
        else:
            logger.info("Intent resolved: %s", log_data)
        if self.debug and result.reasoning_trace:
            logger.debug("Reasoning trace: %s", result.reasoning_trace)

    def get_metrics(self) -> Dict[str, Any]:
        avg_latency = (
            self._total_latency_ms / self._successful_resolutions
            if self._successful_resolutions > 0 else 0.0
        )
        return {
            "total_resolutions": self._total_resolutions,
            "success_rate": self._successful_resolutions / max(1, self._total_resolutions),
            "disambiguation_rate": self._disambiguation_count / max(1, self._total_resolutions),
            "fallback_rate": self._fallback_count / max(1, self._total_resolutions),
            "average_latency_ms": round(avg_latency, 2),
        }
