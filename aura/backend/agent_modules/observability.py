from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("akansha.telemetry")


@dataclass
class TraceSpan:
    trace_id: str
    span_id: str
    module_name: str
    operation: str
    duration_ms: float
    cost_justified: bool
    status: str = "success"
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class ObservabilityTracer:
    """
    Observability, Tracing & Performance Metrics Engine.
    Provides end-to-end execution spans, structured JSON telemetry logging,
    and P50/P99 latency calculations.
    """

    def __init__(self) -> None:
        self.active_spans: List[TraceSpan] = []

    def start_span(self, module_name: str, operation: str, trace_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "trace_id": trace_id or f"tr_{uuid.uuid4().hex[:8]}",
            "span_id": f"sp_{uuid.uuid4().hex[:6]}",
            "module_name": module_name,
            "operation": operation,
            "start_time": time.time(),
        }

    def end_span(
        self,
        span_context: Dict[str, Any],
        cost_justified: bool = True,
        status: str = "success",
        error: Optional[str] = None,
    ) -> TraceSpan:
        duration_ms = (time.time() - span_context["start_time"]) * 1000
        span = TraceSpan(
            trace_id=span_context["trace_id"],
            span_id=span_context["span_id"],
            module_name=span_context["module_name"],
            operation=span_context["operation"],
            duration_ms=duration_ms,
            cost_justified=cost_justified,
            status=status,
            error=error,
        )
        self.active_spans.append(span)

        # Structured telemetry log
        log_entry = json.dumps(asdict(span))
        logger.info(log_entry)
        return span

    def calculate_latency_metrics(self) -> Dict[str, float]:
        if not self.active_spans:
            return {"p50_ms": 0.0, "p99_ms": 0.0, "total_spans": 0}

        durations = sorted([s.duration_ms for s in self.active_spans])
        n = len(durations)
        p50 = durations[int(n * 0.50)]
        p99 = durations[min(int(n * 0.99), n - 1)]
        return {"p50_ms": round(p50, 2), "p99_ms": round(p99, 2), "total_spans": n}
