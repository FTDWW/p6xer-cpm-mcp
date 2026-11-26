# Changelog

## [0.2.0] - 2026-11-26
### Changed
- Published under the new package/repository name `p6xer-cpm-mcp` (PyPI: `p6xer-cpm-mcp`).
- Updated project URLs, author metadata, and the console script to `p6xer-cpm-mcp`.

## Public release (this repository)

### Added
- Deterministic CPM engine (`schedule_engine.py`) with FS/SS/FF/SF relationships,
  lag, P6 calendar arithmetic, start/finish constraints, completed-activity
  anchoring, and zero-duration milestones.
- Editable in-memory workspace (`schedule_workspace.py`) with operation batching,
  preview/diff, and P6-compatible XER write-back (`xer_writer.py`).
- Schedule graph export, cycle detection, and topological sort (`schedule_graph.py`).
- Operation validation (`graph_validator.py`).
- 25 new MCP tools: graph/CPM, workspace management, activity editing, and
  optional AI-assisted schedule analysis.
- Tool provenance table in `README.md` (upstream 13 tools vs. 25 added here).

### Removed
- Private bid/schedule-update workflow, EPC template/knowledge bases, unit-scope
  rules, experience tables, project-specific XER fixtures, and all related tools.
- Local scratch scripts, temporary directories, internal design/plan documents,
  and the previous Git history.

## [0.1.3] - 2026-03-25
### Fixed
- `FastMCP` initialization by removing unsupported `title` keyword argument from
  `src/p6xer_mcp_server/server.py` and moving it into `instructions` text.
- Addressed startup crash `TypeError: FastMCP.__init__() got an unexpected keyword argument 'title'`.

## [0.1.1] - prior release (already tagged)
- Baseline MCP server functionality for parsing Primavera P6 XER files.