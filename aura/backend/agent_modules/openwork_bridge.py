"""
OpenWork Capability Bridge for Akansha AI OS
Bridges OpenWork's capability protocol (search_capabilities, execute_capability)
and @openwork/handsfree accessibility format into Akansha's 3-Lane Execution Engine.
"""
from __future__ import annotations

import logging
import os
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

@dataclass
class CapabilityDefinition:
    id: str
    name: str
    description: str
    domain: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    provider: str = "openwork_akansha"

class OpenWorkCapabilityBridge:
    """
    Bridge module for managing OpenWork capabilities, MCP tool discovery,
    and translating OpenWork handsfree compact element references ({e1}, {e2})
    into Akansha execution targets.
    """

    def __init__(self) -> None:
        self.capabilities: Dict[str, CapabilityDefinition] = {}
        self._register_default_capabilities()

    def _register_default_capabilities(self) -> None:
        defaults = [
            CapabilityDefinition(
                id="browser_navigate",
                name="Navigate Web Page",
                description="Navigate to target URL using real browser driver with DOM snapshot",
                domain="browser",
                parameters={"url": "string", "wait_until": "string"},
            ),
            CapabilityDefinition(
                id="browser_click",
                name="Click Element",
                description="Click element by selector or compact reference ref={e1}",
                domain="browser",
                parameters={"element_ref": "string", "tab_id": "string"},
            ),
            CapabilityDefinition(
                id="browser_fill",
                name="Fill Form Field",
                description="Fill text into input element by selector or ref={e1}",
                domain="browser",
                parameters={"element_ref": "string", "value": "string"},
            ),
            CapabilityDefinition(
                id="desktop_switch_window",
                name="Switch Window Focus",
                description="Focus target window by title or process PID",
                domain="desktop",
                parameters={"title": "string", "pid": "int"},
            ),
            CapabilityDefinition(
                id="desktop_click",
                name="Desktop Accessibility Click",
                description="Perform native UI automation click via window PID and element ID",
                domain="desktop",
                parameters={"element_id": "string", "pid": "int"},
            ),
            CapabilityDefinition(
                id="desktop_type",
                name="Desktop Background Type Text",
                description="Send background typing events to target process PID",
                domain="desktop",
                parameters={"text": "string", "pid": "int"},
            ),
            CapabilityDefinition(
                id="jarvis_continuous_task",
                name="Execute Continuous Multi-Turn Goal",
                description="Launch handsfree Jarvis continuous automation loop for longer multi-turn goals",
                domain="jarvis",
                parameters={"goal": "string", "voice_feedback": "bool"},
            ),
            CapabilityDefinition(
                id="continuous_tab_automation",
                name="Continuous Conversational Tab Automation",
                description="Execute continuous multi-turn agent automation isolated within a single dedicated tab",
                domain="browser",
                parameters={"goal": "string", "target_tab_id": "string"},
            ),
        ]
        for cap in defaults:
            self.capabilities[cap.id] = cap

    def search_capabilities(self, query: str = "", domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search available OpenWork capabilities."""
        results = []
        q = query.lower().strip()
        for cap in self.capabilities.values():
            if not cap.enabled:
                continue
            if domain and cap.domain != domain:
                continue
            if not q or q in cap.name.lower() or q in cap.description.lower() or q in cap.id.lower():
                results.append({
                    "id": cap.id,
                    "name": cap.name,
                    "description": cap.description,
                    "domain": cap.domain,
                    "parameters": cap.parameters,
                    "provider": cap.provider,
                })
        return results

    def resolve_compact_ref(self, ref_str: str, dom_snapshot: Dict[str, Any]) -> Optional[str]:
        """
        Translates OpenWork handsfree compact refs (e.g. '{e1}', '{e2}') 
        into actual CSS selectors or element IDs from the snapshot.
        """
        if not ref_str:
            return None
        clean_ref = ref_str.strip().strip("{}")
        elements = dom_snapshot.get("compact_elements", {})
        if clean_ref in elements:
            return elements[clean_ref].get("selector") or elements[clean_ref].get("id")
        return ref_str

    def execute_capability(self, capability_id: str, params: Dict[str, Any], router: Any = None) -> Dict[str, Any]:
        """Execute capability by dispatching to Akansha domain router or execution handler."""
        if capability_id not in self.capabilities:
            return {"status": "error", "error": f"Capability '{capability_id}' not registered"}
        
        cap = self.capabilities[capability_id]
        logger.info("Executing OpenWork Capability: %s (Domain: %s)", cap.name, cap.domain)

        if router:
            return router.route_and_execute(
                user_input=f"Execute capability {cap.name}",
                action=capability_id,
                params=params
            )

        # No router means nothing ran. This used to return
        # `{"status": "success", "message": "Successfully executed capability ..."}`
        # here, which is how a caller could be told a browser had been driven when
        # no browser existed -- the registration of a capability was being reported
        # as its execution. The live route passes a real router, so this branch is
        # reached by tests and by any embedder that forgets one; either way the only
        # true thing to say is that there was nothing to dispatch to.
        logger.warning("No domain router bound: %s was not executed", capability_id)
        return {
            "status": "error",
            "capability": capability_id,
            "domain": cap.domain,
            "params": params,
            "executed": False,
            "error": (
                f"{cap.name} was not executed: no execution router is bound to this "
                "bridge, so there is nothing to dispatch the capability to."
            ),
        }

    def system_prompt_section(self) -> str:
        return """
# MODULE: OPENWORK CAPABILITY BRIDGE & CONTINUOUS CONVERSATIONAL AUTOMATION
- CAPABILITY PROTOCOL: Exposes search_capabilities & execute_capability tool schemas for external MCP clients.
- HANDSFREE REF RESOLUTION: Resolves compact element references ({e1}, {e2}) to precise DOM selectors / UI automation coordinates.
- TAB ISOLATION (ZERO INTERFERENCE): Focuses exclusively on target tab_id without touching other tabs, windows, or desktop processes.
- CONTINUOUS AUTOMATION LOOP: Auto-executes multi-turn goals continuously with live status feedback and voice barge-in support.
"""
