"""
voice_kernel.task_graph — hierarchical task state (§15, §20, §35, §37, §38).
===========================================================================

The old code had a flat list of "subtasks" advanced one HTTP call at a time,
with no dependency edges, no cancellation, and no way to express "cancel the
deployment but keep the fixes". This module is the replacement.

Three properties matter more than anything else here:

  Dependencies are real.   `ready_nodes()` only returns work whose upstream
                           edges have all COMPLETED, so a runner can be dumb.
  Cancellation is scoped.  Every node carries a `CancellationToken`; cancelling
                           a node cancels its subtree and nothing else. That is
                           what makes §35's "production → CANCELLED, staging →
                           ACTIVE" a two-line operation.
  State is serialisable.   `snapshot()` / `restore()` round-trip the whole graph
                           as plain JSON so §37/§38 checkpointing is not a
                           second, divergent representation.

No threads, no asyncio, no I/O. A runner drives this from outside.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


class NodeStatus(str, Enum):
    """§15 — the seven statuses, exactly."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"      # blocked on the user, not on a dependency
    BLOCKED = "BLOCKED"      # an upstream dependency failed or was cancelled
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: Statuses from which no further work will happen.
TERMINAL = (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.CANCELLED)
#: Statuses that mean "in flight right now".
ACTIVE = (NodeStatus.RUNNING, NodeStatus.WAITING)


class CancellationToken:
    """§20 — a cooperative cancellation flag with a reason and callbacks.

    Deliberately not `asyncio.Event`: the kernel is sync and transport-agnostic,
    and a tool executor may be a thread, a subprocess or a browser driver. All
    of those can poll `is_cancelled`.
    """

    __slots__ = ("_cancelled", "_reason", "_at", "_callbacks")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason: Optional[str] = None
        self._at: Optional[float] = None
        self._callbacks: List[Callable[[str], None]] = []

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def cancelled_at(self) -> Optional[float]:
        return self._at

    def cancel(self, reason: str = "user requested") -> bool:
        """Trip the token. Returns False if it was already tripped."""
        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = reason
        self._at = time.time()
        for cb in tuple(self._callbacks):
            try:
                cb(reason)
            except Exception:
                # A misbehaving observer must not block cancellation.
                pass
        return True

    def on_cancel(self, callback: Callable[[str], None]) -> None:
        """Register a stopper. Fires immediately if already cancelled."""
        if self._cancelled:
            try:
                callback(self._reason or "cancelled")
            except Exception:
                pass
            return
        self._callbacks.append(callback)

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise TaskCancelled(self._reason or "cancelled")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<CancellationToken cancelled={self._cancelled} reason={self._reason!r}>"


class TaskCancelled(RuntimeError):
    """Raised by `CancellationToken.raise_if_cancelled`."""


