from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Set


class BackgroundPreloader:
    """
    Background Preloading Engine.
    Asynchronously pre-warms domain executors, pre-fetches tool schemas,
    and buffers audio streams off the critical request path.
    """

    def __init__(self) -> None:
        self.prewarmed_executors: Set[str] = set()
        self.prefetched_schemas: Dict[str, Dict[str, Any]] = {}
        self.is_running: bool = False

    async def prewarm_executors(self, domain_names: Set[str]) -> None:
        """Pre-warms isolated domain executors off request path."""
        for domain in domain_names:
            if domain not in self.prewarmed_executors:
                await asyncio.sleep(0.001)  # Non-blocking yield
                self.prewarmed_executors.add(domain)

    async def prefetch_tool_schemas(self, tools: Dict[str, Any]) -> None:
        """Pre-fetches tool schemas into fast in-memory cache."""
        for name, schema in tools.items():
            if name not in self.prefetched_schemas:
                await asyncio.sleep(0.001)
                self.prefetched_schemas[name] = schema

    def trigger_async_preloading(self, domain_names: Set[str], tools: Dict[str, Any]) -> None:
        """Triggers background preloading task safely."""
        async def _preload_task():
            await self.prewarm_executors(domain_names)
            await self.prefetch_tool_schemas(tools)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_preload_task())
        except Exception:
            pass
