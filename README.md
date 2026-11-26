# P6XER CPM MCP Server

mcp-name: io.github.FTDWW/p6xer-cpm-mcp

[![PyPI version](https://badge.fury.io/py/p6xer-cpm-mcp.svg)](https://badge.fury.io/py/p6xer-cpm-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-green.svg)](https://modelcontextprotocol.io)

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server for **Primavera P6 XER** schedule files, built on [PyP6XER](https://github.com/HassanEmam/PyP6Xer).

It exposes **38 tools**, **3 resources**, and **2 prompts** so any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.) can parse, query, analyze, edit, recalculate, and export P6 schedules.

> **Attribution / 工具来源**
>
> This project is based on the open-source project
> [osama-ata/p6xer-mcp-server](https://github.com/osama-ata/p6xer-mcp-server).
> The original project provides the 13 read-only analysis tools listed in
> [§ Tool provenance](#tool-provenance--工具来源说明). This repository adds a
> deterministic CPM engine, an editable in-memory workspace, XER write-back,
> validation, and AI-assisted analysis (25 additional tools).
>
> 本项目基于开源项目 [osama-ata/p6xer-mcp-server](https://github.com/osama-ata/p6xer-mcp-server)：
> 原有的 13 个只读分析工具来自上游，其余 25 个工具（CPM 重算、工作区编辑、XER 导出、校验、AI 分析）
> 由本项目新增。公开版已移除与具体项目投标流程、EPC 模板库、内部自动更新工作流相关的工具与数据。

---

## Features

- **Read-only analysis** — projects, activities, WBS, relationships, calendars, resources, critical path, earned value, DCMA-style quality checks.
- **Schedule graph** — build a node/edge graph from an XER file, detect cycles, export JSON.
- **Deterministic CPM** — full forward/backward pass with all four relationship types (FS/SS/FF/SF + lag), P6 calendars, start/finish constraints, completed-activity anchoring, and zero-duration milestones.
- **Editable workspace** — open an XER into memory, apply structured operations, preview changes, recalculate, and export a P6-compatible XER.
- **Validation** — pre-check operations (task existence, field types, relationship/constraint values) before mutating the graph.
- **AI analysis** — send schedule context to any OpenAI-compatible LLM for an open-ended risk/quality review (optional).

---

## Installation

```bash
# Clone the repository
git clone https://github.com/FTDWW/p6xer-cpm-mcp.git
cd p6xer-cpm-mcp

# Install with uv (recommended)
uv sync

# Or with pip
pip install "p6xer-cpm-mcp"
```

Python 3.10+ is required.

## Running

**Development / MCP Inspector:**

```bash
uv run mcp dev src/p6xer_mcp_server/server.py
```

**Stdio (Claude Desktop / Claude Code):**

```bash
uv run p6xer-cpm-mcp
```

---

## Client configuration

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "p6xer": {
      "command": "uvx",
      "args": ["p6xer-cpm-mcp"]
    }
  }
}
```

Config file locations:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

### Claude Code (CLI)

```bash
claude mcp add p6xer -- uvx p6xer-cpm-mcp
```

### AI analysis tool (optional)

`analyze_schedule_ai` calls an OpenAI-compatible chat-completions endpoint.
Set the key before starting the server:

```bash
export P6XER_LLM_API_KEY=...        # or DEEPSEEK_API_KEY
# optional overrides:
export P6XER_LLM_BASE_URL=https://api.deepseek.com/v1
export P6XER_LLM_MODEL=deepseek-chat
```

The tool fails gracefully with a clear error if no key is configured. All other
tools work without any LLM.

---

## Tools (38)

### Upstream read-only analysis (13)

| Tool | Description |
|------|-------------|
| `parse_xer_file` | Parse an XER file — project list, totals, status breakdowns |
| `get_project_activities` | Activities filtered by project, status, or task type |
| `get_critical_path` | Critical path activities (float ≤ 0), sorted by early start |
| `analyze_resource_utilization` | Planned/actual hours and costs per resource; over-allocation flags |
| `check_schedule_quality` | DCMA-style check: missing logic, long durations, high float, unresourced tasks |
| `get_resources` | List resources, optionally filtered by type |
| `get_resource_assignments` | Resource–activity assignments with names and costs |
| `get_wbs` | Work Breakdown Structure hierarchy |
| `get_relationships` | Predecessor/successor relationships enriched with task codes |
| `get_calendars` | Calendar definitions with hours-per-period data |
| `get_schedule_summary` | Counts, date range, and critical count at a glance |
| `get_earned_value` | EVM: PV, EV, AC, CV, SV, CPI, SPI, EAC per project |
| `get_activity_detail` | Full detail for one activity (predecessors + successors + resources) |

### Added by this project (25)

**Schedule graph & CPM**

| Tool | Description |
|------|-------------|
| `export_schedule_graph` | Export the schedule as nodes/edges JSON with cycle detection |
| `open_schedule_workspace` | Open an XER into an editable in-memory workspace |
| `close_workspace` | Close a workspace and free its memory |
| `list_workspaces` | List all open workspaces |
| `get_workspace_info` | Metadata about a workspace |
| `clone_workspace` | Deep-copy a workspace in memory (no disk I/O) |
| `apply_schedule_operations` | Apply a batch of structured operations |
| `recalculate_schedule` | Run the CPM forward/backward pass |
| `preview_schedule_changes` | Compare recalculated dates against the original XER |
| `export_schedule_xer` | Write the workspace back to a P6-compatible XER |
| `run_cpm_diagnostics` | Cycle detection, orphan activities, CPM summary |
| `get_activity_cpm_dates` | ES/EF/LS/LF/TF/FF for one activity |
| `list_activities_after_cpm` | All activities with CPM dates, sorted and paginated |
| `get_milestones` | All milestone activities (`TT_Mile` / `TT_FinMile`) |

**Editing & validation**

| Tool | Description |
|------|-------------|
| `validate_operations` | Pre-validate operations without applying them |
| `set_activity_duration` | Set an activity's duration in hours |
| `set_activity_duration_days` | Set an activity's duration in calendar days |
| `add_activity_predecessor` | Add or update a predecessor relationship (upsert) |
| `remove_activity_predecessor` | Remove a predecessor relationship |
| `set_activity_constraint` | Set or remove a date constraint |
| `set_activity_status` | Set an activity's status |
| `create_activity` | Create a new activity |
| `rename_activity` | Rename an activity |
| `delete_activity` | Delete an activity |

**AI analysis**

| Tool | Description |
|------|-------------|
| `analyze_schedule_ai` | Send schedule context to an OpenAI-compatible LLM for analysis |

### Resources (3)

| URI | Description |
|-----|-------------|
| `xer-project://{file_path}/{project_id}` | Text summary of a specific project |
| `xer-activities://{file_path}` | Activities summary with status breakdown |
| `xer-resources://{file_path}` | Resources summary with assignment statistics |

### Prompts (2)

| Prompt | Types |
|--------|-------|
| `analyze_xer_project` | `general` · `schedule` · `resources` · `progress` · `quality` |
| `xer_reporting_prompt` | `executive` · `detailed` · `critical_path` · `resource` · `milestone` |

---

## Typical CPM workflow

```
open_schedule_workspace("project.xer")
  → resolve_task / get_activity_detail          # identify what to change
  → validate_operations([...])                  # optional pre-check
  → apply_schedule_operations([...])            # edit the graph
  → recalculate_schedule()                      # CPM forward/backward pass
  → preview_schedule_changes()                  # review the diff
  → export_schedule_xer("project_modified.xer") # write back to XER
```

All date propagation is done by the deterministic `ScheduleEngine`; the LLM only
chooses operations and parameters.

---

## Example prompts

```
Parse project.xer and give me an overview of all projects

What are the critical path activities, sorted by start date?

Run a DCMA schedule quality check on project.xer

Calculate earned value metrics — what are the CPI and SPI?

Open project.xer as a workspace, set activity A1020 to 10 days,
recalculate, and show me what moved.
```

---

## Project structure

```
p6xer-cpm-mcp/
├── src/p6xer_mcp_server/
│   ├── server.py              # FastMCP server: tools, resources, prompts
│   ├── schedule_graph.py      # Directed graph + cycle detection + topo sort
│   ├── schedule_engine.py     # Deterministic CPM engine + calendar arithmetic
│   ├── schedule_workspace.py  # In-memory editable workspace + XER export
│   ├── xer_writer.py          # Template-based XER writer (P6-compatible)
│   ├── graph_validator.py     # Structural validation of operations
│   └── llm_client.py          # Optional OpenAI-compatible LLM client
├── manifest.json              # MCPB bundle manifest
├── mcp.json                   # MCP registry metadata
├── server.json                # MCP registry server descriptor
├── smithery.yaml              # Smithery registry configuration
├── pyproject.toml
└── README.md
```

---

## Tool provenance / 工具来源说明

| 来源 | 数量 | 工具 |
|------|------|------|
| 上游 `osama-ata/p6xer-mcp-server` | 13 | `parse_xer_file`, `get_project_activities`, `get_critical_path`, `analyze_resource_utilization`, `check_schedule_quality`, `get_resources`, `get_resource_assignments`, `get_wbs`, `get_relationships`, `get_calendars`, `get_schedule_summary`, `get_earned_value`, `get_activity_detail` |
| 本项目新增 | 25 | `export_schedule_graph`, `open_schedule_workspace`, `close_workspace`, `list_workspaces`, `get_workspace_info`, `clone_workspace`, `apply_schedule_operations`, `recalculate_schedule`, `preview_schedule_changes`, `export_schedule_xer`, `run_cpm_diagnostics`, `get_activity_cpm_dates`, `list_activities_after_cpm`, `get_milestones`, `validate_operations`, `set_activity_duration`, `set_activity_duration_days`, `add_activity_predecessor`, `remove_activity_predecessor`, `set_activity_constraint`, `set_activity_status`, `create_activity`, `rename_activity`, `delete_activity`, `analyze_schedule_ai` |

**公开版移除了**与原项目投标/招标进度计划自动调整、EPC 模板库、机组作用域、经验表等内部工作流相关的工具与数据；因此本仓库不包含任何真实项目名称、XER 文件、里程碑编码或内部经验数据。

---

## License

MIT. See [LICENSE](LICENSE). The original copyright notice is retained.