from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GRAPH_ROOT = Path(os.getenv("AKANSHA_KNOWLEDGE_GRAPH_DIR", PROJECT_ROOT / "graphify" / "users"))

# The graph owner used when the request carries no identity.
#
# Deliberately still the string "default_user" rather than something tidier like
# "local-owner": the id is the *filename* of the graph
# (graphify/users/<id>.akansha-graph.json), so renaming it does not migrate the
# existing graph -- it silently starts an empty one beside it and every fact learned
# so far becomes unreachable. Rename this only together with a file rename.
LOCAL_GRAPH_OWNER_ID = "default_user"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_graph_user_id(user_id: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]+", "_", (user_id or LOCAL_GRAPH_OWNER_ID).strip())[:120]
    return cleaned or LOCAL_GRAPH_OWNER_ID


def user_graph_path(user_id: str | None = None) -> Path:
    return GRAPH_ROOT / f"{safe_graph_user_id(user_id)}.akansha-graph.json"


def _redact_sensitive(text: str) -> str:
    redacted = text or ""
    redacted = re.sub(
        r"(?i)\b(password|passcode|otp|token|api[_ -]?key|secret)\b\s*(?:is|are|=|:)?\s*['\"]?[^,\n;.]+",
        lambda match: f"{match.group(1)}: [redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(password|passcode|otp|token|api[_ -]?key|secret)\b\s*[:=]?\s*\S+",
        r"\1: [redacted]",
        redacted,
    )
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[redacted]", redacted)
    redacted = re.sub(r"\b\d{6}\b", "[redacted-code]", redacted)
    return redacted


def _snippet(text: str, limit: int = 360) -> str:
    clean = " ".join(_redact_sensitive(text).split())
    return clean[:limit]


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "::".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _empty_graph(user_id: str) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "version": 1,
        "kind": "akansha-user-knowledge-graph",
        "user_id": user_id,
        "created_at": now,
        "updated_at": now,
        "event_count": 0,
        "surfaces": {
            "chat": {"messages": 0},
            "voice": {"events": 0},
            "automation": {"events": 0},
            "alarms": {"events": 0},
            "social": {"events": 0},
        },
        "permissions": {},
        "nodes": {},
        "edges": [],
        "recent_events": [],
    }


def load_user_knowledge_graph(user_id: str | None = None) -> dict[str, Any]:
    safe_user = safe_graph_user_id(user_id)
    path = user_graph_path(safe_user)
    if not path.exists():
        return _empty_graph(safe_user)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_graph(safe_user)
    if not isinstance(data, dict):
        return _empty_graph(safe_user)
    data.setdefault("version", 1)
    data.setdefault("kind", "akansha-user-knowledge-graph")
    data.setdefault("user_id", safe_user)
    data.setdefault("nodes", {})
    data.setdefault("edges", [])
    data.setdefault("recent_events", [])
    data.setdefault("permissions", {})
    data.setdefault("surfaces", _empty_graph(safe_user)["surfaces"])
    return data


def _write_graph(user_id: str, graph: dict[str, Any]) -> Path:
    GRAPH_ROOT.mkdir(parents=True, exist_ok=True)
    path = user_graph_path(user_id)
    graph["updated_at"] = utc_now_iso()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(GRAPH_ROOT), suffix=".tmp") as handle:
        json.dump(graph, handle, ensure_ascii=False, indent=2, sort_keys=True)
        temp_name = handle.name
    Path(temp_name).replace(path)
    return path


def _add_node(graph: dict[str, Any], node_id: str, node_type: str, label: str, attributes: dict[str, Any] | None = None) -> None:
    now = utc_now_iso()
    nodes = graph.setdefault("nodes", {})
    existing = nodes.get(node_id, {})
    nodes[node_id] = {
        "id": node_id,
        "type": node_type,
        "label": label,
        "attributes": {**existing.get("attributes", {}), **(attributes or {})},
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }


