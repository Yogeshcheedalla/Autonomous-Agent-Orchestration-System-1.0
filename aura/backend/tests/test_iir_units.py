"""Unit tests for IIR — FallbackIntentResolver, ConfidenceEvaluator, EntityExtractor, IntentContextManager."""
import sys, os, unittest, importlib.util

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

_IIR_DIR = os.path.join(_BASE, "agent_modules", "intelligent_intent_resolver")

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_IIR_DIR, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_models = _load("iir.models", "models.py")
_fallback = _load("iir.fallback", "fallback_resolver.py")
_conf = _load("iir.confidence", "confidence_evaluator.py")
_entity = _load("iir.entity", "entity_extractor.py")
_context = _load("iir.context", "intent_context_manager.py")
_cache = _load("iir.cache", "intent_cache.py")
_redact = _load("iir.redact", "redactor.py")

FallbackIntentResolver = _fallback.FallbackIntentResolver
ConfidenceEvaluator = _conf.ConfidenceEvaluator
EntityExtractor = _entity.EntityExtractor
IntentContextManager = _context.IntentContextManager
IntentCache = _cache.IntentCache
redact_sensitive_data = _redact.redact_sensitive_data
IntentResolutionResult = _models.IntentResolutionResult
CLARIFICATION_THRESHOLD = _models.CLARIFICATION_THRESHOLD


class TestFallbackIntentResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = FallbackIntentResolver()

    def test_url_extraction_http(self):
        result = self.resolver.resolve_simple("open https://www.github.com now")
        self.assertEqual(result.entity_type, "website")
        self.assertIn("github.com", result.canonical_url or "")
        self.assertGreater(result.confidence, 0.7)

    def test_url_extraction_bare_domain(self):
        result = self.resolver.resolve_simple("go to leetcode.com")
        self.assertEqual(result.entity_type, "website")
        self.assertGreater(result.confidence, 0.5)

    def test_keyword_matching_youtube(self):
        result = self.resolver.resolve_simple("open youtube and search cooking")
        self.assertEqual(result.entity_type, "website")
        self.assertIn("youtube.com", result.canonical_url or "")

    def test_keyword_matching_github(self):
        result = self.resolver.resolve_simple("navigate to github repos")
        self.assertIn("github.com", result.canonical_url or "")

    def test_generic_fallback(self):
        result = self.resolver.resolve_simple("play some music please")
        self.assertEqual(result.entity_type, "query")
        self.assertLess(result.confidence, 0.5)
        self.assertTrue(len(result.reasoning_trace) > 0)

    def test_empty_input(self):
        result = self.resolver.resolve_simple("")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, IntentResolutionResult)

    def test_resolved_by_fallback(self):
        result = self.resolver.resolve_simple("random text")
        self.assertEqual(result.resolved_by, "fallback")


class TestConfidenceEvaluator(unittest.TestCase):
    def setUp(self):
        self.evaluator = ConfidenceEvaluator()

    def _make_resolution(self, entity=None, confidence=0.8, entity_type="website",
                          intent="navigate", params=None, alts=None):
        return IntentResolutionResult(
            resolved_intent=intent,
            target_entity=entity,
            entity_type=entity_type,
            canonical_url=None,
            confidence=confidence,
            parameters=params or {},
            alternatives=alts or [],
        )

    def test_high_confidence_no_clarification(self):
        res = self._make_resolution(entity="YouTube", confidence=0.95)
        score = self.evaluator.evaluate(res, {})
        self.assertFalse(score.requires_clarification)
        self.assertGreater(score.overall, CLARIFICATION_THRESHOLD)

    def test_low_confidence_triggers_clarification(self):
        res = self._make_resolution(entity=None, confidence=0.2, entity_type="query")
        score = self.evaluator.evaluate(res, {})
        self.assertTrue(score.requires_clarification)
        self.assertLess(score.overall, CLARIFICATION_THRESHOLD)

    def test_history_boost(self):
        res = self._make_resolution(entity="YouTube", confidence=0.85)
        ctx = {"frequent_entities": ["YouTube"], "recent_entities": []}
        score_with = self.evaluator.evaluate(res, ctx)
        score_without = self.evaluator.evaluate(res, {})
        self.assertGreaterEqual(score_with.overall, score_without.overall)

    def test_ambiguity_penalty(self):
        res = self._make_resolution(entity="Youtube", confidence=0.7,
                                     alts=["YouTube Gaming", "YouTube Music", "YouTube Shorts"])
        score = self.evaluator.evaluate(res, {})
        self.assertLessEqual(score.entity_confidence, 0.85)


class TestEntityExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = EntityExtractor()

    def test_direct_url(self):
        entities = self.extractor.extract_entities("https://www.youtube.com")
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].url, "https://www.youtube.com")
        self.assertGreater(entities[0].confidence, 0.95)

    def test_known_entity(self):
        entities = self.extractor.extract_entities("open github")
        names = [e.canonical_name.lower() for e in entities]
        self.assertTrue(any("github" in n for n in names))

    def test_resolve_website_corrections(self):
        ctx = {"corrections": {"u tube": "https://www.youtube.com"}, "recent_entities": []}
        url = self.extractor.resolve_website("u tube", ctx)
        self.assertEqual(url, "https://www.youtube.com")

    def test_resolve_website_common_entity(self):
        ctx = {"corrections": {}, "recent_entities": []}
        url = self.extractor.resolve_website("youtube", ctx)
        self.assertIn("youtube.com", url or "")

    def test_resolve_website_unknown(self):
        ctx = {"corrections": {}, "recent_entities": []}
        url = self.extractor.resolve_website("somerandomunknownsite123", ctx)
        self.assertIsNone(url)


class TestIntentContextManager(unittest.TestCase):
    def setUp(self):
        self.mgr = IntentContextManager()

    def test_get_user_context_default(self):
        ctx = self.mgr.get_user_context("user1")
        self.assertIn("user_id", ctx)
        self.assertIn("frequent_entities", ctx)
        self.assertIn("recent_entities", ctx)
        self.assertIn("corrections", ctx)

    def test_record_resolution_updates_history(self):
        from unittest.mock import MagicMock
        res = MagicMock()
        res.target_entity = "YouTube"
        self.mgr.record_resolution("user1", res, confirmed=True)
        ctx = self.mgr.get_user_context("user1")
        self.assertIn("YouTube", ctx["frequent_entities"])

    def test_record_correction(self):
        self.mgr.record_correction("user1", "u tube", "YouTube")
        ctx = self.mgr.get_user_context("user1")
        self.assertEqual(ctx["corrections"].get("u tube"), "YouTube")


class TestIntentCache(unittest.TestCase):
    def setUp(self):
        self.cache = IntentCache(ttl_seconds=300)

    def test_set_and_get(self):
        self.cache.set("key1", "value1")
        self.assertTrue(self.cache.has("key1"))
        self.assertEqual(self.cache.get("key1"), "value1")

    def test_missing_key(self):
        self.assertFalse(self.cache.has("nonexistent"))
        self.assertIsNone(self.cache.get("nonexistent"))

    def test_should_use_cache_fresh_high_confidence(self):
        from unittest.mock import MagicMock
        val = MagicMock()
        val.confidence = 0.9
        self.cache.set("key2", val, context={"active_workflow": None})
        self.assertTrue(self.cache.should_use_cache("key2", {"active_workflow": None}))

    def test_should_not_use_cache_low_confidence(self):
        from unittest.mock import MagicMock
        val = MagicMock()
        val.confidence = 0.5
        self.cache.set("key3", val, context={"active_workflow": None})
        self.assertFalse(self.cache.should_use_cache("key3", {"active_workflow": None}))


class TestRedactor(unittest.TestCase):
    def test_password_redacted(self):
        result = redact_sensitive_data("my password: secret123")
        self.assertNotIn("secret123", result)
        self.assertIn("[REDACTED]", result)

    def test_api_key_redacted(self):
        result = redact_sensitive_data("api_key: sk-abc123xyz")
        self.assertNotIn("sk-abc123xyz", result)

    def test_no_sensitive_data_unchanged(self):
        text = "open youtube and search cooking videos"
        result = redact_sensitive_data(text)
        self.assertEqual(result, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
