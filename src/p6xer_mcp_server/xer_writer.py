"""
Template-based XER writer that preserves original file structure.

Parses the original XER, keeps unmodified sections as raw bytes (never
decoded, so no encoding artifacts), and allows surgical field-level
updates to TASK and TASKPRED rows.  Output matches P6 expectations
because passthrough sections are byte-identical to the source.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class _EditableSection:
    """Parsed TASK or TASKPRED section that can be modified."""

    table_name: str
    fields_line: str
    field_names: list[str]
    field_index: dict[str, int]
    rows: list[list[str]]
    # task_id -> row index (TASK) or (task_id, pred_task_id) -> row index (TASKPRED)
    row_index: dict = field(default_factory=dict)


class XERWriter:
    """Reads an XER file, allows surgical edits, writes P6-compatible output.

    Unmodified sections are kept as raw bytes — they are never decoded or
    re-encoded, so the original byte sequences (including any encoding
    quirks like the 0x80 Euro sign in GB18030 files) pass through unchanged.

    Usage:
        w = XERWriter("input.xer")
        w.update_task(102317, {"target_drtn_hr_cnt": "160", ...})
        w.add_relationship(task_id=102318, pred_task_id=102317, ...)
        w.remove_relationship(task_id=102319, pred_task_id=102318)
        w.write("output.xer")
    """

    def __init__(self, file_path: str, encoding: str | None = None) -> None:
        self._file_path = file_path
        self._passthrough: dict[str, bytes] = {}  # table_name -> raw bytes
        self._editable: dict[str, _EditableSection] = {}
        self._section_order: list[str] = []
        self._ermhdr: bytes = b""
        self._source_encoding: str = "utf-8"
        self._parse(file_path, encoding)

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------

    def _parse(self, file_path: str, encoding: str | None) -> None:
        with open(file_path, "rb") as fh:
            raw_data = fh.read()

        # Detect encoding for text sections (TASK, TASKPRED)
        self._source_encoding = encoding or self._detect_encoding(raw_data)

        # Split into ERMHDR + sections
        # Find first %T marker
        first_t = raw_data.find(b"%T\t")
        if first_t == -1:
            raise ValueError("No %T marker found — not a valid XER file")

        # ERMHDR is everything before first %T (minus trailing CRLF)
        ermhdr_end = first_t
        if ermhdr_end >= 2 and raw_data[ermhdr_end - 2:ermhdr_end] == b"\r\n":
            ermhdr_end -= 2
        elif raw_data[ermhdr_end - 1] == ord("\n"):
            ermhdr_end -= 1
        self._ermhdr = raw_data[:ermhdr_end]

        # Find all %T markers and %E
        end_marker = raw_data.find(b"%E")
        if end_marker == -1:
            end_marker = len(raw_data)

        t_markers: list[tuple[int, str]] = []  # (position, table_name)
        pos = first_t
        while pos < end_marker:
            line_end = raw_data.find(b"\n", pos)
            if line_end == -1:
                break
            line = raw_data[pos:line_end].rstrip(b"\r")
            if line.startswith(b"%T\t"):
                name = line.decode("ascii")[3:]  # skip "%T\t"
                t_markers.append((pos, name))
            pos = line_end + 1

        # Extract each section's raw bytes
        for idx, (start_pos, table_name) in enumerate(t_markers):
            if idx + 1 < len(t_markers):
                end_pos = t_markers[idx + 1][0]
            else:
                end_pos = end_marker
            section_bytes = raw_data[start_pos:end_pos]
            self._section_order.append(table_name)

            if table_name in ("TASK", "TASKPRED"):
                self._editable[table_name] = self._parse_editable(
                    table_name, section_bytes, self._source_encoding
                )
            else:
                self._passthrough[table_name] = section_bytes

    @staticmethod
    def _detect_encoding(raw_data: bytes) -> str:
        try:
            raw_data.decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            pass
        for enc in ("gb18030", "gbk", "cp936"):
            try:
                raw_data.decode(enc)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue
        return "utf-8"

    def _parse_editable(
        self, table_name: str, raw_bytes: bytes, encoding: str
    ) -> _EditableSection:
        """Parse a TASK or TASKPRED section from raw bytes into editable form."""
        # Decode just this section as text
        text = raw_bytes.decode(encoding, errors="replace")
        lines = text.split("\n")

        section = _EditableSection(
            table_name=table_name,
            fields_line="",
            field_names=[],
            field_index={},
            rows=[],
            row_index={},
        )

        for line in lines:
            line = line.rstrip("\r")
            if not line:
                continue
            if line.startswith("%F\t"):
                section.fields_line = line
                field_names = line.split("\t")[1:]
                section.field_names = field_names
                section.field_index = {n: idx for idx, n in enumerate(field_names)}
            elif line.startswith("%R\t"):
                values = line.split("\t")[1:]
                expected = len(section.field_names)
                if len(values) < expected:
                    values.extend([""] * (expected - len(values)))
                section.rows.append(values)
                self._index_row(section, values)

        return section

    def _index_row(self, section: _EditableSection, values: list[str]) -> None:
        if section.table_name == "TASK" and "task_id" in section.field_index:
            tid = values[section.field_index["task_id"]]
            if tid:
                section.row_index[int(tid)] = len(section.rows) - 1
        elif section.table_name == "TASKPRED":
            tid_idx = section.field_index.get("task_id")
            pid_idx = section.field_index.get("pred_task_id")
            if tid_idx is not None and pid_idx is not None:
                tid = values[tid_idx]
                pid = values[pid_idx]
                if tid and pid:
                    section.row_index[(int(tid), int(pid))] = len(section.rows) - 1

    # ------------------------------------------------------------------
    # Task updates
    # ------------------------------------------------------------------

    def update_task(self, task_id: int, field_values: dict[str, str]) -> None:
        section = self._editable.get("TASK")
        if section is None:
            raise KeyError("No TASK section in XER")
        row_idx = section.row_index.get(task_id)
        if row_idx is None:
            raise KeyError(f"Task {task_id} not found in TASK section")
        row = section.rows[row_idx]
        for field_name, value in field_values.items():
            col = section.field_index.get(field_name)
            if col is not None:
                row[col] = str(value) if value is not None else ""

    # ------------------------------------------------------------------
    # Relationship updates
    # ------------------------------------------------------------------

    def add_relationship(
        self,
        task_id: int,
        pred_task_id: int,
        pred_type: str = "PR_FS",
        lag_hr_cnt: float = 0,
        proj_id: int = 0,
        pred_proj_id: int = 0,
    ) -> None:
        section = self._editable.get("TASKPRED")
        if section is None:
            raise KeyError("No TASKPRED section in XER")
        key = (task_id, pred_task_id)
        if key in section.row_index:
            # Upsert: update existing relationship
            row_idx = section.row_index[key]
            row = section.rows[row_idx]
            for name, val in [
                ("pred_type", pred_type),
                ("lag_hr_cnt", str(float(lag_hr_cnt))),
                ("proj_id", str(proj_id)),
                ("pred_proj_id", str(pred_proj_id)),
            ]:
                col = section.field_index.get(name)
                if col is not None:
                    row[col] = str(val)
            return
        pred_id_idx = section.field_index.get("task_pred_id")
        max_id = 0
        if pred_id_idx is not None:
            for row in section.rows:
                try:
                    max_id = max(max_id, int(row[pred_id_idx] or 0))
                except (ValueError, IndexError):
                    pass
        new_pred_id = max_id + 1
        field_count = len(section.field_names)
        row = [""] * field_count
        for name, val in [
            ("task_pred_id", str(new_pred_id)),
            ("task_id", str(task_id)),
            ("pred_task_id", str(pred_task_id)),
            ("proj_id", str(proj_id)),
            ("pred_proj_id", str(pred_proj_id)),
            ("pred_type", pred_type),
            ("lag_hr_cnt", str(int(float(lag_hr_cnt)))),
        ]:
            idx = section.field_index.get(name)
            if idx is not None:
                row[idx] = val
        section.rows.append(row)
        section.row_index[key] = len(section.rows) - 1

    def add_task(
        self,
        task_id: int,
        task_code: str,
        task_name: str,
        *,
        proj_id: int = 0,
        wbs_id: int = 0,
        clndr_id: int = 1,
        task_type: str = "TT_Task",
        status_code: str = "TK_NotStart",
        target_drtn_hr_cnt: float = 0.0,
        remain_drtn_hr_cnt: float = 0.0,
    ) -> None:
        """Append a new task row to the TASK section."""
        section = self._editable.get("TASK")
        if section is None:
            raise KeyError("No TASK section in XER")

        key = task_id
        if key in section.row_index:
            return  # already exists

        field_count = len(section.field_names)
        row = [""] * field_count
        for name, val in [
            ("task_id", str(task_id)),
            ("proj_id", str(proj_id)),
            ("wbs_id", str(wbs_id)),
            ("clndr_id", str(clndr_id)),
            ("task_type", task_type),
            ("status_code", status_code),
            ("task_code", task_code),
            ("task_name", task_name),
            ("target_drtn_hr_cnt", str(int(float(target_drtn_hr_cnt)))),
            ("remain_drtn_hr_cnt", str(int(float(remain_drtn_hr_cnt)))),
        ]:
            idx = section.field_index.get(name)
            if idx is not None:
                row[idx] = val
        section.rows.append(row)
        section.row_index[key] = len(section.rows) - 1

    def remove_task(self, task_id: int) -> int:
        """Remove a TASK row and all its relationships from TASKPRED.

        Returns the number of relationships removed.
        """
        section = self._editable.get("TASK")
        if section is None:
            return 0
        row_idx = section.row_index.pop(task_id, None)
        if row_idx is None:
            return 0
        section.rows[row_idx] = []  # mark for deletion

        # Remove all relationships involving this task
        rel_removed = 0
        pred_section = self._editable.get("TASKPRED")
        if pred_section is not None:
            to_remove = []
            for (tid, pred_tid), ridx in pred_section.row_index.items():
                if tid == task_id or pred_tid == task_id:
                    to_remove.append((tid, pred_tid))
            for key in to_remove:
                ridx = pred_section.row_index.pop(key, None)
                if ridx is not None:
                    pred_section.rows[ridx] = []
                    rel_removed += 1
        return rel_removed

    def remove_relationship(self, task_id: int, pred_task_id: int) -> bool:
        section = self._editable.get("TASKPRED")
        if section is None:
            return False
        key = (task_id, pred_task_id)
        row_idx = section.row_index.pop(key, None)
        if row_idx is None:
            return False
        section.rows[row_idx] = []  # mark for deletion
        return True

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, output_path: str) -> None:
        """Write all sections. Passthrough sections are raw bytes; TASK
        and TASKPRED are re-encoded to the source encoding."""
        parts: list[bytes] = []

        # ERMHDR with updated date
        ermhdr_text = self._ermhdr.decode("ascii", errors="replace")
        ermhdr_parts = ermhdr_text.split("\t")
        if len(ermhdr_parts) >= 3:
            ermhdr_parts[2] = dt.date.today().strftime("%Y-%m-%d")
        parts.append("\t".join(ermhdr_parts).encode("ascii"))

        for table_name in self._section_order:
            if table_name in self._passthrough:
                parts.append(self._passthrough[table_name])
            else:
                section = self._editable[table_name]
                enc = self._source_encoding
                lines = [
                    f"%T\t{table_name}",
                    section.fields_line,
                ]
                for row in section.rows:
                    if row:
                        lines.append("%R\t" + "\t".join(row))
                # Build text with CRLF, then encode
                text = "\r\n".join(lines) + "\r\n"
                parts.append(text.encode(enc, errors="replace"))

        # %E end marker
        parts.append(b"%E\r\n")

        with open(output_path, "wb") as fh:
            for p in parts:
                fh.write(b"\r\n" if fh.tell() > 0 and not p.startswith(b"\r\n") else b"")
                fh.write(p)

        # Fix: the parts already have trailing CRLF, but we need proper joining
        # Rewrite cleanly
        self._write_clean(output_path)

    def _write_clean(self, output_path: str) -> None:
        """Write with proper CRLF line endings."""
        lines: list[bytes] = []

        # ERMHDR
        ermhdr_text = self._ermhdr.decode("ascii", errors="replace")
        ermhdr_parts = ermhdr_text.split("\t")
        if len(ermhdr_parts) >= 3:
            ermhdr_parts[2] = dt.date.today().strftime("%Y-%m-%d")
        lines.append("\t".join(ermhdr_parts).encode("ascii"))

        for table_name in self._section_order:
            if table_name in self._passthrough:
                # Split passthrough bytes into lines and add
                raw = self._passthrough[table_name]
                # Remove trailing CRLF if present
                if raw.endswith(b"\r\n"):
                    raw = raw[:-2]
                elif raw.endswith(b"\n"):
                    raw = raw[:-1]
                lines.append(raw)
            else:
                section = self._editable[table_name]
                enc = self._source_encoding
                text_lines: list[str] = [
                    f"%T\t{table_name}",
                    section.fields_line,
                ]
                for row in section.rows:
                    if row:
                        text_lines.append("%R\t" + "\t".join(row))
                text = "\r\n".join(text_lines)
                lines.append(text.encode(enc, errors="replace"))

        lines.append(b"%E")

        with open(output_path, "wb") as fh:
            fh.write(b"\r\n".join(lines))
            fh.write(b"\r\n")  # trailing newline