@dataclass
class TaskNode:
    """§15 — one unit of work. Every field in the spec is present."""

    description: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: NodeStatus = NodeStatus.PENDING
    #: Node ids that must reach COMPLETED before this one may run.
    dependencies: List[str] = field(default_factory=list)
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)

    #: What the executor should do. `tool` is resolved against the §39 registry.
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)

    result: Optional[Any] = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 2

    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

    #: True when this step must be read back before running (§36, §39).
    requires_confirmation: bool = False
    #: Free-form label used by the UI task panel (§27).
    kind: str = "step"

    token: CancellationToken = field(default_factory=CancellationToken, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def duration_s(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def can_retry(self) -> bool:
        return self.status is NodeStatus.FAILED and self.retry_count < self.max_retries

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "dependencies": list(self.dependencies),
            "parent_id": self.parent_id,
            "children": list(self.children),
            "tool": self.tool,
            "args": dict(self.args),
            "result": self.result,
            "error": self.error,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "requires_confirmation": self.requires_confirmation,
            "kind": self.kind,
            "duration_s": self.duration_s,
            "cancelled": self.token.is_cancelled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskNode":
        node = cls(
            description=data["description"],
            id=data.get("id") or uuid.uuid4().hex[:12],
            status=NodeStatus(data.get("status", "PENDING")),
            dependencies=list(data.get("dependencies") or []),
            parent_id=data.get("parent_id"),
            children=list(data.get("children") or []),
            tool=data.get("tool"),
            args=dict(data.get("args") or {}),
            result=data.get("result"),
            error=data.get("error"),
            retry_count=int(data.get("retry_count") or 0),
            max_retries=int(data.get("max_retries") or 2),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            requires_confirmation=bool(data.get("requires_confirmation")),
            kind=data.get("kind") or "step",
        )
        node.created_at = float(data.get("created_at") or time.time())
        if data.get("cancelled"):
            node.token.cancel("restored from checkpoint")
        return node


class TaskGraph:
    """A hierarchical, dependency-aware task DAG with scoped cancellation."""

    def __init__(self, goal: str = "", *, session_id: str = "") -> None:
        self.goal = goal
        self.session_id = session_id
        self.nodes: Dict[str, TaskNode] = {}
        #: Insertion order — the UI renders steps in this order (§27).
        self.order: List[str] = []
        self.created_at = time.time()
        self.root_token = CancellationToken()
        #: Audit trail of every status change, for the timeline panel.
        self.log: List[Dict[str, Any]] = []

    # ── construction ──────────────────────────────────────────────────────
    def add(
        self,
        description: str,
        *,
        depends_on: Optional[Iterable[str]] = None,
        parent: Optional[str] = None,
        tool: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        requires_confirmation: bool = False,
        kind: str = "step",
        node_id: Optional[str] = None,
    ) -> TaskNode:
        node = TaskNode(
            description=description,
            id=node_id or uuid.uuid4().hex[:12],
            dependencies=list(depends_on or []),
            parent_id=parent,
            tool=tool,
            args=dict(args or {}),
            requires_confirmation=requires_confirmation,
            kind=kind,
        )
        unknown = [d for d in node.dependencies if d not in self.nodes]
        if unknown:
            raise KeyError(f"unknown dependencies: {unknown}")
        if parent is not None:
            if parent not in self.nodes:
                raise KeyError(f"unknown parent {parent!r}")
            self.nodes[parent].children.append(node.id)
        self.nodes[node.id] = node
        self.order.append(node.id)
        self._note(node, "added")
        return node

    def add_sequence(self, descriptions: Iterable[str], *, parent: Optional[str] = None) -> List[TaskNode]:
        """Convenience: chain steps so each depends on the previous one."""
        out: List[TaskNode] = []
        previous: Optional[str] = None
        for description in descriptions:
            node = self.add(
                description,
                depends_on=[previous] if previous else None,
                parent=parent,
            )
            out.append(node)
            previous = node.id
        return out

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def get(self, node_id: str) -> TaskNode:
        return self.nodes[node_id]

    def find(self, needle: str) -> List[TaskNode]:
        """Nodes whose description mentions `needle`. Used by §35 corrections."""
        lowered = needle.lower().strip()
        if not lowered:
            return []
        return [n for n in self.iter_nodes() if lowered in n.description.lower()]

    def iter_nodes(self) -> List[TaskNode]:
        return [self.nodes[i] for i in self.order if i in self.nodes]

    # ── scheduling ────────────────────────────────────────────────────────
    def ready_nodes(self) -> List[TaskNode]:
        """PENDING nodes whose dependencies have all COMPLETED.

        A runner can loop on this and needs to know nothing about the graph.
        """
        if self.root_token.is_cancelled:
            return []
        out: List[TaskNode] = []
        for node in self.iter_nodes():
            if node.status is not NodeStatus.PENDING or node.token.is_cancelled:
                continue
            deps = [self.nodes[d] for d in node.dependencies if d in self.nodes]
            if any(d.status is not NodeStatus.COMPLETED for d in deps):
                continue
            out.append(node)
        return out

    def next_node(self) -> Optional[TaskNode]:
        ready = self.ready_nodes()
        return ready[0] if ready else None

    @property
    def is_complete(self) -> bool:
        """True when nothing further can happen without user input.

        BLOCKED counts as settled: an unreplaced cancelled dependency means
        that branch is finished, and a graph that can never reach 100% would
        leave the runner spinning.
        """
        if not self.nodes:
            return False
        return all(
            n.is_terminal or n.status is NodeStatus.BLOCKED for n in self.iter_nodes()
        )

    @property
    def is_running(self) -> bool:
        return any(n.status in ACTIVE for n in self.iter_nodes())

    @property
    def progress(self) -> float:
        """Fraction of nodes in a terminal state, 0.0–1.0."""
        if not self.nodes:
            return 0.0
        done = sum(1 for n in self.iter_nodes() if n.is_terminal)
        return round(done / len(self.nodes), 4)

    def counts(self) -> Dict[str, int]:
        tally: Dict[str, int] = {s.value: 0 for s in NodeStatus}
        for node in self.iter_nodes():
            tally[node.status.value] += 1
        return tally

    # ── transitions ───────────────────────────────────────────────────────
    def start(self, node_id: str) -> TaskNode:
        node = self.nodes[node_id]
        if node.token.is_cancelled or self.root_token.is_cancelled:
            raise TaskCancelled(node.token.reason or "graph cancelled")
        if node.status not in (NodeStatus.PENDING, NodeStatus.WAITING, NodeStatus.FAILED):
            raise ValueError(f"cannot start {node_id} from {node.status.value}")
        node.status = NodeStatus.RUNNING
        node.started_at = time.time()
        node.error = None
        self._note(node, "started")
        return node

    def complete(self, node_id: str, result: Any = None) -> TaskNode:
        node = self.nodes[node_id]
        node.status = NodeStatus.COMPLETED
        node.result = result
        node.completed_at = time.time()
        self._note(node, "completed")
        return node

    def fail(self, node_id: str, error: str) -> TaskNode:
        node = self.nodes[node_id]
        node.status = NodeStatus.FAILED
        node.error = error
        node.completed_at = time.time()
        self._note(node, "failed", error=error)
        self._block_downstream(node_id, "upstream step failed")
        return node

    def wait_for_user(self, node_id: str, question: str) -> TaskNode:
        """§15 WAITING — blocked on a person, not on a dependency."""
        node = self.nodes[node_id]
        node.status = NodeStatus.WAITING
        self._note(node, "waiting", question=question)
        return node

    def retry(self, node_id: str) -> TaskNode:
        node = self.nodes[node_id]
        if not node.can_retry:
            raise ValueError(f"{node_id} is not retryable ({node.status.value}, {node.retry_count} tries)")
        node.retry_count += 1
        node.status = NodeStatus.PENDING
        node.error = None
        node.started_at = None
        node.completed_at = None
        self._note(node, "retry")
        # Downstream work that was blocked by this failure becomes eligible again.
        self._unblock_downstream(node_id)
        return node

    # ── cancellation (§20) and correction (§35) ────────────────────────────
    def cancel_node(self, node_id: str, reason: str = "user requested") -> List[str]:
        """Cancel one node and everything beneath/after it. Returns cancelled ids.

        "Cancel the deployment but keep the fixes" is exactly this call against
        the deployment subtree: sibling branches are untouched.
        """
        cancelled: List[str] = []
        for target in self._subtree(node_id):
            node = self.nodes[target]
            if node.status is NodeStatus.COMPLETED:
                # Completed work stays completed — that is the "keep the fixes" half.
                continue
            if node.status is NodeStatus.CANCELLED:
                continue
            node.token.cancel(reason)
            node.status = NodeStatus.CANCELLED
            node.completed_at = time.time()
            cancelled.append(target)
            self._note(node, "cancelled", reason=reason)
        if cancelled:
            self._block_downstream(node_id, "upstream step cancelled")
        return cancelled

    def cancel_all(self, reason: str = "user requested") -> List[str]:
        """Hard stop. Trips the root token so `ready_nodes()` goes empty."""
        self.root_token.cancel(reason)
        cancelled: List[str] = []
        for node in self.iter_nodes():
            if node.status in (NodeStatus.COMPLETED, NodeStatus.CANCELLED):
                continue
            node.token.cancel(reason)
            node.status = NodeStatus.CANCELLED
            node.completed_at = time.time()
            cancelled.append(node.id)
            self._note(node, "cancelled", reason=reason)
        return cancelled

    def replace(
        self,
        node_id: str,
        description: str,
        *,
        tool: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        reason: str = "user correction",
    ) -> TaskNode:
        """§35 — "Deploy to production" → "Actually, staging".

        The old node becomes CANCELLED (auditable), and a replacement inherits
        its dependencies and dependents so the rest of the plan survives.
        """
        old = self.nodes[node_id]
        dependents = [n for n in self.iter_nodes() if node_id in n.dependencies]
        self.cancel_node(node_id, reason)
        new = self.add(
            description,
            depends_on=list(old.dependencies),
            parent=old.parent_id,
            tool=tool if tool is not None else old.tool,
            args=args if args is not None else dict(old.args),
            requires_confirmation=old.requires_confirmation,
            kind=old.kind,
        )
        for dependent in dependents:
            dependent.dependencies = [new.id if d == node_id else d for d in dependent.dependencies]
            # It was blocked by the cancellation; the replacement re-opens it.
            if dependent.status is NodeStatus.BLOCKED:
                dependent.status = NodeStatus.PENDING
                self._note(dependent, "unblocked")
        self._note(new, "replaces", replaced=node_id, reason=reason)
        return new

    # ── graph internals ───────────────────────────────────────────────────
    def _subtree(self, node_id: str) -> List[str]:
        """`node_id` plus all of its descendants, parents-first."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        out: List[str] = []
        seen: Set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop(0)
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            out.append(current)
            stack.extend(self.nodes[current].children)
        return out

    def _dependents(self, node_id: str) -> List[str]:
        """Everything downstream of `node_id` through dependency edges."""
        out: List[str] = []
        seen: Set[str] = {node_id}
        frontier = [node_id]
        while frontier:
            current = frontier.pop(0)
            for node in self.iter_nodes():
                if current in node.dependencies and node.id not in seen:
                    seen.add(node.id)
                    out.append(node.id)
                    frontier.append(node.id)
        return out

    def _block_downstream(self, node_id: str, reason: str) -> List[str]:
        blocked: List[str] = []
        for target in self._dependents(node_id):
            node = self.nodes[target]
            if node.status in (NodeStatus.PENDING, NodeStatus.WAITING):
                node.status = NodeStatus.BLOCKED
                blocked.append(target)
                self._note(node, "blocked", reason=reason)
        return blocked

    def _unblock_downstream(self, node_id: str) -> List[str]:
        freed: List[str] = []
        for target in self._dependents(node_id):
            node = self.nodes[target]
            if node.status is NodeStatus.BLOCKED and not node.token.is_cancelled:
                node.status = NodeStatus.PENDING
                freed.append(target)
                self._note(node, "unblocked")
        return freed

    def _note(self, node: TaskNode, event: str, **extra: Any) -> None:
        self.log.append({
            "at": time.time(),
            "node_id": node.id,
            "event": event,
            "status": node.status.value,
            "description": node.description,
            **extra,
        })
        del self.log[:-400]

    # ── §37/§38: serialisation for checkpoints ─────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        """Whole-graph state as plain JSON-safe data."""
        return {
            "goal": self.goal,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "order": list(self.order),
            "nodes": [self.nodes[i].as_dict() for i in self.order if i in self.nodes],
            "cancelled": self.root_token.is_cancelled,
            "progress": self.progress,
            "counts": self.counts(),
        }

    @classmethod
    def restore(cls, data: Dict[str, Any]) -> "TaskGraph":
        graph = cls(data.get("goal", ""), session_id=data.get("session_id", ""))
        graph.created_at = float(data.get("created_at") or time.time())
        for raw in data.get("nodes") or []:
            node = TaskNode.from_dict(raw)
            graph.nodes[node.id] = node
        graph.order = [i for i in (data.get("order") or list(graph.nodes)) if i in graph.nodes]
        for node_id in graph.nodes:
            if node_id not in graph.order:
                graph.order.append(node_id)
        if data.get("cancelled"):
            graph.root_token.cancel("restored cancelled")
        return graph

    def resumable_nodes(self) -> List[TaskNode]:
        """§37 — work that was in flight when the process died.

        RUNNING cannot survive a restart: nothing is executing any more. These
        are the nodes to ask the user about before continuing.
        """
        return [
            n for n in self.iter_nodes()
            if n.status in (NodeStatus.RUNNING, NodeStatus.WAITING, NodeStatus.PENDING)
            and not n.token.is_cancelled
        ]

    def reopen_running(self) -> List[str]:
        """Demote RUNNING back to PENDING after a restart."""
        reopened: List[str] = []
        for node in self.iter_nodes():
            if node.status is NodeStatus.RUNNING:
                node.status = NodeStatus.PENDING
                node.started_at = None
                reopened.append(node.id)
                self._note(node, "reopened")
        return reopened

    # ── §17: what is worth saying out loud ────────────────────────────────
    def spoken_summary(self) -> str:
        """A one-line answer to "what are you doing?" — §14."""
        running = [n for n in self.iter_nodes() if n.status is NodeStatus.RUNNING]
        if running:
            done = sum(1 for n in self.iter_nodes() if n.status is NodeStatus.COMPLETED)
            return f"{running[0].description} — step {done + 1} of {len(self.nodes)}."
        waiting = [n for n in self.iter_nodes() if n.status is NodeStatus.WAITING]
        if waiting:
            return f"Waiting on you for: {waiting[0].description}."
        failed = [n for n in self.iter_nodes() if n.status is NodeStatus.FAILED]
        if failed:
            return f"{failed[0].description} failed: {failed[0].error or 'unknown error'}."
        if self.is_complete:
            cancelled = sum(1 for n in self.iter_nodes() if n.status is NodeStatus.CANCELLED)
            if cancelled and cancelled == len(self.nodes):
                return "Everything was cancelled."
            return f"Finished{' the ' + self.goal if self.goal else ''}."
        pending = self.next_node()
        if pending:
            return f"About to start: {pending.description}."
        return "Nothing in flight."

    def as_dict(self) -> Dict[str, Any]:
        data = self.snapshot()
        data["is_complete"] = self.is_complete
        data["is_running"] = self.is_running
        data["summary"] = self.spoken_summary()
        return data
