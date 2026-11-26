"""
ScheduleWorkspace: in-memory editable copy of an XER schedule.

Wraps a Reader + ScheduleGraph and applies structured operations
(set_duration, add/remove relationships, set_constraint, etc.).

Module-level cache: dict[str, "ScheduleWorkspace"] keyed by workspace_id.
Persists across tool calls within the same MCP process (Cursor session).
"""

from __future__ import annotations

import copy
import datetime as dt
import os
from typing import Any

from .schedule_graph import ScheduleGraph
from .schedule_engine import ScheduleEngine, CalendarHelper, HOURS_PER_DAY
from .xer_writer import XERWriter


# Module-level workspace cache
_workspaces: dict[str, "ScheduleWorkspace"] = {}


def get_workspace(workspace_id: str) -> "ScheduleWorkspace | None":
    return _workspaces.get(workspace_id)


def list_workspaces() -> dict[str, str]:
    return {k: w.source_path for k, w in _workspaces.items()}


def clone_workspace(source_id: str, target_id: str) -> "ScheduleWorkspace":
    """Deep-copy *source_id* workspace to *target_id* in memory (no disk I/O)."""
    import copy
    src = _workspaces.get(source_id)
    if src is None:
        raise ValueError(f"Source workspace '{source_id}' not found.")
    if target_id in _workspaces:
        raise ValueError(f"Target workspace '{target_id}' already exists. "
                         f"Use close_workspace first or choose a different id.")

    # Deep-copy the graph (nodes + edges + adjacency)
    new_graph = copy.deepcopy(src.graph)
    # Deep-copy the calendar helper
    new_cal = copy.deepcopy(src.calendar_helper)
    # Build a new engine on the cloned graph (lightweight)
    from .schedule_engine import ScheduleEngine
    new_engine = ScheduleEngine(new_graph, new_cal)

    # Build new workspace sharing the same reader reference (read-only)
    ws = ScheduleWorkspace.__new__(ScheduleWorkspace)
    ws.workspace_id = target_id
    ws.source_path = src.source_path
    ws.graph = new_graph
    ws.calendar_helper = new_cal
    ws.engine = new_engine
    ws._reader = src._reader
    ws._real_calendars = getattr(src, "_real_calendars", None)
    ws._dirty = True
    ws._operations = list(src._operations)
    ws._cpm_result = copy.deepcopy(src._cpm_result) if src._cpm_result else None
    ws._original_cpm = copy.deepcopy(src._original_cpm) if src._original_cpm else None
    ws._task_map = copy.deepcopy(src._task_map)
    ws.original_path = src.original_path
    ws.source_encoding = src.source_encoding
    ws._cached_proj_id = getattr(src, "_cached_proj_id", None)
    _workspaces[target_id] = ws
    return ws


