"""
Primavera P6 XER MCP Server
============================
A full-featured MCP server for PyP6XER, exposing:
  • 38 Tools   – parse, query, analyze, quality-check, graph-export, CPM recalculate, write-back XER, and AI-powered schedule tools
  • 3 Resources – live project / activities / resources data feeds
  • 2 Prompts   – guided analysis and reporting prompt generators

Run (development / inspector):
    uv run mcp dev src/p6xer_mcp_server/server.py

Run (stdio, for Claude Desktop / Claude Code):
    uv run p6xer-cpm-mcp
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from typing import Any

# ── MCP ──────────────────────────────────────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: mcp not installed. Run: pip install 'mcp[cli]'", file=sys.stderr)
    sys.exit(1)

# ── PyP6XER ──────────────────────────────────────────────────────────────────
try:
    from xerparser.reader import Reader  # type: ignore
except ImportError:
    print("Error: PyP6XER not installed. Run: pip install PyP6XER", file=sys.stderr)
    sys.exit(1)

# ── Schedule Graph / Engine ───────────────────────────────────────────────────
from .schedule_graph import ScheduleGraph
from .schedule_workspace import ScheduleWorkspace, get_workspace, list_workspaces

# ── LLM Client ─────────────────────────────────────────────────────────────────
from .llm_client import DeepSeekClient

def _log_slow(label: str, start: float, threshold: float = 5.0) -> None:
    """Print elapsed seconds to stderr if above threshold — for diagnosing stalls.

    单线程 MCP 服务器上任何工具调用 > 数秒都值得记录，避免下次卡住无从排查。
    """
    el = time.time() - start
    if el > threshold:
        print(f"[p6xer][slow] {label} took {el:.1f}s", file=sys.stderr, flush=True)


mcp = FastMCP(
    "p6xer-cpm-mcp",
    instructions=(
        "P6XER — Primavera P6 Schedule Analyzer\n"
        "Use this server to parse, query, and analyze Primavera P6 XER schedule files. "
        "Start with parse_xer_file to load a file, then use the other tools to query "
        "activities, resources, critical path, earned value, and schedule quality."
    ),
)

# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load(file_path: str) -> Reader:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"XER file not found: {file_path}")
    return Reader(_recode_xer_for_pyp6xer(file_path))


def _recode_xer_for_pyp6xer(file_path: str) -> str:
    """Ensure the XER file is UTF-8 so PyP6XER can read Chinese characters.

    Primavera P6 exports XER files in the system's ANSI code page. On Chinese
    Windows this is CP936/GB18030. PyP6XER hardcodes ``encoding='utf-8'`` so
    Chinese task names get garbled.  This function detects the encoding and
    returns a path to a UTF-8 copy when needed, or the original path if the
    file is already UTF-8.
    """
    # Whole-file strict UTF-8 check, NOT a 4096-byte head check.  The XER
    # TASK section (with the Chinese task names) starts ~20KB in — a GBK
    # file whose first 4096 bytes are ASCII headers would be falsely deemed
    # UTF-8 and passed through un-transcoded, so PyP6XER reads the GBK bytes
    # as UTF-8 and every Chinese name becomes U+FFFD (lossy corruption).
    with open(file_path, "rb") as fh:
        raw_data = fh.read()

    try:
        raw_data.decode("utf-8")
        return file_path  # Already UTF-8
    except UnicodeDecodeError:
        pass

    # Read entire file and decode as GB18030 (superset of GBK/CP936) with
    # surrogateescape for isolated CP1252 bytes (e.g. Euro sign 0x80).

    for enc in ("gb18030", "gbk", "cp936"):
        try:
            text = raw_data.decode(enc, errors="surrogateescape")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return file_path  # Nothing worked — let PyP6XER try its default

    # Write a UTF-8 temp file for PyP6XER.
    # Must use newline='' to preserve original \\r\\n endings — otherwise
    # Python on Windows doubles them to \\r\\r\\n.
    fd, tmp_path = tempfile.mkstemp(suffix=".xer", prefix="p6xer_")
    os.close(fd)
    with open(tmp_path, "w", encoding="utf-8", errors="replace", newline="") as fh:
        fh.write(text)
    # NOTE: temp file intentionally not deleted — Reader opens it lazily
    # in some code paths. Cleaned up by the OS on next reboot.
    return tmp_path


def _s(val: Any) -> Any:
    """Safe JSON-serialisable value."""
    if val is None:
        return None
    try:
        json.dumps(val)
        return val
    except (TypeError, ValueError):
        return str(val)


def _d(val: Any) -> str | None:
    """Stringify a date/None safely."""
    return str(val) if val is not None else None


def _activity_dict(a) -> dict:
    return {
        "task_id":            _s(getattr(a, "task_id", None)),
        "task_code":          _s(getattr(a, "task_code", None)),
        "task_name":          _s(getattr(a, "task_name", None)),
        "status_code":        _s(getattr(a, "status_code", None)),
        "task_type":          _s(getattr(a, "task_type", None)),
        "duration":           _s(getattr(a, "duration", None)),
        "total_float_hr_cnt": _s(getattr(a, "total_float_hr_cnt", None)),
        "free_float_hr_cnt":  _s(getattr(a, "free_float_hr_cnt", None)),
        "phys_complete_pct":  _s(getattr(a, "phys_complete_pct", None)),
        "early_start_date":   _d(getattr(a, "early_start_date", None)),
        "early_end_date":     _d(getattr(a, "early_end_date", None)),
        "late_start_date":    _d(getattr(a, "late_start_date", None)),
        "late_end_date":      _d(getattr(a, "late_end_date", None)),
        "act_start_date":     _d(getattr(a, "act_start_date", None)),
        "act_end_date":       _d(getattr(a, "act_end_date", None)),
        "target_start_date":  _d(getattr(a, "target_start_date", None)),
        "target_end_date":    _d(getattr(a, "target_end_date", None)),
        "wbs_id":             _s(getattr(a, "wbs_id", None)),
        "clndr_id":           _s(getattr(a, "clndr_id", None)),
        "proj_id":            _s(getattr(a, "proj_id", None)),
    }


def _project_dict(p) -> dict:
    activities = list(getattr(p, "activities", []))
    return {
        "proj_id":          _s(getattr(p, "proj_id", None)),
        "proj_short_name":  _s(getattr(p, "proj_short_name", None)),
        "proj_name":        _s(getattr(p, "proj_long_name", getattr(p, "proj_name", None))),
        "scd_start_date":   _d(getattr(p, "scd_start_date", getattr(p, "plan_start_date", None))),
        "scd_end_date":     _d(getattr(p, "scd_end_date",   getattr(p, "plan_end_date",   None))),
        "last_recalc_date": _d(getattr(p, "last_recalc_date", None)),
        "activity_count":   len(activities),
        "completed":   sum(1 for a in activities if getattr(a, "status_code", "") == "TK_Complete"),
        "in_progress": sum(1 for a in activities if getattr(a, "status_code", "") == "TK_Active"),
        "not_started": sum(1 for a in activities if getattr(a, "status_code", "") == "TK_NotStart"),
    }


def _resource_dict(r) -> dict:
    return {
        "rsrc_id":        _s(getattr(r, "rsrc_id", None)),
        "rsrc_name":      _s(getattr(r, "rsrc_name", None)),
        "rsrc_short_name":_s(getattr(r, "rsrc_short_name", None)),
        "rsrc_type":      _s(getattr(r, "rsrc_type", None)),
        "email_addr":     _s(getattr(r, "email_addr", None)),
        "parent_rsrc_id": _s(getattr(r, "parent_rsrc_id", None)),
    }


def _wbs_dict(w) -> dict:
    return {
        "wbs_id":        _s(getattr(w, "wbs_id", None)),
        "wbs_name":      _s(getattr(w, "wbs_name", None)),
        "wbs_short_name":_s(getattr(w, "wbs_short_name", None)),
        "parent_wbs_id": _s(getattr(w, "parent_wbs_id", None)),
        "proj_id":       _s(getattr(w, "proj_id", None)),
        "seq_num":       _s(getattr(w, "seq_num", None)),
    }


def _filter_activities(xer: Reader, project_id: str | None, project_short_name: str | None):
    """Return activities filtered by project_id OR project_short_name (both optional)."""
    acts = list(xer.activities)
    if project_id:
        acts = [a for a in acts if str(getattr(a, "proj_id", "")) == str(project_id)]
    elif project_short_name:
        ids = {
            getattr(p, "proj_id", None) for p in xer.projects
            if getattr(p, "proj_short_name", "") == project_short_name
        }
        acts = [a for a in acts if getattr(a, "proj_id", None) in ids]
    return acts


# ═══════════════════════════════════════════════════════════════════════════════
# TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def parse_xer_file(file_path: str) -> dict:
    """
    Parse a Primavera P6 XER file and return basic project information:
    project list with status breakdowns, total activities, resources, calendars,
    WBS elements, and relationships.
    """
    try:
        xer = _load(file_path)
        projects   = list(xer.projects)
        activities = list(xer.activities)
        resources  = list(xer.resources)
        calendars  = list(xer.calendars)
        wbss       = list(xer.wbss)
        relations  = list(xer.relations)
        return {
            "file_path":           file_path,
            "projects":            [_project_dict(p) for p in projects],
            "total_projects":      len(projects),
            "total_activities":    len(activities),
            "total_resources":     len(resources),
            "total_calendars":     len(calendars),
            "total_wbs":           len(wbss),
            "total_relationships": len(relations),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_project_activities(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Get activities from an XER file.

    Filter options:
    - project_id: numeric P6 project ID
    - project_short_name: project short name string
    - status: TK_NotStart | TK_Active | TK_Complete
    - task_type: TT_Task | TT_Mile | TT_FinMile | TT_WBS
    - limit: max rows returned (default 100)
    """
    try:
        xer = _load(file_path)
        acts = _filter_activities(xer, project_id, project_short_name)
        if status:
            acts = [a for a in acts if getattr(a, "status_code", "") == status]
        if task_type:
            acts = [a for a in acts if getattr(a, "task_type", "") == task_type]
        total = len(acts)
        eff_limit = total if limit <= 0 else limit
        return {
            "file_path":           file_path,
            "project_id":          project_id,
            "project_short_name":  project_short_name,
            "total_matching":      total,
            "returned":            min(total, eff_limit),
            "activities":          [_activity_dict(a) for a in acts[:eff_limit]],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_critical_path(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
) -> dict:
    """
    Find critical path activities (total float <= 0, incomplete only).
    Results are sorted by early start date ascending.
    Accepts either project_id (numeric) or project_short_name as a filter.
    """
    try:
        xer = _load(file_path)
        acts = _filter_activities(xer, project_id, project_short_name)
        critical = sorted(
            [
                a for a in acts
                if getattr(a, "total_float_hr_cnt", None) is not None
                and float(getattr(a, "total_float_hr_cnt", 1)) <= 0
                and getattr(a, "status_code", "") != "TK_Complete"
            ],
            key=lambda a: str(getattr(a, "early_start_date", None) or ""),
        )
        return {
            "file_path":                file_path,
            "project_id":               project_id,
            "project_short_name":       project_short_name,
            "critical_activities_count":len(critical),
            "critical_activities":      [_activity_dict(a) for a in critical],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def analyze_resource_utilization(
    file_path: str,
    max_hours_per_year: float = 2080,
) -> dict:
    """
    Summarize resource utilization: planned/actual hours and costs per resource.
    Labor resources (RT_Labor) are flagged as over_allocated if planned hours
    exceed max_hours_per_year (default 2080 = 40 hrs/week x 52 weeks).
    Results are sorted by planned hours descending.
    """
    try:
        xer = _load(file_path)
        rsrc_map = {r.rsrc_id: r for r in xer.resources}
        util: dict[Any, dict] = {}

        for a in xer.activityresources:
            rid = getattr(a, "rsrc_id", None)
            if rid is None:
                continue
            if rid not in util:
                rsrc = rsrc_map.get(rid)
                util[rid] = {
                    "rsrc_id":          _s(rid),
                    "rsrc_name":        _s(getattr(rsrc, "rsrc_name", None)) if rsrc else None,
                    "rsrc_type":        _s(getattr(rsrc, "rsrc_type", None)) if rsrc else None,
                    "assignments_count":0,
                    "total_target_qty": 0.0,
                    "total_target_cost":0.0,
                    "total_actual_qty": 0.0,
                    "total_actual_cost":0.0,
                }
            u = util[rid]
            u["assignments_count"] += 1
            u["total_target_qty"]  += float(getattr(a, "target_qty",  0) or 0)
            u["total_target_cost"] += float(getattr(a, "target_cost", 0) or 0)
            u["total_actual_qty"]  += float(getattr(a, "act_reg_qty", 0) or 0) + float(getattr(a, "act_ot_qty",  0) or 0)
            u["total_actual_cost"] += float(getattr(a, "act_reg_cost",0) or 0) + float(getattr(a, "act_ot_cost", 0) or 0)

        result = []
        for u in util.values():
            u["total_target_qty"]  = round(u["total_target_qty"],  2)
            u["total_target_cost"] = round(u["total_target_cost"], 2)
            u["total_actual_qty"]  = round(u["total_actual_qty"],  2)
            u["total_actual_cost"] = round(u["total_actual_cost"], 2)
            if u["rsrc_type"] == "RT_Labor":
                u["over_allocated"]  = u["total_target_qty"] > max_hours_per_year
                u["utilization_pct"] = round(u["total_target_qty"] / max_hours_per_year * 100, 1)
            result.append(u)

        result.sort(key=lambda x: x["total_target_qty"], reverse=True)
        return {
            "file_path":           file_path,
            "resources_count":     len(result),
            "max_hours_threshold": max_hours_per_year,
            "resources":           result,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def check_schedule_quality(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
    long_duration_days: float = 20,
    high_float_days: float = 44,
) -> dict:
    """
    DCMA-style schedule quality check on open/in-progress activities.

    Identifies:
    - Activities missing predecessors (excluding start milestones)
    - Activities missing successors (excluding finish milestones)
    - Long-duration activities (threshold: long_duration_days, default 20)
    - High-float activities (threshold: high_float_days, default 44)
    - TT_Task activities without any resource assignment
    - Activities missing early start dates
    """
    try:
        xer = _load(file_path)
        HOURS_PER_DAY = 8.0
        acts = _filter_activities(xer, project_id, project_short_name)
        open_acts = [a for a in acts if getattr(a, "status_code", "") in ("TK_NotStart", "TK_Active")]

        # Use relation table for accurate pred/succ detection
        tasks_with_preds = {getattr(r, "task_id",      None) for r in xer.relations}
        tasks_with_succs = {getattr(r, "pred_task_id", None) for r in xer.relations}
        tasks_with_rsrc  = {getattr(a, "task_id",      None) for a in xer.activityresources}

        issues: dict[str, list] = {
            "no_predecessors":        [],
            "no_successors":          [],
            "long_duration":          [],
            "high_float":             [],
            "no_resource_assignment": [],
            "missing_dates":          [],
        }

        for act in open_acts:
            tid    = getattr(act, "task_id",           None)
            tcode  = getattr(act, "task_code",         "?")
            tname  = getattr(act, "task_name",         "?")
            ttype  = getattr(act, "task_type",         "")
            dur    = getattr(act, "duration",          None)
            tfloat = getattr(act, "total_float_hr_cnt",None)
            estart = getattr(act, "early_start_date",  None)
            ref    = {"task_code": tcode, "task_name": tname}

            if tid not in tasks_with_preds and ttype != "TT_Mile":
                issues["no_predecessors"].append(ref)

            if tid not in tasks_with_succs and ttype not in ("TT_FinMile", "TT_Mile"):
                issues["no_successors"].append(ref)

            if dur is not None:
                dur_days = float(dur) / HOURS_PER_DAY
                if dur_days > long_duration_days:
                    issues["long_duration"].append({**ref, "duration_days": round(dur_days, 1)})

            if tfloat is not None:
                float_days = float(tfloat) / HOURS_PER_DAY
                if float_days > high_float_days:
                    issues["high_float"].append({**ref, "float_days": round(float_days, 1)})

            if ttype == "TT_Task" and tid not in tasks_with_rsrc:
                issues["no_resource_assignment"].append(ref)

            if estart is None and ttype != "TT_Mile":
                issues["missing_dates"].append(ref)

        counts = {k: len(v) for k, v in issues.items()}
        return {
            "file_path":                      file_path,
            "project_id":                     project_id,
            "project_short_name":             project_short_name,
            "total_open_activities_checked":  len(open_acts),
            "total_issues_found":             sum(counts.values()),
            "summary":                        counts,
            "details":                        issues,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_resources(
    file_path: str,
    rsrc_type: str | None = None,
) -> dict:
    """
    List all resources in an XER file.
    Optionally filter by type: RT_Labor | RT_Mat | RT_Equip
    """
    try:
        xer = _load(file_path)
        resources = list(xer.resources)
        if rsrc_type:
            resources = [r for r in resources if getattr(r, "rsrc_type", "") == rsrc_type]
        return {"count": len(resources), "resources": [_resource_dict(r) for r in resources]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_resource_assignments(
    file_path: str,
    rsrc_name: str | None = None,
    task_code: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Get resource-activity assignments, enriched with resource and activity names.
    - rsrc_name: partial match on resource name (case-insensitive)
    - task_code: exact match on activity task code
    """
    try:
        xer = _load(file_path)
        rsrc_map = {r.rsrc_id: r for r in xer.resources}
        task_map = {a.task_id: a for a in xer.activities}
        assignments = list(xer.activityresources)

        if rsrc_name:
            ids = {r.rsrc_id for r in xer.resources
                   if rsrc_name.lower() in (getattr(r, "rsrc_name", "") or "").lower()}
            assignments = [a for a in assignments if getattr(a, "rsrc_id", None) in ids]

        if task_code:
            ids2 = {t.task_id for t in xer.activities
                    if (getattr(t, "task_code", "") or "").lower() == task_code.lower()}
            assignments = [a for a in assignments if getattr(a, "task_id", None) in ids2]

        total = len(assignments)
        result = []
        for a in assignments[:limit]:
            rsrc = rsrc_map.get(getattr(a, "rsrc_id", None))
            task = task_map.get(getattr(a, "task_id", None))
            result.append({
                "taskrsrc_id":  _s(getattr(a,    "taskrsrc_id", None)),
                "task_id":      _s(getattr(a,    "task_id",     None)),
                "task_code":    _s(getattr(task, "task_code",   None)) if task else None,
                "task_name":    _s(getattr(task, "task_name",   None)) if task else None,
                "rsrc_id":      _s(getattr(a,    "rsrc_id",     None)),
                "rsrc_name":    _s(getattr(rsrc,  "rsrc_name",  None)) if rsrc else None,
                "rsrc_type":    _s(getattr(rsrc,  "rsrc_type",  None)) if rsrc else None,
                "target_qty":   _s(getattr(a, "target_qty",     None)),
                "target_cost":  _s(getattr(a, "target_cost",    None)),
                "act_reg_qty":  _s(getattr(a, "act_reg_qty",    None)),
                "act_reg_cost": _s(getattr(a, "act_reg_cost",   None)),
                "act_ot_qty":   _s(getattr(a, "act_ot_qty",     None)),
                "act_ot_cost":  _s(getattr(a, "act_ot_cost",    None)),
            })
        return {"total_matching": total, "returned": len(result), "assignments": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_wbs(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
) -> dict:
    """Get the Work Breakdown Structure (WBS) hierarchy, optionally filtered by project."""
    try:
        xer = _load(file_path)
        wbs_list = list(xer.wbss)
        if project_id:
            wbs_list = [w for w in wbs_list if str(getattr(w, "proj_id", "")) == str(project_id)]
        elif project_short_name:
            ids = {getattr(p, "proj_id", None) for p in xer.projects
                   if getattr(p, "proj_short_name", "") == project_short_name}
            wbs_list = [w for w in wbs_list if getattr(w, "proj_id", None) in ids]
        return {"count": len(wbs_list), "wbs": [_wbs_dict(w) for w in wbs_list]}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_relationships(
    file_path: str,
    task_code: str | None = None,
    limit: int = 100,
) -> dict:
    """
    Get activity predecessor/successor relationships, enriched with task codes.
    If task_code is supplied, returns only relationships where that activity
    appears as the successor or as the predecessor.
    """
    try:
        xer = _load(file_path)
        task_map = {a.task_id: a for a in xer.activities}
        rels = list(xer.relations)

        if task_code:
            target = {t.task_id for t in xer.activities
                      if (getattr(t, "task_code", "") or "").lower() == task_code.lower()}
            rels = [r for r in rels
                    if getattr(r, "task_id",      None) in target
                    or getattr(r, "pred_task_id", None) in target]

        total = len(rels)
        eff_limit = total if limit <= 0 else limit
        result = []
        for r in rels[:eff_limit]:
            task = task_map.get(getattr(r, "task_id",      None))
            pred = task_map.get(getattr(r, "pred_task_id", None))
            result.append({
                "task_pred_id":   _s(getattr(r, "task_pred_id",  None)),
                "task_id":        _s(getattr(r, "task_id",       None)),
                "task_code":      _s(getattr(task, "task_code",  None)) if task else None,
                "pred_task_id":   _s(getattr(r, "pred_task_id",  None)),
                "pred_task_code": _s(getattr(pred, "task_code",  None)) if pred else None,
                "pred_type":      _s(getattr(r, "pred_type",     None)),
                "lag_hr_cnt":     _s(getattr(r, "lag_hr_cnt",    None)),
            })
        return {"total_matching": total, "returned": len(result), "relationships": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_calendars(file_path: str) -> dict:
    """List all calendar definitions in the XER file with hours-per-period data."""
    try:
        xer = _load(file_path)
        cals = [
            {
                "clndr_id":    _s(getattr(c, "clndr_id",    None)),
                "clndr_name":  _s(getattr(c, "clndr_name",  None)),
                "clndr_type":  _s(getattr(c, "clndr_type",  None)),
                "day_hr_cnt":  _s(getattr(c, "day_hr_cnt",  None)),
                "week_hr_cnt": _s(getattr(c, "week_hr_cnt", None)),
                "month_hr_cnt":_s(getattr(c, "month_hr_cnt",None)),
                "year_hr_cnt": _s(getattr(c, "year_hr_cnt", None)),
            }
            for c in xer.calendars
        ]
        return {"count": len(cals), "calendars": cals}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_schedule_summary(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
) -> dict:
    """
    Comprehensive schedule summary: activity counts by status, critical activity count,
    milestone count, resource/calendar/WBS/relationship totals, and date range.
    """
    try:
        xer = _load(file_path)
        acts = _filter_activities(xer, project_id, project_short_name)
        total       = len(acts)
        not_started = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_NotStart")
        in_progress = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_Active")
        completed   = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_Complete")
        milestones  = sum(1 for a in acts if getattr(a, "task_type", "") in ("TT_Mile", "TT_FinMile"))
        critical    = sum(
            1 for a in acts
            if getattr(a, "total_float_hr_cnt", None) is not None
            and float(getattr(a, "total_float_hr_cnt", 1)) <= 0
            and getattr(a, "status_code", "") != "TK_Complete"
        )
        starts  = [getattr(a, "target_start_date", None) or getattr(a, "early_start_date", None) for a in acts]
        ends    = [getattr(a, "target_end_date",   None) or getattr(a, "early_end_date",   None) for a in acts]
        starts  = [d for d in starts if d]
        ends    = [d for d in ends   if d]

        return {
            "file_path":          file_path,
            "project_id":         project_id,
            "project_short_name": project_short_name,
            "total_projects":     len(list(xer.projects)),
            "total_activities":   total,
            "status_breakdown": {
                "not_started": not_started,
                "in_progress": in_progress,
                "completed":   completed,
                "other":       total - not_started - in_progress - completed,
            },
            "milestones":          milestones,
            "critical_activities": critical,
            "total_resources":     len(list(xer.resources)),
            "total_calendars":     len(list(xer.calendars)),
            "total_wbs_elements":  len(list(xer.wbss)),
            "total_relationships": len(list(xer.relations)),
            "schedule_start":      str(min(starts)) if starts else None,
            "schedule_finish":     str(max(ends))   if ends   else None,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_earned_value(
    file_path: str,
    project_id: str | None = None,
    project_short_name: str | None = None,
) -> dict:
    """
    Calculate Earned Value Management (EVM) metrics per project:
    PV (Planned Value), EV (Earned Value), AC (Actual Cost),
    CV (Cost Variance), SV (Schedule Variance), CPI, SPI, EAC.
    """
    try:
        xer = _load(file_path)
        projs = list(xer.projects)
        if project_id:
            projs = [p for p in projs if str(getattr(p, "proj_id", "")) == str(project_id)]
        elif project_short_name:
            projs = [p for p in projs if getattr(p, "proj_short_name", "") == project_short_name]

        task_map = {a.task_id: a for a in xer.activities}
        results = []

        for proj in projs:
            pid      = getattr(proj, "proj_id", None)
            task_ids = {a.task_id for a in xer.activities if getattr(a, "proj_id", None) == pid}
            assigns  = [a for a in xer.activityresources if getattr(a, "task_id", None) in task_ids]

            pv = ev = ac = 0.0
            for a in assigns:
                task = task_map.get(getattr(a, "task_id", None))
                tc   = float(getattr(a, "target_cost",  0) or 0)
                arc  = float(getattr(a, "act_reg_cost", 0) or 0)
                aoc  = float(getattr(a, "act_ot_cost",  0) or 0)
                pct  = float(getattr(task, "phys_complete_pct", 0) or 0) if task else 0.0
                pv  += tc
                ev  += tc * (pct / 100.0)
                ac  += arc + aoc

            cpi = round(ev / ac,  4) if ac > 0 else None
            spi = round(ev / pv,  4) if pv > 0 else None
            eac = round(pv / cpi, 2) if cpi and cpi > 0 else None

            results.append({
                "project":           _s(getattr(proj, "proj_short_name", None)),
                "project_id":        _s(pid),
                "planned_value":     round(pv, 2),
                "earned_value":      round(ev, 2),
                "actual_cost":       round(ac, 2),
                "cost_variance":     round(ev - ac, 2),
                "schedule_variance": round(ev - pv, 2),
                "cpi": cpi,
                "spi": spi,
                "eac": eac,
            })

        return {"projects": results}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_activity_detail(file_path: str, task_code: str) -> dict:
    """
    Get full detail for a single activity by its task_code (case-insensitive).
    Returns all scheduling fields plus enriched predecessors, successors,
    and resource assignments with names and costs.
    """
    try:
        if not task_code or not task_code.strip():
            return {"error": "task_code is required (e.g. 'A1000')."}
        xer = _load(file_path)
        activity = next(
            (a for a in xer.activities
             if (getattr(a, "task_code", "") or "").lower() == task_code.lower()),
            None,
        )
        if activity is None:
            return {"error": f"Activity '{task_code}' not found."}

        tid      = getattr(activity, "task_id", None)
        detail   = _activity_dict(activity)
        task_map = {a.task_id: a for a in xer.activities}
        rsrc_map = {r.rsrc_id: r for r in xer.resources}

        preds = [r for r in xer.relations if getattr(r, "task_id",      None) == tid]
        succs = [r for r in xer.relations if getattr(r, "pred_task_id", None) == tid]

        detail["predecessors"] = [
            {
                "pred_task_code": _s(getattr(task_map.get(getattr(r, "pred_task_id", None)), "task_code", None)),
                "pred_task_name": _s(getattr(task_map.get(getattr(r, "pred_task_id", None)), "task_name", None)),
                "pred_type":      _s(getattr(r, "pred_type",  None)),
                "lag_hr_cnt":     _s(getattr(r, "lag_hr_cnt", None)),
            }
            for r in preds
        ]
        detail["successors"] = [
            {
                "succ_task_code": _s(getattr(task_map.get(getattr(r, "task_id", None)), "task_code", None)),
                "succ_task_name": _s(getattr(task_map.get(getattr(r, "task_id", None)), "task_name", None)),
                "pred_type":      _s(getattr(r, "pred_type",  None)),
                "lag_hr_cnt":     _s(getattr(r, "lag_hr_cnt", None)),
            }
            for r in succs
        ]
        detail["resource_assignments"] = [
            {
                "rsrc_name":    _s(getattr(rsrc_map.get(getattr(a, "rsrc_id", None)), "rsrc_name", None)),
                "rsrc_type":    _s(getattr(rsrc_map.get(getattr(a, "rsrc_id", None)), "rsrc_type", None)),
                "target_qty":   _s(getattr(a, "target_qty",   None)),
                "target_cost":  _s(getattr(a, "target_cost",  None)),
                "act_reg_qty":  _s(getattr(a, "act_reg_qty",  None)),
                "act_reg_cost": _s(getattr(a, "act_reg_cost", None)),
            }
            for a in xer.activityresources if getattr(a, "task_id", None) == tid
        ]
        return detail
    except Exception as e:
        return {"error": str(e)}


# ── Schedule Graph ─────────────────────────────────────────────────────────

@mcp.tool()
def export_schedule_graph(file_path: str, project_id: int | None = None) -> dict:
    """
    Export a schedule graph (nodes and edges) from an XER file as JSON.

    Nodes are activities with scheduling fields (duration, dates, constraints,
    calendar, status). Edges are logical relationships (FS/SS/FF/SF + lag).

    Returns nodes, edges, and metadata including cycle detection results.
    Optionally filter by project_id.
    """
    try:
        xer = _load(file_path)
        g = ScheduleGraph.from_reader(xer, project_id=project_id)
        result = g.to_dict()
        cycle = g.detect_cycle()
        result["metadata"]["has_cycle"] = cycle is not None
        if cycle:
            result["metadata"]["cycle"] = cycle
        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def open_schedule_workspace(file_path: str, workspace_id: str = "default") -> dict:
    """
    Open an XER file into an editable in-memory workspace.

    The workspace persists across tool calls within the same MCP process
    (Cursor session). Use workspace_id to manage multiple concurrent workspaces.

    After opening, use apply_schedule_operations to make changes,
    then recalculate_schedule to run CPM, and preview_schedule_changes
    to see the diff before exporting.
    """
    try:
        ws = ScheduleWorkspace.open_xer(file_path, workspace_id)
        return {
            "workspace_id":      ws.workspace_id,
            "source_path":       ws.source_path,
            "activity_count":    len(ws.graph.nodes),
            "edge_count":        len(ws.graph.edges),
            "hours_per_day":     ws.calendar_helper.hours_per_day,
            "open_workspaces":   list_workspaces(),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def apply_schedule_operations(
    workspace_id: str = "default",
    operations: list[dict] | None = None,
    recalculate: bool = True,
) -> dict:
    """
    Apply structured change operations to an open workspace.

    Supported operations:
      - {"op": "set_duration", "task_code": "A1020", "duration_hours": 80.0}
      - {"op": "set_duration_days", "task_code": "A1020", "duration_days": 10}
      - {"op": "add_predecessor", "task_code": "B2000", "pred_task_code": "A1020",
         "pred_type": "PR_FS", "lag_hours": 0}
        (upsert: updates pred_type/lag if relationship already exists)
      - {"op": "remove_predecessor", "task_code": "B2000", "pred_task_code": "A1020"}
      - {"op": "set_constraint", "task_code": "M1000", "cstr_type": "CS_MEO",
         "cstr_date": "2026-06-01 08:00"}
      - {"op": "set_status", "task_code": "A1020", "status_code": "TK_Active"}

    Set recalculate=false to defer CPM recalculation when batching operations.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found. Use open_schedule_workspace first."}
        if not operations:
            return {"error": "No operations provided."}

        result = ws.apply_operations(operations)

        if recalculate:
            cpm_result = ws.recalculate()
            result["cpm_summary"] = cpm_result

        return result
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def recalculate_schedule(workspace_id: str = "default") -> dict:
    """
    Run CPM forward/backward pass on the workspace and return a summary:
    critical count, schedule start/finish, cycle detection status.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found. Use open_schedule_workspace first."}
        return ws.recalculate()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def preview_schedule_changes(workspace_id: str = "default") -> dict:
    """
    Compare recalculated dates/floats against the original XER values.

    Returns a list of activities where ES, EF, or total float changed.
    Use this to review the impact of changes before calling export_schedule_xer.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found. Use open_schedule_workspace first."}
        return ws.diff()
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def export_schedule_xer(workspace_id: str = "default", output_path: str = "") -> dict:
    """
    Export the workspace to a new XER file with recalculated dates.

    Uses a template-based writer that preserves the original XER file
    structure — only TASK and TASKPRED rows that changed via CPM or
    operations are modified; all other tables pass through unchanged.
    This ensures the exported file can be re-imported into P6.

    If no output_path is given, a temp file is created.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found. Use open_schedule_workspace first."}

        if not ws.has_cached_cpm:
            ws.recalculate()

        if not output_path:
            base, ext = os.path.splitext(ws.source_path)
            output_path = f"{base}_modified{ext}"

        actual = ws.export_to_xer(output_path)
        return {
            "workspace_id":       ws.workspace_id,
            "source_path":        ws.source_path,
            "output_path":        actual,
            "operations_applied": ws.operation_count,
            "activity_count":     len(ws.graph.nodes),
            "edge_count":         len(ws.graph.edges),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Validation Tools ───────────────────────────────────────────────────────

@mcp.tool()
def validate_operations(
    workspace_id: str = "default",
    operations: list[dict] | None = None,
) -> dict:
    """
    Validate operations before applying — MANDATORY pre-check.

    **When to use**: After building all operations (compression + constraints),
    before calling any set_activity_* or add_activity_* tool with recalculate=True.

    **What it does**: Runs graph validation (existence, type checks).
    Does NOT apply anything.  Returns ``passed`` flag and per-issue
    feedback with suggested fixes.

    **Next step**: If ``passed``, proceed to execute the operations.  If
    not, show the ``feedback`` to the user and ask them to correct.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found."}
        if not operations:
            return {"error": "No operations provided."}

        # Accept JSON string (common when called via Anthropic tool use)
        if isinstance(operations, str):
            import json
            try:
                operations = json.loads(operations)
            except json.JSONDecodeError:
                return {"error": "operations 参数格式错误，需要 JSON 数组"}

        from .graph_validator import GraphValidator

        graph_result = GraphValidator(ws.graph).validate(operations)

        return {
            "workspace_id": workspace_id,
            "passed": graph_result.passed,
            "graph": {
                "passed": graph_result.passed,
                "issues": [
                    {"layer": i.layer, "field": i.field, "severity": i.severity,
                     "message": i.message_cn, "suggestion": i.suggestion,
                     "candidates": i.candidates}
                    for i in graph_result.issues
                ],
            },
            "feedback": (
                graph_result.to_feedback() if not graph_result.passed
                else "所有验证通过。"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Atomic Editing Tools ─────────────────────────────────────────────────────

def _validate_and_apply(
    workspace_id: str, op: dict, ws, recalculate: bool = True,
) -> dict:
    """Run GraphValidator on *op*, apply if passed, optionally recalculate."""
    from .graph_validator import GraphValidator

    graph_r = GraphValidator(ws.graph).validate([op])

    if not graph_r.passed:
        return {
            "applied": False,
            "graph_passed": False,
            "feedback": graph_r.to_feedback().strip(),
            "graph_issues": [
                {"layer": i.layer, "field": i.field, "severity": i.severity,
                 "message": i.message_cn, "suggestion": i.suggestion}
                for i in graph_r.issues
            ],
        }

    # Apply
    result = ws._apply_one(op)
    cpm = ws.recalculate() if recalculate else None

    # Include task_name so the Agent sees semantic context, not just a code
    tid = ws._find_node(op["task_code"])
    task_name = ws.graph.nodes[tid].get("task_name", "")

    return {
        "applied": True,
        "operation": op,
        "result": result,
        "task_name": task_name,
        "cpm_summary": cpm,
    }


@mcp.tool()
def set_activity_duration(
    workspace_id: str = "default",
    task_code: str = "",
    duration_hours: float = 0.0,
    recalculate: bool = True,
) -> dict:
    """
    Set the target duration of a single activity in hours.

    duration_hours: positive, e.g. 80.0 ≈ 10 working days @ 8h/day.
    Returns ``{"applied": true/false, ...}`` — check ``applied`` before proceeding.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "set_duration", "task_code": task_code,
        "duration_hours": duration_hours,
    }, ws, recalculate)


@mcp.tool()
def set_activity_duration_days(
    workspace_id: str = "default",
    task_code: str = "",
    duration_days: float = 0.0,
    recalculate: bool = True,
) -> dict:
    """
    Set the target duration of a single activity in calendar days.

    duration_days: positive.  Internally converted to hours via the project's
    hours-per-day setting (e.g. 10d × 8h/d = 80h).
    Returns ``{"applied": true/false, ...}``.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "set_duration_days", "task_code": task_code,
        "duration_days": duration_days,
    }, ws, recalculate)


@mcp.tool()
def add_activity_predecessor(
    workspace_id: str = "default",
    task_code: str = "",
    pred_task_code: str = "",
    pred_type: str = "PR_FS",
    lag_hours: float = 0.0,
    recalculate: bool = True,
) -> dict:
    """
    Add or update a predecessor relationship.

    pred_type: "PR_FS" | "PR_SS" | "PR_FF" | "PR_SF".
    lag_hours: positive = lag (gap), negative = lead (overlap).
    If the relationship already exists it is updated (upsert).
    Cycle detection runs automatically — rejects with feedback on cycle.
    Returns ``{"applied": true/false, ...}``.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "add_predecessor", "task_code": task_code,
        "pred_task_code": pred_task_code,
        "pred_type": pred_type, "lag_hours": lag_hours,
    }, ws, recalculate)


@mcp.tool()
def remove_activity_predecessor(
    workspace_id: str = "default",
    task_code: str = "",
    pred_task_code: str = "",
    recalculate: bool = True,
) -> dict:
    """
    Remove a predecessor relationship.

    Silently succeeds if the relationship does not exist (no error).
    Returns ``{"applied": true/false, ...}``.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "remove_predecessor", "task_code": task_code,
        "pred_task_code": pred_task_code,
    }, ws, recalculate)


@mcp.tool()
def set_activity_constraint(
    workspace_id: str = "default",
    task_code: str = "",
    cstr_type: str = "",
    cstr_date: str = "",
    recalculate: bool = True,
) -> dict:
    """
    Set/remove a date constraint. ONLY when user explicitly requests a hard date.

    Use ONLY when the user says: "开始不晚于X", "完成不早于X", "必须在X日前完成",
    "强制X日开始".  For general "提前/推迟", compress the blocking-chain durations
    instead — do NOT add a constraint as the first action.

    cstr_type: "CS_MSO" (start on or after) | "CS_MANDFIN" (mandatory finish)
    | "CS_MEO" (mandatory finish) | "CS_FNOB" (finish no later than).
    cstr_date: "YYYY-MM-DD HH:MM".
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "set_constraint", "task_code": task_code,
        "cstr_type": cstr_type or None, "cstr_date": cstr_date,
    }, ws, recalculate)


@mcp.tool()
def set_activity_status(
    workspace_id: str = "default",
    task_code: str = "",
    status_code: str = "",
    recalculate: bool = True,
) -> dict:
    """
    Set the status of an activity.

    status_code: "TK_NotStart" | "TK_Active" | "TK_Complete".
    ``TK_Complete`` anchors the activity to its actual dates (no longer recalculated).
    Returns ``{"applied": true/false, ...}``.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return _validate_and_apply(workspace_id, {
        "op": "set_status", "task_code": task_code,
        "status_code": status_code,
    }, ws, recalculate)


@mcp.tool()
def create_activity(
    workspace_id: str = "default",
    task_name: str = "",
    task_code: str = "",
    task_type: str = "TT_Task",
    duration_hours: float = 0.0,
    status_code: str = "TK_NotStart",
    clndr_id: int = 0,
    wbs_id: int = 0,
) -> dict:
    """
    Create a new activity in the schedule.

    If ``task_code`` is left empty the system auto-generates a unique code
    (``NEW001``, ``NEW002``, …).  Always check the returned ``task_code``
    so you can reference the new activity in subsequent calls.

    After creation use ``add_activity_predecessor`` and
    ``set_activity_constraint`` to link the new activity into the schedule
    network, then ``recalculate_schedule`` to update CPM dates.

    Parameters
    ----------
    task_name:
        Human-readable name (e.g. "锅炉保温施工").  Required.
    task_code:
        Optional unique code.  If empty, auto-generated (``NEW001`` …).
    task_type:
        "TT_Task" (default), "TT_Mile", or "TT_FinMile".
    duration_hours:
        Planned duration in working hours.  80h ≈ 10 days @ 8h/day.
    status_code:
        "TK_NotStart" (default), "TK_Active", or "TK_Complete".
    clndr_id:
        Calendar ID.  0 = auto-detect from the project's first calendar.
    wbs_id:
        Target WBS id for the new activity.  0 = auto (leave unset); pass a
        real wbs_id to place the activity into a specific WBS node.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}

    op = {
        "op": "create_task",
        "task_name": task_name,
        "task_code": task_code if task_code else "",
        "task_type": task_type,
        "target_drtn_hr_cnt": duration_hours,
        "status_code": status_code,
        "clndr_id": clndr_id if clndr_id else None,
        "wbs_id": wbs_id if wbs_id else None,
    }

    try:
        result = ws._apply_one(op)
        cpm = ws.recalculate()
        # Extract the auto-generated task_code from the result message
        import re
        m = re.search(r"Created task (\S+)", result)
        assigned_code = m.group(1) if m else task_code
        return {"created": True, "task_code": assigned_code, "result": result, "cpm_summary": cpm}
    except Exception as e:
        return {"created": False, "task_name": task_name, "error": str(e)}


# ── Workspace Management ─────────────────────────────────────────────────────

@mcp.tool()
def clone_workspace(
    source_id: str,
    target_id: str,
) -> dict:
    """
    Deep-copy an open workspace in memory (no disk I/O).

    Creates an independent copy under *target_id*.  The original workspace
    under *source_id* is left untouched.  Use this before making experimental
    changes to a reference schedule.

    Returns basic stats about the cloned workspace.
    """
    try:
        from .schedule_workspace import clone_workspace as _clone
        ws = _clone(source_id, target_id)
        return {
            "cloned": True,
            "source_id": source_id,
            "target_id": target_id,
            "activity_count": len(ws.graph.nodes),
            "edge_count": len(ws.graph.edges),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def delete_activity(
    workspace_id: str = "default",
    task_code: str = "",
) -> dict:
    """
    Delete an activity and all its relationships from the workspace.

    Removes the node and every edge touching it (predecessors/successors).
    Use to remove leftover reference milestone nodes from the milestone WBS
    during Step 2 alignment (the ``extra`` nodes reported by
    ``verify_milestone_alignment``).

    ⚠️ Deleting a task that others depend on severs those chains — re-link
    downstream activities afterward if the deletion strands them.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found."}
        if not task_code.strip():
            return {"error": "task_code is required."}
        result = ws._delete_task(task_code.strip())
        return {"deleted": True, "result": result}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def rename_activity(
    workspace_id: str = "default",
    task_code: str = "",
    new_name: str = "",
) -> dict:
    """
    Rename an activity's task_name.

    Use this when an imported schedule uses different naming conventions
    from a reference schedule.  Returns old and new names.
    """
    try:
        ws = get_workspace(workspace_id)
        if ws is None:
            return {"error": f"Workspace '{workspace_id}' not found."}
        if not task_code or not new_name.strip():
            return {"error": "task_code and new_name are required."}

        tid = ws._find_node(task_code)
        old_name = ws.graph.nodes[tid].get("task_name", "")
        ws.apply_operations([{"op": "rename", "task_code": task_code,
                              "new_name": new_name.strip()}])
        return {
            "renamed": True,
            "task_code": task_code,
            "old_name": old_name,
            "new_name": new_name.strip(),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_milestones(workspace_id: str = "default") -> dict:
    """
    Return all milestone tasks (TT_Mile, TT_FinMile) sorted by early start.

    Milestones are usually grouped under a dedicated WBS node (e.g. "里程碑").
    Prefer milestones on the critical path (TF=0) over those with float.

    Call this FIRST for reschedule_project to get the project skeleton
    (first concrete, commissioning, 168h) in one shot.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    _t0 = time.time()

    milestones = []
    for tid, node in ws.graph.nodes.items():
        tt = node.get("task_type", "")
        if tt not in ("TT_Mile", "TT_FinMile"):
            continue
        name = node.get("task_name") or ""
        milestones.append({
            "task_code": node.get("task_code"),
            "task_name": name,
            "task_type": tt,
            "early_start": node.get("early_start_date"),
            "early_finish": node.get("early_end_date"),
            "late_finish": node.get("late_end_date"),
            "total_float_hr": node.get("total_float_hr_cnt"),
        })

    milestones.sort(key=lambda m: str(m.get("early_start", "")))
    _log_slow("get_milestones", _t0)
    return {"total": len(milestones), "milestones": milestones}


# ── Workspace Management ────────────────────────────────────────────────────

@mcp.tool()
def close_workspace(workspace_id: str = "default") -> dict:
    """Close a workspace and free its memory."""
    from .schedule_workspace import _workspaces
    if workspace_id in _workspaces:
        del _workspaces[workspace_id]
        return {"closed": True, "workspace_id": workspace_id}
    return {"closed": False, "error": f"Workspace '{workspace_id}' not found."}


@mcp.tool()
def list_workspaces() -> dict:
    """List all open workspaces."""
    from .schedule_workspace import list_workspaces as _list
    return {"workspaces": _list()}


@mcp.tool()
def get_workspace_info(workspace_id: str = "default") -> dict:
    """Return metadata about a workspace."""
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    return {
        "workspace_id": ws.workspace_id,
        "source_path": ws.source_path,
        "original_path": ws.original_path,
        "activity_count": len(ws.graph.nodes),
        "edge_count": len(ws.graph.edges),
        "hours_per_day": ws.calendar_helper.hours_per_day,
        "operation_count": ws.operation_count,
        "has_cached_cpm": ws.has_cached_cpm,
    }


# ── CPM Inspection Tools ─────────────────────────────────────────────────────

@mcp.tool()
def get_activity_cpm_dates(
    workspace_id: str = "default",
    task_code: str = "",
) -> dict:
    """
    Get CPM dates (ES/EF/LS/LF/TF/FF) for a single activity.

    Returns the dates from the most recent recalculation.  Returns an
    error if the workspace hasn't been recalculated yet.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    if not ws.has_cached_cpm:
        return {"error": "No CPM results. Run recalculate_schedule first."}

    tid = ws._find_node(task_code)
    cpm = ws._cpm_result
    task_name = ws.graph.nodes[tid].get("task_name", "")
    return {
        "task_code": task_code,
        "task_name": task_name,
        "early_start": cpm["es"].get(tid),
        "early_finish": cpm["ef"].get(tid),
        "late_start": cpm["ls"].get(tid),
        "late_finish": cpm["lf"].get(tid),
        "total_float_hr": cpm["total_float_hr"].get(tid),
        "free_float_hr": cpm["free_float_hr"].get(tid),
        "is_critical": (cpm["total_float_hr"].get(tid, 1) or 0) <= 0,
    }


@mcp.tool()
def list_activities_after_cpm(
    workspace_id: str = "default",
    sort_by: str = "early_start",
    limit: int = 100,
) -> dict:
    """
    List all activities with their CPM dates, sorted and paginated.

    sort_by: "early_start" (default), "total_float", "late_start", "late_finish"
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}
    if not ws.has_cached_cpm:
        return {"error": "No CPM results. Run recalculate_schedule first."}

    cpm = ws._cpm_result
    rows = []
    for tid, node in ws.graph.nodes.items():
        rows.append({
            "task_code": node.get("task_code"),
            "task_name": node.get("task_name"),
            "task_type": node.get("task_type"),
            "status_code": node.get("status_code"),
            "early_start": cpm["es"].get(tid),
            "early_finish": cpm["ef"].get(tid),
            "late_start": cpm["ls"].get(tid),
            "late_finish": cpm["lf"].get(tid),
            "total_float_hr": cpm["total_float_hr"].get(tid),
            "free_float_hr": cpm["free_float_hr"].get(tid),
        })

    # Sort
    key_map = {
        "early_start": "early_start",
        "total_float": "total_float_hr",
        "late_start": "late_start",
        "late_finish": "late_finish",
    }
    sort_key = key_map.get(sort_by, "early_start")
    rows.sort(key=lambda r: str(r.get(sort_key, "")))

    total = len(rows)
    rows = rows[:limit] if limit > 0 else rows
    return {"total": total, "returned": len(rows), "sort_by": sort_key, "activities": rows}


@mcp.tool()
def run_cpm_diagnostics(workspace_id: str = "default") -> dict:
    """
    Run diagnostics on the schedule graph: cycle detection, orphan
    activities (no predecessors / no successors), and CPM summary.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return {"error": f"Workspace '{workspace_id}' not found."}

    cycle = ws.graph.detect_cycle()
    cycle_str = " → ".join(
        str(ws.graph.nodes.get(n, {}).get("task_code", n))
        for n in (cycle or [])
    ) if cycle else None

    no_pred: list[str] = []
    no_succ: list[str] = []
    for tid, node in ws.graph.nodes.items():
        tc = node.get("task_code", str(tid))
        if not ws.graph._predecessors.get(tid):
            no_pred.append(tc)
        if not ws.graph._successors.get(tid):
            no_succ.append(tc)

    cpm = None
    if ws.has_cached_cpm:
        cpm = ws.recalculate()

    return {
        "has_cycle": cycle is not None,
        "cycle": cycle_str,
        "no_predecessors": no_pred[:20],
        "no_predecessors_count": len(no_pred),
        "no_successors": no_succ[:20],
        "no_successors_count": len(no_succ),
        "cpm_summary": cpm,
    }


@mcp.tool()
def analyze_schedule_ai(
    file_path: str,
    question: str = "分析这个进度计划的主要风险和问题",
) -> dict:
    """
    Send schedule context to DeepSeek AI for intelligent analysis.

    Gathers schedule summary, critical path, quality check results, and
    earned value data from the XER file, then sends it all to the LLM
    with your question. Returns the AI's analysis as markdown text.

    Use this for open-ended questions like:
    - "这个进度计划有什么风险？"
    - "哪些任务最可能延期？"
    - "如何优化这个进度？"
    """
    try:
        xer = _load(file_path)
        activities = list(xer.activities)

        # Gather context data
        projects = [_project_dict(p) for p in list(xer.projects)]

        # Status breakdown
        not_started = sum(1 for a in activities if getattr(a, "status_code", "") == "TK_NotStart")
        in_progress = sum(1 for a in activities if getattr(a, "status_code", "") == "TK_Active")
        completed = sum(1 for a in activities if getattr(a, "status_code", "") == "TK_Complete")
        milestones = sum(1 for a in activities if getattr(a, "task_type", "") in ("TT_Mile", "TT_FinMile"))

        # Critical path
        critical = [
            a for a in activities
            if getattr(a, "total_float_hr_cnt", None) is not None
            and float(getattr(a, "total_float_hr_cnt", 1)) <= 0
            and getattr(a, "status_code", "") != "TK_Complete"
        ]

        # Quality check summary
        open_acts = [a for a in activities if getattr(a, "status_code", "") in ("TK_NotStart", "TK_Active")]
        tasks_with_preds = {getattr(r, "task_id", None) for r in xer.relations}
        tasks_with_succs = {getattr(r, "pred_task_id", None) for r in xer.relations}
        no_pred = sum(1 for a in open_acts if getattr(a, "task_id", None) not in tasks_with_preds)
        no_succ = sum(1 for a in open_acts if getattr(a, "task_id", None) not in tasks_with_succs)

        # Date range
        starts = [getattr(a, "early_start_date", None) for a in activities]
        ends = [getattr(a, "early_end_date", None) for a in activities]
        starts = [d for d in starts if d]
        ends = [d for d in ends if d]

        context = {
            "file_path": file_path,
            "project": projects,
            "summary": {
                "total_activities": len(activities),
                "not_started": not_started,
                "in_progress": in_progress,
                "completed": completed,
                "milestones": milestones,
                "critical_activities": len(critical),
                "total_relationships": len(list(xer.relations)),
                "schedule_start": str(min(starts)) if starts else None,
                "schedule_finish": str(max(ends)) if ends else None,
            },
            "quality": {
                "open_activities": len(open_acts),
                "missing_predecessors": no_pred,
                "missing_successors": no_succ,
            },
        }

        # Top 10 critical activities
        context["top_critical"] = [
            {
                "task_code": getattr(a, "task_code", None),
                "task_name": getattr(a, "task_name", None),
                "total_float_hr": getattr(a, "total_float_hr_cnt", None),
                "early_start": str(getattr(a, "early_start_date", None)),
                "early_end": str(getattr(a, "early_end_date", None)),
            }
            for a in sorted(critical, key=lambda a: float(getattr(a, "total_float_hr_cnt", 9999) or 9999))[:10]
        ]

        system_prompt = (
            "You are a Primavera P6 schedule analyst. You receive schedule data "
            "from an XER file and answer questions about the schedule. "
            "Provide concise, actionable analysis in Chinese. "
            "Focus on: critical path risks, logic issues, resource concerns, "
            "milestone achievability, and practical recommendations. "
            "Format your response in Markdown with clear sections."
        )

        client = DeepSeekClient()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Schedule Context:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\nQuestion: {question}"},
        ]

        try:
            analysis = client.chat(messages, temperature=0.3, max_tokens=4096)
        except Exception as e:
            return {"error": f"LLM call failed: {e}", "context": context}

        return {
            "file_path": file_path,
            "question": question,
            "context_summary": context["summary"],
            "analysis": analysis,
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# RESOURCES  (live URI-addressed data feeds)
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.resource("xer-project://{file_path}/{project_id}")
def xer_project_resource(file_path: str, project_id: str) -> str:
    """Detailed text summary for a specific project from an XER file."""
    try:
        xer  = _load(file_path)
        proj = next(
            (p for p in xer.projects if str(getattr(p, "proj_id", "")) == str(project_id)),
            None,
        )
        if not proj:
            return f"Project '{project_id}' not found in {file_path}"

        acts      = list(getattr(proj, "activities", []))
        completed = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_Complete")
        in_prog   = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_Active")
        not_start = sum(1 for a in acts if getattr(a, "status_code", "") == "TK_NotStart")
        critical  = sum(
            1 for a in acts
            if getattr(a, "total_float_hr_cnt", None) is not None
            and float(getattr(a, "total_float_hr_cnt", 1)) <= 0
            and getattr(a, "status_code", "") != "TK_Complete"
        )
        scd_start = getattr(proj, "scd_start_date", getattr(proj, "plan_start_date", "N/A"))
        scd_end   = getattr(proj, "scd_end_date",   getattr(proj, "plan_end_date",   "N/A"))

        return (
            f"Project Details\n"
            f"===============\n"
            f"ID:           {proj.proj_id}\n"
            f"Short Name:   {getattr(proj, 'proj_short_name', 'N/A')}\n"
            f"Full Name:    {getattr(proj, 'proj_long_name', getattr(proj, 'proj_name', 'N/A'))}\n"
            f"Start Date:   {scd_start}\n"
            f"Finish Date:  {scd_end}\n"
            f"\nActivity Summary\n"
            f"----------------\n"
            f"Total:        {len(acts)}\n"
            f"Not Started:  {not_start}\n"
            f"In Progress:  {in_prog}\n"
            f"Completed:    {completed}\n"
            f"Critical:     {critical}\n"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("xer-activities://{file_path}")
def xer_activities_resource(file_path: str) -> str:
    """Activities summary: status breakdown and duration statistics for an XER file."""
    try:
        xer  = _load(file_path)
        acts = list(xer.activities)

        status_counts: dict[str, int] = {}
        for a in acts:
            s = getattr(a, "status_code", "Unknown")
            status_counts[s] = status_counts.get(s, 0) + 1

        durations = [float(getattr(a, "duration", 0) or 0) for a in acts if getattr(a, "duration", None)]
        dur_section = ""
        if durations:
            avg = sum(durations) / len(durations)
            dur_section = (
                f"\nDuration Statistics (hours)\n"
                f"---------------------------\n"
                f"Average: {avg:.1f}\n"
                f"Max:     {max(durations):.1f}\n"
                f"Min:     {min(durations):.1f}\n"
            )

        status_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(status_counts.items()))
        return (
            f"Activities Summary\n"
            f"==================\n"
            f"File:  {file_path}\n"
            f"Total: {len(acts)}\n"
            f"\nStatus Breakdown\n"
            f"----------------\n"
            f"{status_lines}\n"
            f"{dur_section}"
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("xer-resources://{file_path}")
def xer_resources_resource(file_path: str) -> str:
    """Resources summary: type breakdown and assignment statistics for an XER file."""
    try:
        xer       = _load(file_path)
        resources = list(xer.resources)

        type_counts: dict[str, int] = {}
        for r in resources:
            rt = getattr(r, "rsrc_type", "Unknown")
            type_counts[rt] = type_counts.get(rt, 0) + 1

        total_assigns = len(list(xer.activityresources))
        avg_assign    = total_assigns / len(resources) if resources else 0

        type_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(type_counts.items()))
        return (
            f"Resources Summary\n"
            f"=================\n"
            f"File:              {file_path}\n"
            f"Total Resources:   {len(resources)}\n"
            f"\nResource Types\n"
            f"--------------\n"
            f"{type_lines}\n"
            f"\nAssignment Statistics\n"
            f"---------------------\n"
            f"Total Assignments:            {total_assigns}\n"
            f"Avg Assignments per Resource: {avg_assign:.1f}\n"
        )
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# PROMPTS  (guided AI analysis starters)
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.prompt()
def analyze_xer_project(file_path: str, analysis_type: str = "general") -> str:
    """
    Generate an analysis prompt for a Primavera P6 XER project file.

    analysis_type options:
    - general    – comprehensive overview (default)
    - schedule   – critical path, milestones, timeline risks
    - resources  – allocation, over-allocation, optimisation
    - progress   – completion %, earned value, variances
    - quality    – DCMA-style logic and schedule health
    """
    focus_map = {
        "general":  "Provide a comprehensive analysis: project overview, schedule health, resource utilisation, and key insights.",
        "schedule": "Analyse the schedule: critical path activities, milestone tracking, schedule quality issues, and timeline risks.",
        "resources":"Analyse resource allocation and utilisation patterns, flag over-allocated resources, and suggest optimisation opportunities.",
        "progress": "Analyse project progress via completion percentages, earned value metrics (CPI/SPI), schedule/cost variances, and forecast to complete.",
        "quality":  "Perform a DCMA-style schedule quality assessment: missing logic, inappropriate constraints, long durations, high float, and best-practice violations.",
    }
    focus = focus_map.get(analysis_type, focus_map["general"])
    return (
        f"Please analyse the Primavera P6 XER file at: {file_path}\n\n"
        f"Analysis focus ({analysis_type}):\n{focus}\n\n"
        f"Use the available MCP tools — parse_xer_file, get_project_activities, "
        f"get_critical_path, analyze_resource_utilization, check_schedule_quality, "
        f"get_earned_value — to gather the data, then present a clear, structured report."
    )


@mcp.prompt()
def xer_reporting_prompt(file_path: str, report_type: str = "executive") -> str:
    """
    Generate a prompt for creating a professional project report from XER data.

    report_type options:
    - executive      – high-level KPIs and risks for stakeholders (default)
    - detailed       – full activity, resource, and schedule detail
    - critical_path  – critical activities and risk mitigation
    - resource       – utilisation, costs, and optimisation
    - milestone      – milestone status and forecast dates
    """
    report_map = {
        "executive":    "Create an executive summary: key metrics (CPI, SPI, % complete), top risks, and recommendations for senior stakeholders.",
        "detailed":     "Create a detailed report covering all activities, resource assignments, schedule dates, float values, and variance analysis.",
        "critical_path":"Create a critical path report: all critical activities, their dates, float values, schedule risks, and recommended mitigation.",
        "resource":     "Create a resource utilisation report: planned vs actual hours and costs per resource, over-allocation flags, and optimisation recommendations.",
        "milestone":    "Create a milestone tracking report: all milestone activities, planned vs actual dates, and any forecast delays.",
    }
    focus = report_map.get(report_type, report_map["executive"])
    return (
        f"Generate a professional {report_type.title()} Report from: {file_path}\n\n"
        f"{focus}\n\n"
        f"Structure the report with: Executive Summary, Key Findings, Detailed Analysis, "
        f"Risks & Issues, and Recommendations. Use MCP tool data to populate each section."
    )


# schedule_edit_prompt removed — workflows now in classify_intent's system prompt.


def run() -> None:
    """Entry point for the MCP server (pyproject [project.scripts])."""
    mcp.run()


if __name__ == "__main__":
    run()
