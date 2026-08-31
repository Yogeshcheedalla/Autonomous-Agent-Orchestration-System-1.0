from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class VerificationDecision:
    requires_formal_verification: bool
    risk_score: float
    reason: str
    is_approved: bool


class SelectiveRiskVerifier:
    """
    Selective Risk Verifier.
    Executes formal verification checks ONLY for high-risk operations (risk_score >= 0.7
    or destructive file/CLI operations), skipping verification overhead on routine safe actions.
    """

    HIGH_RISK_THRESHOLD = 0.7
    DESTRUCTIVE_KEYWORDS = {"rm", "del", "delete", "drop", "overwrite", "format", "shutdown", "wipe", "force"}

    def evaluate_and_verify(
        self,
        action_name: str,
        parameters: Dict[str, Any],
        base_risk_score: float = 0.1,
        user_confirmed: bool = False,
    ) -> VerificationDecision:
        param_str = str(parameters).lower()
        action_lower = action_name.lower()

        is_destructive = any(kw in param_str or kw in action_lower for kw in self.DESTRUCTIVE_KEYWORDS)
        effective_risk = 0.95 if is_destructive else base_risk_score

        if effective_risk >= self.HIGH_RISK_THRESHOLD:
            if user_confirmed:
                return VerificationDecision(
                    requires_formal_verification=True,
                    risk_score=effective_risk,
                    reason=f"High-risk action '{action_name}' formally verified via user confirmation.",
                    is_approved=True,
                )
            else:
                return VerificationDecision(
                    requires_formal_verification=True,
                    risk_score=effective_risk,
                    reason=f"High-risk action '{action_name}' requires formal verification before execution.",
                    is_approved=False,
                )
        else:
            # Low-risk routine task: bypass verification overhead
            return VerificationDecision(
                requires_formal_verification=False,
                risk_score=effective_risk,
                reason=f"Routine low-risk action '{action_name}' fast-tracked without verification overhead.",
                is_approved=True,
            )