def _add_edge(
    graph: dict[str, Any],
    source: str,
    target: str,
    relationship: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    edge_id = _stable_id("edge", source, relationship, target, json.dumps(attributes or {}, sort_keys=True))
    edges = graph.setdefault("edges", [])
    if any(edge.get("id") == edge_id for edge in edges):
        return
    edges.append(
        {
            "id": edge_id,
            "source": source,
            "target": target,
            "relationship": relationship,
            "attributes": attributes or {},
            "created_at": utc_now_iso(),
        }
    )
    if len(edges) > 1200:
        del edges[: len(edges) - 1200]


def record_knowledge_event(
    user_id: str | None,
    surface: str,
    event_type: str,
    summary: str,
    metadata: dict[str, Any] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    safe_user = safe_graph_user_id(user_id)
    graph = load_user_knowledge_graph(safe_user)
    now = utc_now_iso()
    user_node = _stable_id("user", safe_user)
    surface_node = _stable_id("surface", surface)
    event_node = _stable_id("event", safe_user, surface, event_type, now, summary)
    event_summary = _snippet(summary or content or event_type)
    attributes = {
        "surface": surface,
        "event_type": event_type,
        "summary": event_summary,
        "metadata": metadata or {},
    }
    if content:
        attributes["content_snippet"] = _snippet(content)

    _add_node(graph, user_node, "user", safe_user, {"source": "akansha"})
    _add_node(graph, surface_node, "surface", surface, {})
    _add_node(graph, event_node, "event", event_summary or event_type, attributes)
    _add_edge(graph, user_node, event_node, "generated_event", {"surface": surface})
    _add_edge(graph, event_node, surface_node, "belongs_to_surface", {"event_type": event_type})

    surfaces = graph.setdefault("surfaces", {})
    surface_stats = surfaces.setdefault(surface, {"events": 0})
    surface_stats["events"] = int(surface_stats.get("events", 0)) + 1
    if surface == "chat":
        surface_stats["messages"] = int(surface_stats.get("messages", 0)) + 1

    graph["event_count"] = int(graph.get("event_count", 0)) + 1
    graph.setdefault("recent_events", []).append(
        {
            "at": now,
            "surface": surface,
            "event_type": event_type,
            "summary": event_summary,
            "metadata": metadata or {},
        }
    )
    graph["recent_events"] = graph["recent_events"][-80:]
    path = _write_graph(safe_user, graph)
    return {"path": str(path), "graph": graph}


def record_chat_message_in_graph(
    user_id: str | None,
    session_id: str,
    role: str,
    content: str,
    message_id: int | None = None,
) -> dict[str, Any]:
    return record_knowledge_event(
        user_id=user_id,
        surface="chat",
        event_type=f"{role}_message",
        summary=f"{role}: {_snippet(content, 120)}",
        metadata={"session_id": session_id, "message_id": message_id},
        content=content,
    )


def record_social_permission_in_graph(
    user_id: str | None,
    platform: str,
    permission_level: str,
    adapter_key: str | None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    safe_user = safe_graph_user_id(user_id)
    graph = load_user_knowledge_graph(safe_user)
    graph.setdefault("permissions", {})[platform] = {
        "permission_level": permission_level,
        "adapter_key": adapter_key,
        "scopes": scopes or [],
        "updated_at": utc_now_iso(),
    }
    _write_graph(safe_user, graph)
    return record_knowledge_event(
        user_id=safe_user,
        surface="social",
        event_type="permission_updated",
        summary=f"{platform} permission set to {permission_level}",
        metadata={"platform": platform, "permission_level": permission_level, "adapter_key": adapter_key, "scopes": scopes or []},
    )


def graph_snapshot(user_id: str | None = None) -> dict[str, Any]:
    graph = load_user_knowledge_graph(user_id)
    return {"path": str(user_graph_path(graph.get("user_id"))), "graph": graph}
