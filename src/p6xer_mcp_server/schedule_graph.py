"""
ScheduleGraph: directed graph model built from a PyP6XER Reader.

Nodes = activities (Task), Edges = relationships (TaskPred).
Supports cycle detection, JSON export, and topological sort.
Zero new dependencies — pure dict/list adjacency.
"""

from __future__ import annotations

from collections import deque
from typing import Any


class ScheduleGraph:
    """Directed graph of schedule activities and their logical relationships."""

    def __init__(self) -> None:
        self.nodes: dict[int, dict[str, Any]] = {}  # task_id -> node attrs
        self.edges: list[dict[str, Any]] = []         # list of edge dicts
        self._successors: dict[int, list[int]] = {}    # task_id -> [succ_task_id, ...]
        self._predecessors: dict[int, list[int]] = {}  # task_id -> [pred_task_id, ...]

    # ------------------------------------------------------------------
    # Build from Reader
    # ------------------------------------------------------------------

    @classmethod
    def from_reader(cls, reader, project_id: int | None = None) -> "ScheduleGraph":
        """Build a ScheduleGraph from a PyP6XER Reader, optionally filtered by project_id."""
        g = cls()

        # --- nodes ---
        for act in reader.activities:
            pid = getattr(act, "proj_id", None)
            if project_id is not None and pid != project_id:
                continue
            tid = act.task_id
            if tid is None:
                continue
            g.nodes[tid] = {
                "task_id":            tid,
                "task_code":          getattr(act, "task_code", None),
                "task_name":          getattr(act, "task_name", None),
                "task_type":          getattr(act, "task_type", None),
                "status_code":        getattr(act, "status_code", None),
                "target_drtn_hr_cnt": getattr(act, "target_drtn_hr_cnt", None),
                "remain_drtn_hr_cnt": getattr(act, "remain_drtn_hr_cnt", None),
                "clndr_id":           getattr(act, "clndr_id", None),
                "wbs_id":             getattr(act, "wbs_id", None),
                "proj_id":            pid,
                # constraint fields
                "cstr_type":          getattr(act, "cstr_type", None),
                "cstr_date":          _d(getattr(act, "cstr_date", None)),
                "cstr_type2":         getattr(act, "cstr_type2", None),
                "cstr_date2":         _d(getattr(act, "cstr_date2", None)),
                # current schedule dates (from XER / P6)
                "early_start_date":   _d(getattr(act, "early_start_date", None)),
                "early_end_date":     _d(getattr(act, "early_end_date", None)),
                "late_start_date":    _d(getattr(act, "late_start_date", None)),
                "late_end_date":      _d(getattr(act, "late_end_date", None)),
                "act_start_date":     _d(getattr(act, "act_start_date", None)),
                "act_end_date":       _d(getattr(act, "act_end_date", None)),
                "target_start_date":  _d(getattr(act, "target_start_date", None)),
                "target_end_date":    _d(getattr(act, "target_end_date", None)),
                "total_float_hr_cnt": getattr(act, "total_float_hr_cnt", None),
                "free_float_hr_cnt":  getattr(act, "free_float_hr_cnt", None),
            }
            g._successors[tid] = []
            g._predecessors[tid] = []

        # --- edges ---
        task_ids = set(g.nodes)
        for rel in reader.relations:
            succ_id = getattr(rel, "task_id", None)
            pred_id = getattr(rel, "pred_task_id", None)
            if succ_id is None or pred_id is None:
                continue
            # both endpoints must be in our node set
            if succ_id not in task_ids or pred_id not in task_ids:
                continue
            edge = {
                "task_pred_id": getattr(rel, "task_pred_id", None),
                "pred_task_id": pred_id,
                "task_id":      succ_id,
                "pred_type":    getattr(rel, "pred_type", "PR_FS"),
                "lag_hr_cnt":   getattr(rel, "lag_hr_cnt", 0) or 0,
            }
            g.edges.append(edge)
            g._successors.setdefault(pred_id, []).append(succ_id)
            g._predecessors.setdefault(succ_id, []).append(pred_id)

        return g

    # ------------------------------------------------------------------
    # Topological sort (Kahn's algorithm)
    # ------------------------------------------------------------------

    def topological_sort(self) -> list[int]:
        """Return task_ids in topological order. Raises ValueError if a cycle is detected."""
        in_degree: dict[int, int] = {tid: len(self._predecessors.get(tid, [])) for tid in self.nodes}
        queue: deque[int] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        order: list[int] = []

        while queue:
            tid = queue.popleft()
            order.append(tid)
            for succ in self._successors.get(tid, []):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

        if len(order) != len(self.nodes):
            raise ValueError("Graph contains a cycle — topological sort impossible")
        return order

    # ------------------------------------------------------------------
    # Cycle detection (DFS coloring)
    # ------------------------------------------------------------------

    def detect_cycle(self) -> list[int] | None:
        """
        Return a list of task_ids forming a cycle, or None if the graph is acyclic.
        Uses iterative DFS with white/grey/black coloring.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[int, int] = {tid: WHITE for tid in self.nodes}
        parent: dict[int, int | None] = {tid: None for tid in self.nodes}

        # Use an explicit stack to avoid recursion limits on large schedules
        for start in self.nodes:
            if color[start] != WHITE:
                continue
            stack = [(start, iter(self._successors.get(start, [])))]
            color[start] = GREY
            while stack:
                tid, succ_iter = stack[-1]
                try:
                    succ = next(succ_iter)
                except StopIteration:
                    color[tid] = BLACK
                    stack.pop()
                    continue
                if color.get(succ) == GREY:
                    # found a back edge — extract the cycle
                    cycle = [succ, tid]
                    cur = tid
                    while parent.get(cur) is not None and parent[cur] != succ:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.append(succ)
                    cycle.reverse()
                    return cycle
                if color.get(succ) == WHITE:
                    color[succ] = GREY
                    parent[succ] = tid
                    stack.append((succ, iter(self._successors.get(succ, []))))
        return None

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Export the full graph as a JSON-serializable dict."""
        return {
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
            "metadata": {
                "activity_count": len(self.nodes),
                "edge_count":     len(self.edges),
            },
        }

    def summary(self) -> dict[str, Any]:
        """Lightweight summary: counts, projects, cycle status."""
        proj_ids = sorted({n.get("proj_id") for n in self.nodes.values() if n.get("proj_id")})
        cycle = self.detect_cycle()
        return {
            "activity_count": len(self.nodes),
            "edge_count":     len(self.edges),
            "project_ids":    proj_ids,
            "has_cycle":      cycle is not None,
            "cycle":          cycle,
        }


def _d(val: Any) -> str | None:
    """Safely convert a datetime/date to ISO string."""
    if val is None:
        return None
    return str(val)
