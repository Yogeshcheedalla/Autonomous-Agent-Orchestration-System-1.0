"""Sensitive data redactor for IIR — prevents passwords/keys from entering logs or LLM calls."""
from __future__ import annotations
import re
from .models import SENSITIVE_PATTERNS


def redact_sensitive_data(text: str) -> str:
    """Redact sensitive patterns from text before logging or LLM API calls."""
    result = text
    for pattern in SENSITIVE_PATTERNS:
        result = re.sub(pattern, "[REDACTED]", result, flags=re.IGNORECASE)
    return result
