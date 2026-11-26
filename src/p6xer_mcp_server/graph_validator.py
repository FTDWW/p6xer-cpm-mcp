"""
Multi-layer graph validator for structured CPM operations.

Validates operations against a ScheduleGraph *before* they are applied.
Runs read-only: never mutates the graph.  Returns structured results
with Chinese natural-language feedback suitable for user display.

Layers:
  1. Task-code existence + fuzzy-match suggestions (edit distance)
  2. Field type / value-range checks (pred_type, cstr_type, status_code, …)

Cycle detection is handled at application time by ``_add_predecessor()``
in schedule_workspace.py (tentative add → DFS → rollback on failure),
matching P6's approach.  No separate pre-check needed.

(Layer 4 — critical-path impact — is intentionally omitted.
 Layer 5 — constraint conflict — belongs after CPM recalculation, not before.)
"""

from __future__ import annotations

import dataclasses
import re
from difflib import get_close_matches
from typing import Any

from .schedule_graph import ScheduleGraph


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VALID_PRED_TYPES = {"PR_FS", "PR_SS", "PR_FF", "PR_SF"}
VALID_STATUSES = {"TK_NotStart", "TK_Active", "TK_Complete"}

# Start-side constraints that are valid for set_constraint
VALID_CSTR_TYPES_START = {
    "CS_MSO", "CS_MSOA", "CS_MANDSTART", "CS_MS", "CS_MEA", "CS_SOA",
}
# Finish-side constraints
VALID_CSTR_TYPES_FINISH = {
    "CS_MFO", "CS_MEOA", "CS_MANDFIN", "CS_MF", "CS_MEO",
    "CS_FNOB", "CS_FOB", "CS_MEB", "CS_MEOB",
}
VALID_CSTR_TYPES = VALID_CSTR_TYPES_START | VALID_CSTR_TYPES_FINISH

# Maximum lag that doesn't trigger a warning (hours)
MAX_REASONABLE_LAG = 87600  # ~10 years


# ═══════════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclasses.dataclass
class ValidationIssue:
    """A single validation problem found in an operation."""

    op_index: int          # which operation in the batch (0-based)
    layer: int             # 1–5
    field: str             # which field has the issue (or "op")
    severity: str          # "error" (blocks) | "warning" (proceed with caution)
    message_cn: str        # human-readable explanation in Chinese
    suggestion: str | None = None   # suggested fix
    candidates: list[str] | None = None  # for Layer 1: fuzzy-match options


@dataclasses.dataclass
class ValidationReport:
    """Output of a full validation pass."""

    passed: bool                               # True = no errors
    issues: list[ValidationIssue] = dataclasses.field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_feedback(self) -> str:
        """Generate Chinese user-facing feedback."""
        if self.passed and not self.warnings:
            return "所有图论验证通过。"
        parts: list[str] = []
        errs = self.errors
        warns = self.warnings

        if errs:
            parts.append(f"### ⚠ 验证失败（{len(errs)} 项错误）\n")
            for e in errs:
                parts.append(f"- **操作 {e.op_index + 1}**：{e.message_cn}")
                if e.candidates:
                    parts.append(f"  候选任务：{', '.join(e.candidates[:5])}")
                if e.suggestion:
                    parts.append(f"  建议：{e.suggestion}")
            parts.append("")

        if warns:
            parts.append(f"### ⚡ 注意事项（{len(warns)} 项）\n")
            for w in warns:
                parts.append(f"- **操作 {w.op_index + 1}**：{w.message_cn}")

        return "\n".join(parts) if parts else "所有图论验证通过。"


# ═══════════════════════════════════════════════════════════════════════════════
# Graph Validator
# ═══════════════════════════════════════════════════════════════════════════════