class ScheduleWorkspace:
    """An editable copy of a schedule, holding graph + engine state."""

    def __init__(self, workspace_id: str, source_path: str, reader,
                 source_encoding: str = "utf-8",
                 original_path: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.source_path = source_path
        self.source_encoding = source_encoding
        self.original_path = original_path or source_path
        self._reader = reader

        # Build the graph
        self.graph = ScheduleGraph.from_reader(reader)

        # Materialize task map for export — reset iterator first
        # (PyP6XER iterator is stateful, but Tasks stores an internal list)
        reader.activities.index = 0
        self._task_map: dict[int, Any] = {}
        for a in reader.activities:
            tid = getattr(a, "task_id", None)
            if tid is not None:
                self._task_map[tid] = a

        self._operations: list[dict] = []  # applied operations log
        self._dirty = False

        # Build CalendarHelper from actual XER calendar data
        day_hr = HOURS_PER_DAY
        reader.calendars.index = 0
        calendars_list = list(reader.calendars)
        for cal in calendars_list:
            d = getattr(cal, "day_hr_cnt", None)
            if d and d > 0:
                day_hr = float(d)
                break
        self.calendar_helper = CalendarHelper(calendars_list, default_hours_per_day=day_hr)
        self.engine = ScheduleEngine(self.graph, self.calendar_helper)

        # Real Calendar objects captured at open.  PyP6XER's Calendars.__iter__
        # is a stateful single-pass iterator (shared self.index, never reset) —
        # the `list(reader.calendars)` above already consumed it, so any later
        # `list(reader.calendars)` returns [].  _create_task reuses this list
        # for CA_Project auto-detect instead of re-iterating the reader.
        self._real_calendars = calendars_list

        # Cached CPM results (None = stale)
        self._cpm_result: dict | None = None
        self._original_cpm: dict | None = None  # immutable diff baseline (captured before any ops)

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    @classmethod
    def open_xer(cls, file_path: str, workspace_id: str = "default") -> "ScheduleWorkspace":
        """Open an XER file into a workspace. Replaces existing workspace if same ID."""
        from xerparser.reader import Reader
        original_path = file_path
        recoded_path, source_enc = cls._recode_if_needed(file_path)
        reader = Reader(recoded_path)
        ws = cls(workspace_id, recoded_path, reader,
                 source_encoding=source_enc, original_path=original_path)
        _workspaces[workspace_id] = ws
        return ws

    @staticmethod
    def _recode_if_needed(file_path: str) -> tuple[str, str]:
        """Return (file_path, encoding) tuple.

        If file is UTF-8, returns unchanged with encoding='utf-8'.
        If GB18030/GBK/CP936, recodes to UTF-8 temp file and returns
        the temp path with the original (non-UTF-8) encoding.

        P6 exports XER in the system ANSI code page. On Chinese Windows
        this is CP936/GB18030; PyP6XER hardcodes UTF-8 so Chinese task
        names get garbled. We need to remember the original encoding so
        the XER writer can match what P6 expects on import.
        """
        import tempfile
        with open(file_path, "rb") as fh:
            raw_data = fh.read()
        # Whole-file strict UTF-8 check, NOT a 4096-byte head check.  The XER
        # TASK section (with the Chinese task names) starts ~20KB in — a GBK
        # file whose first 4096 bytes are ASCII headers would be falsely deemed
        # UTF-8 and passed through un-transcoded, so PyP6XER reads the GBK bytes
        # as UTF-8 and every Chinese name becomes U+FFFD (lossy corruption).
        try:
            raw_data.decode("utf-8")
            return file_path, "utf-8"
        except UnicodeDecodeError:
            pass
        detected_enc = "utf-8"
        for enc in ("gb18030", "gbk", "cp936"):
            try:
                text = raw_data.decode(enc, errors="surrogateescape")
                detected_enc = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            return file_path, "utf-8"
        fd, tmp_path = tempfile.mkstemp(suffix=".xer", prefix="p6xer_")
        os.close(fd)
        with open(tmp_path, "w", encoding="utf-8", errors="replace", newline="") as fh:
            fh.write(text)
        return tmp_path, detected_enc

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def apply_operations(self, operations: list[dict]) -> dict:
        """
        Apply a list of structured operations to the workspace.

        Supported ops:
          - {"op": "set_duration", "task_code": str, "duration_hours": float}
          - {"op": "set_duration_days", "task_code": str, "duration_days": float}
          - {"op": "add_predecessor", "task_code": str, "pred_task_code": str,
              "pred_type": "PR_FS", "lag_hours": float}
          - {"op": "remove_predecessor", "task_code": str, "pred_task_code": str}
          - {"op": "set_constraint", "task_code": str, "cstr_type": str,
              "cstr_date": str (ISO datetime)}
          - {"op": "set_status", "task_code": str, "status_code": str}
          - {"op": "create_task", "task_name": str, "task_code": str (optional),
              "task_type": "TT_Task", "target_drtn_hr_cnt": float,
              "status_code": "TK_NotStart", "clndr_id": int}
            task_code is auto-generated (NEW001, ...) when omitted.
        """
        results = []
        for op in operations:
            try:
                r = self._apply_one(op)
                results.append({"op": op, "result": r})
            except Exception as e:
                results.append({"op": op, "error": str(e)})

        self._operations.extend(operations)
        self._dirty = True
        self._cpm_result = None  # invalidate cached CPM
        return {"applied": len(results), "results": results}

    def _apply_one(self, op: dict) -> str:
        op_type = op.get("op", "")
        if op_type == "set_duration":
            return self._set_duration(op["task_code"], float(op["duration_hours"]))
        elif op_type == "set_duration_days":
            hours = float(op["duration_days"]) * self.calendar_helper.hours_per_day
            return self._set_duration(op["task_code"], hours)
        elif op_type == "add_predecessor":
            return self._add_predecessor(op["task_code"], op["pred_task_code"],
                                         op.get("pred_type", "PR_FS"),
                                         float(op.get("lag_hours", 0)))
        elif op_type == "remove_predecessor":
            return self._remove_predecessor(op["task_code"], op["pred_task_code"])
        elif op_type == "set_constraint":
            return self._set_constraint(op["task_code"], op["cstr_type"], op.get("cstr_date"))
        elif op_type == "set_status":
            return self._set_status(op["task_code"], op["status_code"])
        elif op_type == "create_task":
            return self._create_task(
                op["task_name"],
                task_code=op.get("task_code", ""),
                task_type=op.get("task_type", "TT_Task"),
                status_code=op.get("status_code", "TK_NotStart"),
                target_drtn_hr_cnt=float(op.get("target_drtn_hr_cnt", 0)),
                clndr_id=op.get("clndr_id"),
                wbs_id=op.get("wbs_id"),
                proj_id=op.get("proj_id"),
            )
        elif op_type == "rename":
            return self._rename_task(op["task_code"], op["new_name"])
        elif op_type == "set_wbs":
            return self._set_wbs(op["task_code"], int(op["wbs_id"]))
        elif op_type == "delete_task":
            return self._delete_task(op["task_code"])
        else:
            raise ValueError(f"Unknown operation: {op_type}")

    def _find_node(self, task_code: str) -> int:
        """Find task_id by task_code (case-insensitive)."""
        code_lower = task_code.lower()
        for tid, node in self.graph.nodes.items():
            nc = (node.get("task_code") or "").lower()
            if nc == code_lower:
                return tid
        raise ValueError(f"Task code '{task_code}' not found in graph")

    def _rename_task(self, task_code: str, new_name: str) -> str:
        """Rename an activity's task_name."""
        tid = self._find_node(task_code)
        old_name = self.graph.nodes[tid].get("task_name", "")
        self.graph.nodes[tid]["task_name"] = new_name
        return f"Renamed {task_code}: '{old_name}' → '{new_name}'"

    def _set_wbs(self, task_code: str, wbs_id: int) -> str:
        """Move an activity to a different WBS node."""
        tid = self._find_node(task_code)
        old_wbs = self.graph.nodes[tid].get("wbs_id")
        self.graph.nodes[tid]["wbs_id"] = wbs_id
        self._dirty = True
        self._cpm_result = None
        return f"Set {task_code} wbs_id: {old_wbs} → {wbs_id}"

    def _delete_task(self, task_code: str) -> str:
        """Delete an activity node and all its relationships."""
        tid = self._find_node(task_code)
        task_name = self.graph.nodes[tid].get("task_name", "")
        # Remove all edges touching this task
        before_edges = len(self.graph.edges)
        self.graph.edges = [
            e for e in self.graph.edges
            if e["task_id"] != tid and e["pred_task_id"] != tid
        ]
        rel_removed = before_edges - len(self.graph.edges)
        # Remove from adjacency lists
        self.graph._successors.pop(tid, None)
        self.graph._predecessors.pop(tid, None)
        for succ_list in self.graph._successors.values():
            if tid in succ_list:
                succ_list.remove(tid)
        for pred_list in self.graph._predecessors.values():
            if tid in pred_list:
                pred_list.remove(tid)
        # Remove the node
        self.graph.nodes.pop(tid, None)
        self._dirty = True
        self._cpm_result = None
        return f"Deleted {task_code} '{task_name}' (id={tid}, {rel_removed} rels removed)"

    def _set_duration(self, task_code: str, hours: float) -> str:
        tid = self._find_node(task_code)
        node = self.graph.nodes[tid]
        old_target = float(node.get("target_drtn_hr_cnt", 0) or 0)
        delta = hours - old_target
        node["target_drtn_hr_cnt"] = hours
        status = node.get("status_code", "")
        if status == "TK_NotStart" or status is None:
            node["remain_drtn_hr_cnt"] = hours
        elif status == "TK_Active":
            old_remain = float(node.get("remain_drtn_hr_cnt", 0) or 0)
            node["remain_drtn_hr_cnt"] = max(0.0, old_remain + delta)
        return f"Set {task_code} duration to {hours:.1f}h (delta={delta:+.1f}h)"

    def _add_predecessor(self, task_code: str, pred_task_code: str,
                         pred_type: str = "PR_FS", lag_hours: float = 0) -> str:
        tid = self._find_node(task_code)
        pred_id = self._find_node(pred_task_code)
        # Check duplicate — if exists, update (upsert) instead of rejecting
        for edge in self.graph.edges:
            if edge["task_id"] == tid and edge["pred_task_id"] == pred_id:
                old_type = edge.get("pred_type", "PR_FS")
                old_lag = edge.get("lag_hr_cnt", 0)
                if old_type == pred_type and old_lag == lag_hours:
                    return f"Relationship {pred_task_code} -> {task_code} already exists (unchanged)"
                edge["pred_type"] = pred_type
                edge["lag_hr_cnt"] = lag_hours
                self._dirty = True
                return f"Updated {pred_task_code} -> {task_code}: {old_type} lag={old_lag}h -> {pred_type} lag={lag_hours}h"
        # Cycle check: tentatively add to adjacency, test, then commit or roll back.
        # We modify _successors / _predecessors first because detect_cycle() only
        # reads _successors.  If a cycle is found we undo both; otherwise the edge
        # is committed below.
        self.graph._successors.setdefault(pred_id, []).append(tid)
        self.graph._predecessors.setdefault(tid, []).append(pred_id)

        cycle = self.graph.detect_cycle()
        if cycle is not None:
            # Roll back — this operation would create a cyclic dependency
            self.graph._successors[pred_id].remove(tid)
            if not self.graph._successors[pred_id]:
                del self.graph._successors[pred_id]
            self.graph._predecessors[tid].remove(pred_id)
            if not self.graph._predecessors[tid]:
                del self.graph._predecessors[tid]
            cycle_str = " → ".join(
                str(self.graph.nodes.get(n, {}).get("task_code", n)) for n in cycle
            )
            raise ValueError(
                f"Cycle detected: {cycle_str}. "
                f"Cannot add predecessor {pred_task_code} → {task_code}."
            )

        edge = {
            "task_pred_id": None,
            "pred_task_id": pred_id,
            "task_id": tid,
            "pred_type": pred_type,
            "lag_hr_cnt": lag_hours,
        }
        self.graph.edges.append(edge)
        return f"Added {pred_task_code} ({pred_type}, lag={lag_hours}h) -> {task_code}"

    def _remove_predecessor(self, task_code: str, pred_task_code: str) -> str:
        tid = self._find_node(task_code)
        pred_id = self._find_node(pred_task_code)
        before = len(self.graph.edges)
        self.graph.edges = [
            e for e in self.graph.edges
            if not (e["task_id"] == tid and e["pred_task_id"] == pred_id)
        ]
        if pred_id in self.graph._successors:
            self.graph._successors[pred_id] = [
                s for s in self.graph._successors[pred_id] if s != tid
            ]
        if tid in self.graph._predecessors:
            self.graph._predecessors[tid] = [
                p for p in self.graph._predecessors[tid] if p != pred_id
            ]
        if len(self.graph.edges) < before:
            # 记录 operation，确保 export 时真正删除原始 XER 中的 TASKPRED 行
            # （export 的关系同步只 ADD 不 REMOVE，删除需靠 operations 日志）
            self._operations.append({
                "op": "remove_predecessor",
                "task_code": task_code,
                "pred_task_code": pred_task_code,
            })
            self._dirty = True
            self._cpm_result = None
            return f"Removed {pred_task_code} -> {task_code}"
        return f"No relationship {pred_task_code} -> {task_code} found"

    def _set_constraint(self, task_code: str, cstr_type: str, cstr_date: str | None) -> str:
        tid = self._find_node(task_code)
        if cstr_type is None:
            self.graph.nodes[tid]["cstr_type"] = None
            self.graph.nodes[tid]["cstr_date"] = None
            return f"Removed constraint from {task_code}"
        self.graph.nodes[tid]["cstr_type"] = cstr_type
        self.graph.nodes[tid]["cstr_date"] = cstr_date
        return f"Set constraint {cstr_type}@{cstr_date} on {task_code}"

    def _set_status(self, task_code: str, status_code: str) -> str:
        tid = self._find_node(task_code)
        old = self.graph.nodes[tid].get("status_code")
        self.graph.nodes[tid]["status_code"] = status_code
        return f"Changed {task_code} status: {old} -> {status_code}"

    def _generate_task_code(self) -> str:
        """Return a unique auto-generated task_code (``NEW001``, ``NEW002``, …)."""
        existing = {
            (node.get("task_code") or "").upper()
            for node in self.graph.nodes.values()
        }
        n = 1
        while True:
            code = f"NEW{n:03d}"
            if code not in existing:
                return code
            n += 1

    def _create_task(
        self,
        task_name: str,
        task_code: str = "",
        task_type: str = "TT_Task",
        status_code: str = "TK_NotStart",
        target_drtn_hr_cnt: float = 0.0,
        clndr_id: int | None = None,
        wbs_id: int | None = None,
        proj_id: int | None = None,
    ) -> str:
        """Create a new activity node in the graph.

        If *task_code* is empty, a unique code (``NEW001``, …) is
        generated automatically.
        """
        if not task_code:
            task_code = self._generate_task_code()

        # Check for duplicate task_code
        for node in self.graph.nodes.values():
            if (node.get("task_code") or "").lower() == task_code.lower():
                raise ValueError(
                    f"Task code '{task_code}' already exists "
                    f"(name: {node.get('task_name', '?')})"
                )

        # Generate a new task_id
        new_id = max(self.graph.nodes.keys()) + 1 if self.graph.nodes else 1

        # Use project's default calendar (clndr_type=="CA_Project") as default
        # if not specified.  NOTE: reader.calendars is a Calendars object whose
        # __iter__ is a stateful single-pass iterator (shared self.index, never
        # reset) — it was already consumed in __init__, so `list(...calendars)`
        # here is [].  Reuse self._real_calendars (real Calendar objects) instead.
        if clndr_id is None:
            cals = getattr(self, "_real_calendars", None)
            if cals is None:
                try:
                    cals = list(self._reader.calendars)
                except (AttributeError, TypeError, IndexError):
                    cals = []
            clndr_id = None
            for c in cals:
                if getattr(c, "clndr_type", "") == "CA_Project":
                    clndr_id = c.clndr_id
                    break
            if clndr_id is None and cals:
                clndr_id = cals[0].clndr_id
            # If no real calendar exists, leave clndr_id=None — the engine's
            # _default_cal() fabricates a fallback.  Never the phantom 1.
        if proj_id is None:
            try:
                proj_id = list(self._reader.projects.keys())[0]
            except (AttributeError, IndexError):
                proj_id = 4607  # fallback

        node = {
            "task_id": new_id,
            "task_code": task_code,
            "task_name": task_name,
            "task_type": task_type,
            "status_code": status_code,
            "target_drtn_hr_cnt": target_drtn_hr_cnt,
            "remain_drtn_hr_cnt": target_drtn_hr_cnt,
            "clndr_id": clndr_id,
            "wbs_id": wbs_id,
            "proj_id": proj_id,
            "early_start_date": None, "early_end_date": None,
            "late_start_date": None, "late_end_date": None,
            "total_float_hr_cnt": None, "free_float_hr_cnt": None,
            "act_start_date": None, "act_end_date": None,
            "target_start_date": None, "target_end_date": None,
            "cstr_type": None, "cstr_date": None,
            "cstr_type2": None, "cstr_date2": None,
        }
        self.graph.nodes[new_id] = node
        self.graph._successors[new_id] = []
        self.graph._predecessors[new_id] = []

        self._dirty = True
        self._cpm_result = None
        return f"Created task {task_code} (id={new_id})"

    # ------------------------------------------------------------------
    # CPM
    # ------------------------------------------------------------------

    def recalculate(self) -> dict:
        """Run CPM on the current graph state. Returns summary dict.

        The first call establishes a baseline; subsequent calls after
        apply_operations compare against it in diff().
        """
        cycle = self.graph.detect_cycle()
        if cycle:
            self._cpm_result = {"error": "cycle detected", "cycle": cycle}
            return self._cpm_result

        # Determine project start date
        proj_start = None
        for p in self._reader.projects:
            sd = getattr(p, "scd_start_date", None) or getattr(p, "plan_start_date", None)
            if sd:
                proj_start = _parse_date(sd)
                break
        if proj_start is None:
            # Fall back to earliest activity early_start_date
            earliest_es = None
            for node in self.graph.nodes.values():
                es = node.get("early_start_date")
                if es:
                    d = _parse_date(es)
                    if earliest_es is None or d < earliest_es:
                        earliest_es = d
            proj_start = earliest_es or dt.datetime(2024, 1, 1)

        proj_finish = None
        for p in self._reader.projects:
            ed = getattr(p, "scd_end_date", None) or getattr(p, "plan_end_date", None)
            if ed:
                proj_finish = _parse_date(ed)
                break

        result = self.engine.run(proj_start, proj_finish)
        self._cpm_result = result
        self._dirty = False

        # Capture original CPM baseline before any operations are applied
        if self._original_cpm is None and len(self._operations) == 0:
            import copy
            self._original_cpm = copy.deepcopy(result)

        # Compute summary
        node_count = len(self.graph.nodes)
        critical_count = sum(
            1 for tid in self.graph.nodes
            if result["total_float_hr"].get(tid, 1) <= 0
            and self.graph.nodes[tid].get("status_code") != "TK_Complete"
        )
        max_date = max(
            (v for v in result["ef"].values()),
            key=lambda d: d, default=str(proj_start),
        )
        min_date = min(
            (v for v in result["es"].values()),
            key=lambda d: d, default=str(proj_start),
        )

        return {
            "activity_count":       node_count,
            "edge_count":           len(self.graph.edges),
            "has_cycle":            False,
            "critical_count":       critical_count,
            "schedule_start":       str(min_date),
            "schedule_finish":      str(max_date),
            "project_start_used":   str(proj_start),
        }

    # ------------------------------------------------------------------
    # Public state queries
    # ------------------------------------------------------------------

    @property
    def has_cached_cpm(self) -> bool:
        return self._cpm_result is not None and "error" not in self._cpm_result

    @property
    def operation_count(self) -> int:
        return len(self._operations)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_to_xer(self, output_path: str) -> str:
        """Write workspace to a new XER file using template-based writer.

        Reads the original XER as a template, surgically updates only the
        TASK and TASKPRED rows that changed via CPM and operations, and
        writes P6-compatible output preserving all other sections unchanged.
        """
        if self._cpm_result is None or self._dirty:
            self.recalculate()
        if self._cpm_result and "error" in self._cpm_result:
            raise RuntimeError(f"Cannot export: {self._cpm_result['error']}")

        if not output_path:
            import tempfile
            base = os.path.splitext(os.path.basename(self.source_path))[0]
            fd, output_path = tempfile.mkstemp(
                suffix=".xer", prefix=f"{base}_exported_"
            )
            os.close(fd)

        writer = XERWriter(self.original_path, encoding=self.source_encoding)

        # Sync CPM dates + graph-node fields to TASK rows
        for tid, node in self.graph.nodes.items():
            es_str = self._cpm_result["es"].get(tid)
            ef_str = self._cpm_result["ef"].get(tid)
            ls_str = self._cpm_result["ls"].get(tid)
            lf_str = self._cpm_result["lf"].get(tid)
            tf_val = self._cpm_result["total_float_hr"].get(tid)
            ff_val = self._cpm_result["free_float_hr"].get(tid)

            updates: dict[str, str] = {}

            # Sync basic fields (must have defaults for new tasks)
            tn = node.get("task_name")
            if tn is not None:
                updates["task_name"] = str(tn)
            # phys_complete_pct: required for pyp6xer, default "0"
            pct = node.get("phys_complete_pct")
            updates["phys_complete_pct"] = str(pct) if pct is not None else "0"

            # clndr_id: keep in sync so calendar reassignments (e.g. NEW001-007
            # off the dangling clndr_id=1 onto a real 7-day calendar) are written.
            if node.get("clndr_id") is not None:
                updates["clndr_id"] = str(node["clndr_id"])

            if es_str:
                updates["early_start_date"] = _fmt_xer_date(es_str)
            if ef_str:
                updates["early_end_date"] = _fmt_xer_date(ef_str)
            if ls_str:
                updates["late_start_date"] = _fmt_xer_date(ls_str)
            if lf_str:
                updates["late_end_date"] = _fmt_xer_date(lf_str)
            if tf_val is not None:
                updates["total_float_hr_cnt"] = f"{float(tf_val):.0f}"
            if ff_val is not None:
                updates["free_float_hr_cnt"] = f"{float(ff_val):.0f}"

            tgt_dur = node.get("target_drtn_hr_cnt")
            if tgt_dur is not None:
                updates["target_drtn_hr_cnt"] = f"{float(tgt_dur):.0f}"

            rem_dur = node.get("remain_drtn_hr_cnt")
            if rem_dur is not None:
                updates["remain_drtn_hr_cnt"] = f"{float(rem_dur):.0f}"

            cstr_type = node.get("cstr_type")
            updates["cstr_type"] = str(cstr_type) if cstr_type else ""

            cstr_date = node.get("cstr_date")
            if (
                cstr_date
                and (cstr_type or "").upper() in ("CS_MEOB", "CS_MEO")
            ):
                cstr_date = _finish_constraint_ef_time(
                    cstr_date, self._cpm_result["ef"].get(tid),
                )
            updates["cstr_date"] = _fmt_xer_date(cstr_date) if cstr_date else ""

            cstr_type2 = node.get("cstr_type2")
            updates["cstr_type2"] = str(cstr_type2) if cstr_type2 else ""

            cstr_date2 = node.get("cstr_date2")
            if (
                cstr_date2
                and (cstr_type2 or "").upper() in ("CS_MEOB", "CS_MEO")
            ):
                cstr_date2 = _finish_constraint_ef_time(
                    cstr_date2, self._cpm_result["ef"].get(tid),
                )
            updates["cstr_date2"] = _fmt_xer_date(cstr_date2) if cstr_date2 else ""

            status_code = node.get("status_code")
            if status_code is not None:
                updates["status_code"] = str(status_code)

            if updates:
                try:
                    writer.update_task(tid, updates)
                except KeyError:
                    # New task (created via create_task) — append to TASK section
                    writer.add_task(
                        tid,
                        task_code=node.get("task_code") or f"T{tid}",
                        task_name=node.get("task_name") or "",
                        proj_id=node.get("proj_id") or 0,
                        wbs_id=node.get("wbs_id") or 0,
                        clndr_id=node.get("clndr_id") or 1,
                        task_type=node.get("task_type") or "TT_Task",
                        status_code=node.get("status_code") or "TK_NotStart",
                        target_drtn_hr_cnt=float(node.get("target_drtn_hr_cnt") or 0),
                        remain_drtn_hr_cnt=float(node.get("remain_drtn_hr_cnt") or 0),
                    )
                    # Now update with CPM dates
                    writer.update_task(tid, updates)

        # ── Sync ALL relationships from current graph state ──
        # This is the source of truth.  Writing from graph.edges guarantees
        # new-milestone predecessors persist, regardless of _operations log.
        for e in self.graph.edges:
            pid = e.get("pred_task_id")
            tid = e.get("task_id")
            if pid is None or tid is None:
                continue
            try:
                writer.add_relationship(
                    task_id=tid,
                    pred_task_id=pid,
                    pred_type=e.get("pred_type", "PR_FS"),
                    lag_hr_cnt=float(e.get("lag_hr_cnt", 0)),
                    proj_id=self._get_proj_id(),
                    pred_proj_id=self._get_proj_id(),
                )
            except (ValueError, KeyError):
                continue

        # Apply relationship changes from operations log
        # (legacy path kept for remove_predecessor which graph.edges sync can't express)
        # Reconcile with current graph state: graph.edges is the source of truth.
        # If the edge being removed is present in graph.edges, it was (re-)added
        # after the remove op was logged (e.g. a convergence experiment removed
        # then re-added a predecessor) — skip the replay, or the fresh edge
        # written above would be deleted right back out of the export.
        for op in self._operations:
            op_type = op.get("op", "")
            if op_type == "remove_predecessor":
                try:
                    tid = self._find_node(op["task_code"])
                    pid = self._find_node(op["pred_task_code"])
                    if any(
                        e.get("task_id") == tid and e.get("pred_task_id") == pid
                        for e in self.graph.edges
                    ):
                        continue  # edge currently exists — already written from graph.edges
                    writer.remove_relationship(tid, pid)
                except (ValueError, KeyError):
                    continue

        # Remove TASK rows that no longer exist in the graph
        task_section = writer._editable.get("TASK")
        if task_section is not None:
            xer_task_ids = set(task_section.row_index.keys())
            graph_task_ids = set(self.graph.nodes.keys())
            deleted_ids = xer_task_ids - graph_task_ids
            for dtid in deleted_ids:
                writer.remove_task(dtid)

        writer.write(output_path)
        return output_path

    def _get_proj_id(self) -> int:
        """Extract proj_id from the first task in the XER (cached, idempotent).

        ``self._reader.projects`` is a stateful iterator: ``Projects.__next__``
        advances ``self.index`` and never resets it.  Iterating consumes it, so
        a naive re-iteration returns 0 on the second call — which broke new
        TASKPRED rows (proj_id=0, invisible to P6).  Reset the cursor before
        reading and cache the result.
        """
        cached = getattr(self, "_cached_proj_id", None)
        if cached is not None:
            return cached
        result = 0
        try:
            projects = self._reader.projects
            projects.index = 0  # reset stateful iterator cursor
            for p in projects:
                pid = getattr(p, "proj_id", None)
                if pid:
                    result = int(pid)
                    break
        except Exception:
            result = 0
        self._cached_proj_id = result
        return result

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(self) -> dict:
        """Compare current CPM results against the baseline (first recalculate).

        Only changes caused by apply_operations will appear — if no operations
        have been applied, the diff should be empty.
        """
        if self._cpm_result is None:
            self.recalculate()

        if self._cpm_result and "error" in self._cpm_result:
            return self._cpm_result

        baseline = self._original_cpm
        if baseline is None:
            return {
                "total_activities": len(self.graph.nodes),
                "changed_activities": 0,
                "changes": [],
                "note": "No baseline established. Run recalculate before applying operations to enable diff.",
            }

        changes: list[dict] = []
        for tid, node in self.graph.nodes.items():
            base_es = baseline["es"].get(tid)
            base_ef = baseline["ef"].get(tid)
            base_tf = baseline["total_float_hr"].get(tid)

            new_es = self._cpm_result["es"].get(tid)
            new_ef = self._cpm_result["ef"].get(tid)
            new_tf = self._cpm_result["total_float_hr"].get(tid)

            es_changed = str(base_es) != str(new_es) if base_es or new_es else False
            ef_changed = str(base_ef) != str(new_ef) if base_ef or new_ef else False
            tf_changed = (
                abs(round(float(base_tf or 0), 2) - round(float(new_tf or 0), 2)) > 0.01
            )

            if es_changed or ef_changed or tf_changed:
                changes.append({
                    "task_code":       node.get("task_code"),
                    "task_name":       node.get("task_name"),
                    "baseline_es":     base_es,
                    "new_es":          new_es,
                    "baseline_ef":     base_ef,
                    "new_ef":          new_ef,
                    "baseline_float_hr": base_tf,
                    "new_float_hr":    new_tf,
                    "status_code":     node.get("status_code"),
                })

        return {
            "total_activities":    len(self.graph.nodes),
            "changed_activities":  len(changes),
            "changes":             changes,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_xer_date(val: Any) -> str:
    """Convert a date value to XER format 'YYYY-MM-DD HH:MM'."""
    if val is None:
        return ""
    if isinstance(val, dt.datetime):
        return val.strftime("%Y-%m-%d %H:%M")
    if isinstance(val, dt.date):
        return dt.datetime.combine(val, dt.time(0, 0)).strftime("%Y-%m-%d %H:%M")
    s = str(val).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
    return s  # return as-is if we can't parse


def _finish_constraint_ef_time(cstr_date: Any, ef: Any) -> str:
    """Rewrite a finish-type constraint's time-of-day to the activity's own EF time.

    P6 compares a constraint against its literal timestamp: a CS_MEOB written as
    08:00 while the activity's EF falls at the end of the working day (e.g.
    16:00) is treated as one working day late.  Only the time-of-day is changed;
    the constraint **date is preserved exactly**.  This does not affect the CPM
    result (the engine already normalizes finish constraints to the end of the
    working period); it only fixes the value written back on export.
    """
    # 严格解析；解析不了就原样返回，绝不借用 _parse_date 的 2024-01-01 兜底（会损坏日期）。
    s = str(cstr_date).strip()
    d = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            break
        except (ValueError, TypeError):
            continue
    if d is None:
        return str(cstr_date)
    if ef is None:
        return str(cstr_date)
    # ef 可能是 datetime 或格式化字符串（CPM 结果存的是字符串）
    if isinstance(ef, dt.datetime):
        t = ef.time()
    else:
        t = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                t = dt.datetime.strptime(str(ef).strip(), fmt).time()
                break
            except (ValueError, TypeError):
                continue
        if t is None:
            return str(cstr_date)
    return dt.datetime.combine(d.date(), t).strftime("%Y-%m-%d %H:%M")


def _parse_date(val: Any) -> dt.datetime:
    """Parse various date formats to datetime."""
    if val is None:
        return dt.datetime(2024, 1, 1)
    if isinstance(val, dt.datetime):
        return val
    if isinstance(val, dt.date):
        return dt.datetime.combine(val, dt.time(0, 0))
    s = str(val).strip()
    if not s:
        return dt.datetime(2024, 1, 1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return dt.datetime(2024, 1, 1)