class GraphValidator:
    """Validate structured operations against a ScheduleGraph.

    Usage::

        validator = GraphValidator(ws.graph)
        report = validator.validate(operations)
        if not report.passed:
            return {"feedback": report.to_feedback(), ...}
        ws.apply_operations(operations)
    """

    def __init__(self, graph: ScheduleGraph) -> None:
        self._graph = graph
        # Pre-compute task_code index for fuzzy matching
        self._all_codes: list[str] = []
        for _tid, node in graph.nodes.items():
            tc = (node.get("task_code") or "").lower()
            if tc:
                self._all_codes.append(tc)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def validate(self, operations: list[dict]) -> ValidationReport:
        """Run all validation layers against *operations*."""
        issues: list[ValidationIssue] = []

        for idx, op in enumerate(operations):
            issues.extend(self._validate_op(idx, op))

        has_errors = any(i.severity == "error" for i in issues)
        return ValidationReport(passed=not has_errors, issues=issues)

    def _validate_op(self, idx: int, op: dict) -> list[ValidationIssue]:
        """Run all layers for a single operation."""
        issues: list[ValidationIssue] = []

        op_type = op.get("op", "")

        # ── Layer 1: Existence ─────────────────────────────────────
        issues.extend(self._check_existence(idx, op, op_type))

        # ── Layer 2: Type / parameter ──────────────────────────────
        issues.extend(self._check_types(idx, op, op_type))

        return issues

    # ------------------------------------------------------------------
    # Layer 1 — task-code existence + fuzzy matching
    # ------------------------------------------------------------------

    def _check_existence(
        self, idx: int, op: dict, op_type: str,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        # Always check task_code
        if "task_code" in op:
            tc = str(op.get("task_code", ""))
            if tc and self._find_tid(tc) is None:
                candidates = self._fuzzy_match(tc)
                issues.append(ValidationIssue(
                    op_index=idx, layer=1, field="task_code",
                    severity="error",
                    message_cn=f"任务编码「{tc}」在进度计划中不存在。",
                    suggestion="请从候选任务中选择，或重新描述任务名称。" if candidates else "请检查任务编码是否正确。",
                    candidates=candidates,
                ))

        # For add/remove predecessor, also check pred_task_code
        if op_type in ("add_predecessor", "remove_predecessor"):
            ptc = str(op.get("pred_task_code", ""))
            if ptc and self._find_tid(ptc) is None:
                candidates = self._fuzzy_match(ptc)
                issues.append(ValidationIssue(
                    op_index=idx, layer=1, field="pred_task_code",
                    severity="error",
                    message_cn=f"紧前任务编码「{ptc}」在进度计划中不存在。",
                    suggestion="请从候选任务中选择。" if candidates else "请检查任务编码是否正确。",
                    candidates=candidates,
                ))

        return issues

    def _find_tid(self, task_code: str) -> int | None:
        """Case-insensitive task_code lookup.  Returns task_id or None."""
        needle = task_code.lower()
        for tid, node in self._graph.nodes.items():
            if (node.get("task_code") or "").lower() == needle:
                return tid
        return None

    def _fuzzy_match(self, task_code: str, n: int = 5) -> list[str]:
        """Return up to *n* similar task_codes by edit distance."""
        needle = task_code.lower()
        # Prefer codes with the same prefix (first 2 chars)
        prefix = needle[:2]
        prefixed = [c for c in self._all_codes if c.startswith(prefix)]
        others = [c for c in self._all_codes if not c.startswith(prefix)]

        # difflib does character-level sequence matching
        from_prefixed = get_close_matches(needle, prefixed, n=n, cutoff=0.0)
        from_others = get_close_matches(needle, others, n=n - len(from_prefixed), cutoff=0.3)
        return (from_prefixed + from_others)[:n]

    # ------------------------------------------------------------------
    # Layer 2 — type / value-range checks
    # ------------------------------------------------------------------

    def _check_types(
        self, idx: int, op: dict, op_type: str,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        if op_type == "add_predecessor":
            pt = op.get("pred_type", "PR_FS")
            if pt not in VALID_PRED_TYPES:
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field="pred_type",
                    severity="error",
                    message_cn=f"无效的逻辑关系类型「{pt}」。",
                    suggestion=f"有效值：{', '.join(sorted(VALID_PRED_TYPES))}",
                ))
            lag = op.get("lag_hours", 0)
            try:
                lag_f = float(lag)
                if abs(lag_f) > MAX_REASONABLE_LAG:
                    issues.append(ValidationIssue(
                        op_index=idx, layer=2, field="lag_hours",
                        severity="warning",
                        message_cn=f"滞后量 {lag_f:.0f} 小时异常大，请确认是否正确。",
                    ))
            except (ValueError, TypeError):
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field="lag_hours",
                    severity="error",
                    message_cn=f"滞后量「{lag}」不是有效数字。",
                ))

        elif op_type in ("set_constraint",):
            ct = op.get("cstr_type", "")
            if ct and ct not in VALID_CSTR_TYPES:
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field="cstr_type",
                    severity="error",
                    message_cn=f"无效的约束类型「{ct}」。",
                    suggestion=f"有效值示例：CS_MSO（开始不早于）、CS_MANDFIN（强制完成）",
                ))
            cd = op.get("cstr_date", "")
            if ct and not cd:
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field="cstr_date",
                    severity="error",
                    message_cn="设置约束时必须提供 cstr_date（格式 YYYY-MM-DD HH:MM）。",
                ))

        elif op_type == "set_status":
            sc = op.get("status_code", "")
            if sc and sc not in VALID_STATUSES:
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field="status_code",
                    severity="error",
                    message_cn=f"无效的状态码「{sc}」。",
                    suggestion=f"有效值：{', '.join(sorted(VALID_STATUSES))}",
                ))

        elif op_type in ("set_duration", "set_duration_days"):
            dur_key = "duration_hours" if op_type == "set_duration" else "duration_days"
            dur_val = op.get(dur_key)
            try:
                if dur_val is not None and float(dur_val) < 0:
                    issues.append(ValidationIssue(
                        op_index=idx, layer=2, field=dur_key,
                        severity="error",
                        message_cn=f"工期「{dur_val}」不能为负数。",
                    ))
            except (ValueError, TypeError):
                issues.append(ValidationIssue(
                    op_index=idx, layer=2, field=dur_key,
                    severity="error",
                    message_cn=f"工期「{dur_val}」不是有效数字。",
                ))

        elif op_type not in ("remove_predecessor",):
            # Unknown op type
            pass  # already caught by _apply_one's dispatch

        return issues

    # ------------------------------------------------------------------
    # Layer 3 — cycle pre-check (read-only simulation)
    # ------------------------------------------------------------------

